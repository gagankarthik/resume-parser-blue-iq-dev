"""
Detect and segment resume sections using header keyword matching.

Returns a dict of {section_name: section_text} which is passed to the
AI parser so it processes sections independently — reduces hallucinations
and lowers token count vs passing the whole resume as one blob.
"""

import re
from collections import OrderedDict

_SECTION_KEYWORDS: dict[str, list[str]] = {
    "summary": [
        "summary", "profile", "objective", "about me", "professional summary",
        "executive summary", "career objective", "personal statement",
        "clinical summary", "nursing summary", "professional profile",
    ],
    "experience": [
        "experience", "work experience", "employment", "work history",
        "career history", "professional experience", "employment history",
        "positions held", "clinical experience", "nursing experience",
        "work assignments", "travel assignments", "agency assignments",
        "hospital experience", "clinical background",
    ],
    "education": [
        "education", "academic background", "academic history",
        "qualifications", "academic qualifications", "degrees",
        "nursing education", "academic training",
    ],
    "skills": [
        "skills", "technical skills", "core competencies", "competencies",
        "technologies", "tools", "expertise", "key skills", "proficiencies",
        "stack", "clinical skills", "specialties", "specialty areas",
        "clinical specialties", "areas of expertise", "nursing skills",
        "clinical competencies", "unit experience", "floors",
    ],
    "certifications": [
        "certifications", "certificates", "licenses", "credentials",
        "accreditations", "professional development", "licensure",
        "nursing licenses", "clinical certifications", "active certifications",
        "bls", "acls", "pals", "certifications and licenses",
    ],
    "projects": [
        "projects", "personal projects", "side projects",
        "open source", "portfolio", "key projects",
    ],
    "achievements": [
        "achievements", "awards", "honors", "accomplishments",
        "recognition", "publications", "patents",
    ],
    "languages": [
        "languages", "language skills", "spoken languages",
    ],
    "references": [
        "references", "referees",
    ],
}

# Pre-compiled per section: line is a header if it matches a keyword (whole word, case-insensitive)
_HEADER_PATTERNS: dict[str, re.Pattern] = {
    section: re.compile(
        r"^\s*(?:" + "|".join(re.escape(k) for k in keywords) + r")\s*[:\-]?\s*$",
        re.IGNORECASE,
    )
    for section, keywords in _SECTION_KEYWORDS.items()
}

# Matches lines that look like headers: short, possibly ALL CAPS, no sentence punctuation
_GENERIC_HEADER = re.compile(r"^[A-Z][A-Za-z\s&/]{2,40}$")


def detect(text: str) -> dict[str, str]:
    """
    Split resume text into labeled sections.
    Falls back to passing the full text under 'full_text' if no sections found.
    """
    lines = text.splitlines()
    sections: OrderedDict[str, list[str]] = OrderedDict()
    current_section = "header"  # content before first detected section
    sections[current_section] = []

    for line in lines:
        detected = _detect_section_header(line)
        if detected:
            current_section = detected
            if current_section not in sections:
                sections[current_section] = []
        else:
            sections[current_section].append(line)

    result = {k: "\n".join(v).strip() for k, v in sections.items() if "\n".join(v).strip()}

    if len(result) <= 1:
        # No sections detected — return full text for the AI to handle
        return {"full_text": text}

    return result


def _detect_section_header(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return None
    for section, pattern in _HEADER_PATTERNS.items():
        if pattern.match(stripped):
            return section
    return None
