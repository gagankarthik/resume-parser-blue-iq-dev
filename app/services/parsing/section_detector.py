"""
Resume section detector - header keyword matching.

Segments resume text into labeled sections for the AI parser.
Sending section-segmented text rather than one blob:
  * Reduces hallucination (AI knows what context it's reading)
  * Reduces token count (AI skips irrelevant prose in each section)
  * Allows partial fallback if one section fails

Duplicate-section handling:
  If a resume has two "Experience" headers (common in travel-nurse CVs
  that list assignment blocks separately), content is APPENDED to the
  existing section rather than overwriting it.

Fallback:
  If fewer than 2 sections are detected, the full text is returned
  under "full_text" so the AI still receives all content.
"""

import re
from collections import OrderedDict

_SECTION_KEYWORDS: dict[str, list[str]] = {
    "summary": [
        "summary", "profile", "objective", "about me", "professional summary",
        "executive summary", "career objective", "personal statement",
        "clinical summary", "nursing summary", "professional profile",
        "about", "overview",
    ],
    "experience": [
        "experience", "work experience", "employment", "work history",
        "career history", "professional experience", "employment history",
        "positions held", "clinical experience", "nursing experience",
        "work assignments", "travel assignments", "agency assignments",
        "hospital experience", "clinical background", "assignments",
        "work assignments", "professional background",
    ],
    "education": [
        "education", "academic background", "academic history",
        "qualifications", "academic qualifications", "degrees",
        "nursing education", "academic training", "schooling",
    ],
    "skills": [
        "skills", "technical skills", "core competencies", "competencies",
        "technologies", "tools", "expertise", "key skills", "proficiencies",
        "clinical skills", "specialties", "specialty areas",
        "clinical specialties", "areas of expertise", "nursing skills",
        "clinical competencies", "unit experience", "floors",
        "clinical background", "proficiency", "capabilities",
    ],
    "certifications": [
        "certifications", "certificates", "licenses", "credentials",
        "accreditations", "professional development", "licensure",
        "nursing licenses", "clinical certifications", "active certifications",
        "certifications and licenses", "license", "certification",
        # Professional associations / memberships / committees are credentials-type
        # content and routinely share ONE mixed heading with certs and licenses
        # (e.g. "Professional Associations/Certifications/Licenses/Collaboratives").
        # Bucketing them here keeps that whole block together for the parser.
        "professional associations", "associations", "association",
        "professional affiliations", "affiliations", "affiliation",
        "memberships", "professional memberships", "membership",
        "collaboratives", "collaborative", "committees", "councils",
        "professional organizations", "professional organisations",
    ],
    "projects": [
        "projects", "personal projects", "side projects", "open source",
        "portfolio", "key projects", "notable projects",
    ],
    "achievements": [
        "achievements", "awards", "honors", "accomplishments",
        "recognition", "publications", "patents", "honors and awards",
    ],
    "languages": [
        "languages", "language skills", "spoken languages",
        "language proficiency",
    ],
    "references": [
        "references", "referees", "professional references",
    ],
}

# A header may be the bare keyword ("Education") OR a compound header that joins it
# to a sibling with a connector ("Education & Training", "Education and Certifications",
# "Licenses / Certifications", "Awards, Honors"). The optional connector clause
# requires an explicit &/and///,/+ right after the keyword, so a prose line that
# merely STARTS with the word ("Experience in a 64-bed ICU") is NOT mistaken for a
# header. `_match_section_header` still bounds the line to a short length.
_HEADER_PATTERNS: dict[str, re.Pattern] = {
    section: re.compile(
        r"^\s*(?:" + "|".join(re.escape(k) for k in keywords) + r")"
        r"(?:\s*(?:&|/|\+|,|and)\s+[\w&/,\s+-]+?)?"
        r"\s*[:\-]?\s*$",
        re.IGNORECASE,
    )
    for section, keywords in _SECTION_KEYWORDS.items()
}

# A flat "keyword -> section" map, used to recognise a COMPOUND header whose parts
# are all section keywords joined by connectors, e.g.
# "Professional Associations/Certifications/Licenses/Collaboratives" (which the
# single-keyword patterns above miss: it is 64 chars and joins its parts with a
# bare "/" and no surrounding spaces). Longest keyword first so a two-word keyword
# ("professional associations") wins over its single-word prefix.
_KEYWORD_TO_SECTION: dict[str, str] = {
    kw: section
    for section, keywords in _SECTION_KEYWORDS.items()
    for kw in keywords
}

# Connectors that can join sibling keywords in a compound header. Unlike the
# single-keyword pattern, the surrounding whitespace is OPTIONAL, so a slash-joined
# header with no spaces ("Certifications/Licenses") still splits cleanly.
_HEADER_CONNECTOR_RE = re.compile(r"\s*(?:&|/|\+|,|\band\b)\s*", re.IGNORECASE)

# A header line is bounded to this length so a prose sentence is never mistaken for
# one. Compound headers ("A/B/C/D") run longer than a single keyword, so this is a
# little roomier than the 60-char cap used for single-keyword matching.
_MAX_HEADER_LEN = 90


def _match_compound_header(stripped: str) -> str | None:
    """Detect a header made ENTIRELY of section keywords joined by connectors.

    Returns the section of the FIRST keyword token, or None if any token is not a
    known section keyword - so a real prose line (whose words are not all keywords)
    is never mistaken for a header. This is what recognises a mixed credentials
    heading like "Professional Associations/Certifications/Licenses/Collaboratives".
    """
    core = stripped.strip().rstrip(":-").strip()
    tokens = [t.strip() for t in _HEADER_CONNECTOR_RE.split(core) if t.strip()]
    if len(tokens) < 2:
        return None
    sections = [_KEYWORD_TO_SECTION.get(t.lower()) for t in tokens]
    if all(sections):
        return sections[0]
    return None


def detect(text: str) -> dict[str, str]:
    """
    Split resume into labeled sections.
    Returns {section_name: section_text}.
    Falls back to {"full_text": text} when fewer than 2 sections found.
    """
    lines   = text.splitlines()
    # OrderedDict preserves insertion order; values are lists of lines
    buckets: OrderedDict[str, list[str]] = OrderedDict()
    buckets["header"] = []   # content before first detected section
    current = "header"

    for line in lines:
        detected = _match_section_header(line)
        if detected:
            current = detected
            if current not in buckets:
                buckets[current] = []
            # Don't reset - content from duplicate headers is appended below
        else:
            buckets[current].append(line)

    # Build result dict, dropping empty sections
    result: dict[str, str] = {}
    for key, lines_list in buckets.items():
        block = "\n".join(lines_list).strip()
        if block:
            result[key] = block

    # Fallback: fewer than 2 named sections detected -> return full text
    named = {k: v for k, v in result.items() if k != "header"}
    if len(named) < 2:
        return {"full_text": text}

    return result


def _match_section_header(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADER_LEN:
        return None
    # Single-keyword headers (optionally with one connector clause). Kept at the
    # tighter 60-char bound so a long prose line can't slip through this looser rule.
    if len(stripped) <= 60:
        for section, pattern in _HEADER_PATTERNS.items():
            if pattern.match(stripped):
                return section
    # Compound headers: every connector-joined part is a known section keyword.
    return _match_compound_header(stripped)
