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

from app.models.schemas import (
    EducationItem,
    ExperienceItem,
    ParsedResumeAI,
    PersonalInfo,
    _sanitize_date,
)
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

# Credential / licence / degree tokens that may trail a candidate's name and must
# be stripped (e.g. "Jane Smith, RN BSN" → "Jane Smith"). Lower-cased, dots removed.
_NAME_CREDENTIALS: set[str] = {
    # Nursing
    "rn", "lpn", "lvn", "cna", "crna", "np", "aprn", "fnp", "pmhnp", "agnp",
    "rnfa", "msn", "bsn", "adn", "dnp", "cnm", "cns",
    # Degrees / honorifics
    "md", "do", "phd", "edd", "mba", "bs", "ba", "ms", "ma", "bsc", "msc",
    "mph", "mha", "mhsa", "msph", "faan", "facep", "facp",
    # Respiratory / therapy
    "crt", "rrt", "ot", "otr", "cota", "pt", "dpt", "pta", "slp", "slpa", "ccc",
    # Imaging / allied health
    "rt", "arrt", "rdms", "rdcs", "rvt", "cnmt", "nmtcb",
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
        # Capture any trailing credentials BEFORE stripping them off the name, so
        # post-nominals like "RN, BSN" are never silently lost. Merge with any
        # credentials the model already supplied (case-insensitive dedup, order
        # preserved: model-supplied first, then newly recovered).
        recovered = _extract_name_credentials(personal.full_name)
        if recovered:
            personal.credentials = _dedup_preserve_order(
                [*personal.credentials, *recovered]
            )
        personal.full_name = _strip_name_credentials(personal.full_name)
    elif personal.credentials:
        personal.credentials = _dedup_preserve_order(personal.credentials)


def _dedup_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        token = v.strip()
        key = token.strip(".").lower()
        if token and key not in seen:
            seen.add(key)
            result.append(token)
    return result


def _extract_name_credentials(name: str) -> list[str]:
    """Return the trailing credential tokens of a name, in original case.

    Mirrors `_strip_name_credentials` so exactly the tokens that get removed from
    the name are the ones recovered into personal_info.credentials. Returns [] for
    a genuine "Last, First" name (nothing credential-like to strip).
    """
    def _is_cred(token: str) -> bool:
        return token.strip(".").lower() in _NAME_CREDENTIALS

    recovered: list[str] = []

    head, sep, tail = name.partition(",")
    if sep:
        tail_tokens = [t for t in re.split(r"[,\s]+", tail.strip()) if t]
        if tail_tokens and all(_is_cred(t) for t in tail_tokens):
            recovered.extend(t.strip(".") for t in tail_tokens)
            name = head

    tokens = name.split()
    trailing: list[str] = []
    while len(tokens) > 1 and _is_cred(tokens[-1]):
        trailing.append(tokens.pop().strip(".,"))
    recovered.extend(reversed(trailing))

    return recovered


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
    # Backfill a missing/Unknown role from the most specific signal available
    # (the role-level profession or agency) before expansion, so a travel-
    # assignment sub-entry that lost its title isn't left as a bare "Unknown".
    if (not exp.role) or exp.role.strip().lower() == "unknown":
        if exp.profession:
            exp.role = exp.profession
        elif exp.agency_name:
            exp.role = exp.agency_name

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
    """Normalize a date to MM/DD/YYYY, MM/YYYY, or YYYY — preserving the stated
    precision and never inventing a missing day or month. Delegates to the shared
    parser in schemas so the schema-time and post-processing behaviour are identical
    (e.g. '2/16/2024' → '02/16/2024', 'August 2018' → '08/2018', '2019' → '2019').
    """
    return _sanitize_date(raw)
