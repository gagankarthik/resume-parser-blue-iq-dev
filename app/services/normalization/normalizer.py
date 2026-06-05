"""
Post-processing normalization applied after AI parsing.

Covers:
  • Healthcare specialty normalization (BICU → Burn Intensive Care Unit, etc.)
  • Healthcare profession/credential expansion (RN → Registered Nurse, etc.)
  • Degree expansion (MSc → Master of Science)
  • Date format normalization (various formats → YYYY-MM-DD)
  • Stripping credential/licence suffixes from the candidate's name
  • Duplicate skill/specialty removal (case-insensitive)
"""

import re

from app.models.schemas import EducationItem, ExperienceItem, ParsedResumeAI, PersonalInfo
from app.services.normalization.healthcare_taxonomy import (
    PROFESSION_ABBREVIATIONS,
    normalize_specialty,
    resolve_specialty,
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

# Credential / licence / degree tokens that may trail a candidate's name and must
# be stripped (e.g. "Jane Smith, RN BSN" → "Jane Smith"). Lower-cased, dots removed.
_NAME_CREDENTIALS: set[str] = {
    # Nursing
    "rn", "lpn", "lvn", "cna", "crna", "np", "aprn", "fnp", "pmhnp", "agnp",
    "rnfa", "msn", "bsn", "adn", "dnp", "cnm", "cns",
    # Degrees / honorifics
    "md", "do", "phd", "edd", "mba", "bs", "ba", "ms", "ma", "bsc", "msc",
    "faan", "facep", "facp",
    # Respiratory / therapy
    "crt", "rrt", "ot", "otr", "cota", "pt", "dpt", "pta", "slp", "slpa", "ccc",
    # Social work
    "csw", "lcsw", "licsw", "lmsw", "msw",
    # Common clinical certs that get appended to names
    "ccrn", "cen", "cnor", "ocn", "cpn", "cnrn", "wcc", "tcrn",
    "acls", "bls", "pals", "nrp", "tncc",
}

def normalize(parsed: ParsedResumeAI) -> ParsedResumeAI:
    _normalize_personal(parsed.personal_info)
    parsed.skills = _normalize_skills(parsed.skills)
    for edu in parsed.education:
        _normalize_education(edu)
    for exp in parsed.experience:
        _normalize_experience(exp)
    return parsed


def _normalize_personal(personal: PersonalInfo) -> None:
    if personal.full_name:
        personal.full_name = _strip_name_credentials(personal.full_name)


def _strip_name_credentials(name: str) -> str:
    """Remove trailing credential/licence/degree suffixes from a person's name.

    Handles both comma-delimited ("Jane Smith, RN, BSN") and space-appended
    ("Jane Smith RN BSN") forms. The part before the first comma is kept only
    when everything after it is credential-like, so genuine "Last, First"
    names are preserved.
    """
    def _is_cred(token: str) -> bool:
        return token.strip(".").lower() in _NAME_CREDENTIALS

    head, sep, tail = name.partition(",")
    if sep:
        tail_tokens = [t for t in re.split(r"[,\s]+", tail.strip()) if t]
        if tail_tokens and all(_is_cred(t) for t in tail_tokens):
            name = head

    tokens = name.split()
    while len(tokens) > 1 and _is_cred(tokens[-1]):
        tokens.pop()

    return " ".join(tokens).strip(" ,") or name.strip()


def _normalize_skills(skills: list[str]) -> list[str]:
    """
    Normalize each skill/specialty to its canonical healthcare-taxonomy name
    (specialty, abbreviation, or profession/credential), falling back to the
    original value when out-of-taxonomy. Matching is punctuation-insensitive.
    Deduplicates case-insensitively.
    """
    seen: set[str] = set()
    result: list[str] = []

    for skill in skills:
        raw = skill.strip()
        normalized = resolve_specialty(raw) or raw

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

    # Map each per-role specialty to its canonical taxonomy name (dedup, in order)
    if exp.specialties:
        exp.specialties = _normalize_skills(exp.specialties)

    if exp.start_date and exp.start_date.lower() != "present":
        exp.start_date = _normalize_date(exp.start_date) or exp.start_date
    if exp.end_date and exp.end_date.lower() != "present":
        exp.end_date = _normalize_date(exp.end_date) or exp.end_date


def _expand_role_credentials(role: str) -> str:
    """
    Expand credential abbreviations found at the start of role titles.
    e.g. "RN - ICU"  → "Registered Nurse - Intensive Care Unit"
         "CRT NICU"  → "Certified Respiratory Therapist – Neonatal Intensive Care Unit"
         "RN MICU"   → "Registered Nurse – Medical Intensive Care Unit"
    Leaves roles that don't start with a known abbreviation untouched.
    """
    # First try delimiter-based split: " - ", " – ", "/", ","
    parts = re.split(r"\s*[-–/,]\s*", role, maxsplit=1)
    credential = parts[0].strip()
    suffix = parts[1].strip() if len(parts) > 1 else ""

    # If no delimiter found, fall back to whitespace split when first token
    # is a recognised credential (e.g. "CRT NICU", "RN MICU")
    if not suffix and " " in credential:
        first, _, rest = credential.partition(" ")
        if first.lower() in PROFESSION_ABBREVIATIONS:
            credential, suffix = first, rest.strip()

    expanded_credential = PROFESSION_ABBREVIATIONS.get(credential.lower(), credential)
    expanded_suffix = normalize_specialty(suffix) if suffix else ""

    if expanded_suffix:
        sep = " – " if "–" in role else " - "
        return f"{expanded_credential}{sep}{expanded_suffix}"
    return expanded_credential


def _normalize_date(raw: str) -> str | None:
    """Normalize a date to YYYY-MM-DD.

    The exact day is preserved when the input states one (e.g. '2/16/2024'
    → '2024-02-16'). When only month/year (or year) is known, the day falls
    back to the 1st ('Feb 2024' → '2024-02-01', '2024' → '2024-01-01').
    """
    raw = raw.strip()

    # Already full ISO YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # ISO month only → first of month
    if re.match(r"^\d{4}-\d{2}$", raw):
        return f"{raw}-01"

    _MONTH_NAME = (
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    )

    # Month name + day + year  e.g. "Feb 16, 2024", "February 16 2024"
    m = re.search(rf"(?i){_MONTH_NAME}[\s.]+(\d{{1,2}})(?:st|nd|rd|th)?[\s,]+(\d{{4}})", raw)
    if m:
        month = _MONTH_ABBR.get(m.group(1)[:3].lower(), "01")
        return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"

    # Month name + year  e.g. "January 2023" → first of month
    m = re.search(rf"(?i){_MONTH_NAME}[\s,]+(\d{{4}})", raw)
    if m:
        month = _MONTH_ABBR.get(m.group(1)[:3].lower(), "01")
        return f"{m.group(2)}-{month}-01"

    # Numeric M/D/YYYY (US order — day present)
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"

    # Numeric YYYY/M/D (day present)
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    # YYYY/MM or YYYY-MM → first of month
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-01"

    # MM/YYYY or MM-YYYY → first of month
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", raw)
    if m:
        return f"{m.group(2)}-{m.group(1).zfill(2)}-01"

    # Just a year → first of year
    if re.match(r"^\d{4}$", raw):
        return f"{raw}-01-01"

    return None
