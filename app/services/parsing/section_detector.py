"""
Resume section detector — header keyword matching.

Segments resume text into labeled sections for the AI parser.
Sending section-segmented text rather than one blob:
  • Reduces hallucination (AI knows what context it's reading)
  • Reduces token count (AI skips irrelevant prose in each section)
  • Allows partial fallback if one section fails

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

_HEADER_PATTERNS: dict[str, re.Pattern] = {
    section: re.compile(
        r"^\s*(?:" + "|".join(re.escape(k) for k in keywords) + r")\s*[:\-]?\s*$",
        re.IGNORECASE,
    )
    for section, keywords in _SECTION_KEYWORDS.items()
}


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
            # Don't reset — content from duplicate headers is appended below
        else:
            buckets[current].append(line)

    # Build result dict, dropping empty sections
    result: dict[str, str] = {}
    for key, lines_list in buckets.items():
        block = "\n".join(lines_list).strip()
        if block:
            result[key] = block

    # Fallback: fewer than 2 named sections detected → return full text
    named = {k: v for k, v in result.items() if k != "header"}
    if len(named) < 2:
        return {"full_text": text}

    return result


def _match_section_header(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return None
    for section, pattern in _HEADER_PATTERNS.items():
        if pattern.match(stripped):
            return section
    return None
