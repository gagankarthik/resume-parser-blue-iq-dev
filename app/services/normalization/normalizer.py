"""
Post-processing normalization applied after AI parsing.

Covers:
  • Healthcare specialty normalization (BICU → Burn Intensive Care Unit, etc.)
  • Healthcare profession/credential expansion (RN → Registered Nurse, etc.)
  • Degree expansion (MSc → Master of Science)
  • Date format normalization (various formats → YYYY-MM)
  • Duplicate skill/specialty removal (case-insensitive)
"""

import re

from app.models.schemas import EducationItem, ExperienceItem, ParsedResumeAI
from app.services.normalization.healthcare_taxonomy import (
    ALL_SPECIALTIES,
    PROFESSION_ABBREVIATIONS,
    SPECIALTY_ABBREVIATIONS,
    normalize_specialty,
)

# ── Degree aliases ────────────────────────────────────────────────────────────
_DEGREE_MAP: dict[str, str] = {
    "bsc": "Bachelor of Science", "b.sc": "Bachelor of Science",
    "b.sc.": "Bachelor of Science", "bs": "Bachelor of Science",
    "ba": "Bachelor of Arts", "b.a": "Bachelor of Arts",
    "be": "Bachelor of Engineering", "b.e": "Bachelor of Engineering",
    "btech": "Bachelor of Technology", "b.tech": "Bachelor of Technology",
    "msc": "Master of Science", "m.sc": "Master of Science",
    "ms": "Master of Science", "m.s": "Master of Science",
    "mba": "Master of Business Administration",
    "mtech": "Master of Technology", "m.tech": "Master of Technology",
    "me": "Master of Engineering", "m.e": "Master of Engineering",
    "phd": "Doctor of Philosophy", "ph.d": "Doctor of Philosophy",
    "phd.": "Doctor of Philosophy",
    # Healthcare-specific
    "adn": "Associate Degree in Nursing",
    "bsn": "Bachelor of Science in Nursing",
    "msn": "Master of Science in Nursing",
    "dnp": "Doctor of Nursing Practice",
}

_MONTH_ABBR = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

# Pre-built lowercase lookup for O(1) canonical specialty matching
_CANONICAL_SPECIALTY_LOOKUP: dict[str, str] = {s.lower(): s for s in ALL_SPECIALTIES}


def normalize(parsed: ParsedResumeAI) -> ParsedResumeAI:
    parsed.skills = _normalize_skills(parsed.skills)
    for edu in parsed.education:
        _normalize_education(edu)
    for exp in parsed.experience:
        _normalize_experience(exp)
    return parsed


def _normalize_skills(skills: list[str]) -> list[str]:
    """
    Normalize each skill/specialty:
      1. Check healthcare specialty abbreviation map
      2. Check canonical specialty list (case-insensitive match)
      3. Check profession credential map
      4. Fall back to original value
    Deduplicates case-insensitively.
    """
    seen: set[str] = set()
    result: list[str] = []

    for skill in skills:
        raw = skill.strip()
        key = raw.lower()

        # 1. Healthcare specialty abbreviation
        if key in SPECIALTY_ABBREVIATIONS:
            normalized = SPECIALTY_ABBREVIATIONS[key]
        # 2. Already a canonical specialty name
        elif key in _CANONICAL_SPECIALTY_LOOKUP:
            normalized = _CANONICAL_SPECIALTY_LOOKUP[key]
        # 3. Profession/credential expansion
        elif key in PROFESSION_ABBREVIATIONS:
            normalized = PROFESSION_ABBREVIATIONS[key]
        else:
            normalized = raw

        dedup_key = normalized.lower()
        if dedup_key not in seen:
            seen.add(dedup_key)
            result.append(normalized)

    return result


def normalize_specialties_list(specialties: list[str]) -> list[str]:
    """
    Standalone helper — normalize a list of specialty strings independently.
    Useful when specialties are stored separately from skills.
    """
    return _normalize_skills(specialties)


def _normalize_education(edu: EducationItem) -> None:
    if edu.degree:
        edu.degree = _DEGREE_MAP.get(edu.degree.lower().strip(), edu.degree)


def _normalize_experience(exp: ExperienceItem) -> None:
    # Expand credential abbreviations in role titles
    if exp.role:
        exp.role = _expand_role_credentials(exp.role)

    if exp.start_date and exp.start_date.lower() != "present":
        exp.start_date = _normalize_date(exp.start_date) or exp.start_date
    if exp.end_date and exp.end_date.lower() != "present":
        exp.end_date = _normalize_date(exp.end_date) or exp.end_date


def _expand_role_credentials(role: str) -> str:
    """
    Expand credential abbreviations found at the start of role titles.
    e.g. "RN - ICU" → "Registered Nurse - Intensive Care Unit"
         "CRT NICU"  → "Certified Respiratory Therapist – NICU"
    Leaves roles that don't start with a known abbreviation untouched.
    """
    # Split on common separators: " - ", " – ", ", ", " / "
    parts = re.split(r"\s*[-–/,]\s*", role, maxsplit=1)
    credential = parts[0].strip()
    suffix = parts[1].strip() if len(parts) > 1 else ""

    expanded_credential = PROFESSION_ABBREVIATIONS.get(credential.lower(), credential)
    expanded_suffix = normalize_specialty(suffix) if suffix else ""

    if expanded_suffix:
        sep = " – " if "–" in role else " - "
        return f"{expanded_credential}{sep}{expanded_suffix}"
    return expanded_credential


def _normalize_date(raw: str) -> str | None:
    raw = raw.strip()

    # Already ISO YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", raw):
        return raw

    # Month name + year
    m = re.search(
        r"(?i)(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[\s,]+(\d{4})",
        raw,
    )
    if m:
        month_key = m.group(1)[:3].lower()
        month = _MONTH_ABBR.get(month_key, "01")
        return f"{m.group(2)}-{month}"

    # YYYY/MM or YYYY-MM
    m2 = re.match(r"(\d{4})[-/](\d{1,2})$", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2).zfill(2)}"

    # MM/YYYY or MM-YYYY
    m3 = re.match(r"(\d{1,2})[-/](\d{4})$", raw)
    if m3:
        return f"{m3.group(2)}-{m3.group(1).zfill(2)}"

    # Just a year
    if re.match(r"^\d{4}$", raw):
        return f"{raw}-01"

    return None
