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
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LicenseItem,
    ParsedResumeAI,
    PersonalInfo,
    _sanitize_date,
)
from app.services.normalization import specialty_matcher
from app.services.normalization.healthcare_taxonomy import (
    PROFESSION_ABBREVIATIONS,
    normalize_specialty,
    resolve_specialty,
)
from app.services.normalization.specialty_catalog import get_catalog

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
    # Spelled-out forms — fix grammar ("Associates"→"Associate") to a canonical name.
    "associates in nursing": "Associate Degree in Nursing",
    "associate in nursing": "Associate Degree in Nursing",
    "associates degree in nursing": "Associate Degree in Nursing",
    "associate degree in nursing": "Associate Degree in Nursing",
    "associates of science in nursing": "Associate of Science in Nursing",
    "bachelors of science in nursing": "Bachelor of Science in Nursing",
    "bachelor of science in nursing": "Bachelor of Science in Nursing",
    "masters of science in nursing": "Master of Science in Nursing",
    "master of science in nursing": "Master of Science in Nursing",
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

# US state / territory postal codes → the country they imply. A résumé that gives
# a US state abbreviation (optionally with a ZIP) has stated a US address even when
# it never writes "USA"; we backfill the country deterministically here rather than
# letting the model guess it (the schema tells the model NOT to infer it).
_US_STATE_ABBREVS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})
_US_COUNTRY = "United States"
# "…, NY 14203" / "…, VA 23601" — a two-letter state followed by a 5-digit ZIP.
_US_STATE_ZIP_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")


def normalize(parsed: ParsedResumeAI) -> ParsedResumeAI:
    _normalize_personal(parsed.personal_info)
    parsed.skills = _normalize_skills(parsed.skills)
    parsed.education = _repair_education(parsed.education)
    for edu in parsed.education:
        _normalize_education(edu)
    for exp in parsed.experience:
        _normalize_experience(exp)
    _clean_credential_buckets(parsed)
    for lic in parsed.licenses:
        lic.name = _fix_credential_case(lic.name)
        if lic.license_type:
            lic.license_type = _fix_credential_case(lic.license_type)
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
    personal.credentials = [_fix_credential_case(c) for c in personal.credentials]


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


# ── Credential casing + cross-bucket hygiene ──────────────────────────────────

# Credential tokens whose canonical form is not simply upper-case.
_CRED_CASING_SPECIAL = {"phd": "PhD", "edd": "EdD"}

# Practice credentials that are professional LICENSES, never certifications.
_PRACTICE_LICENSE_TYPES = {"rn", "lpn", "lvn"}

# Academic degrees that sometimes leak into skills[] — they belong in education /
# post-nominal credentials, not in the skills list.
_DEGREE_SKILL_TOKENS = {"bsn", "msn", "adn", "asn", "dnp", "phd", "mph", "mba"}

# Well-known certifications (and listed non-clinical credentials) that belong in
# certifications[], not skills[]. Includes a common résumé misspelling.
_CERT_SKILL_TOKENS = {
    "bls", "acls", "pals", "cpr", "nrp", "tncc", "enpc", "nihss", "stable",
    "ccrn", "cen", "cnor", "ocn", "cpn", "cnrn", "wcc", "tcrn", "first aid",
    "neonatal resuscitation program", "neonatal resucitation program",
    "advanced cardiac life support", "basic life support",
    "pediatric advanced life support", "drivers license", "driver license",
}

_CERT_SUFFIX_RE = re.compile(
    r"\s+(certification|certificate|certified|cert|card)s?\s*$", re.I
)


def _fix_credential_case(token: str) -> str:
    """Restore canonical casing on a credential the model lower-cased ('rn' → 'RN')."""
    t = token.strip()
    key = t.strip(".").lower()
    if key in _CRED_CASING_SPECIAL:
        return _CRED_CASING_SPECIAL[key]
    if key in _NAME_CREDENTIALS or key in PROFESSION_ABBREVIATIONS:
        return t.upper()
    return t


def _cred_key(value: str) -> str:
    """Matching key for cross-bucket comparison: case/punctuation-insensitive,
    with trailing 'Certification'/'Certified'/'Card' noise stripped
    ('CPR Certification' → 'cpr', \"Driver's License\" → 'drivers license')."""
    v = _CERT_SUFFIX_RE.sub("", value.strip().lower())
    v = re.sub(r"[^a-z0-9 ]", "", v)
    return re.sub(r"\s+", " ", v).strip()


def _clean_credential_buckets(parsed: ParsedResumeAI) -> None:
    """Deterministic cross-bucket hygiene after parsing.

    The LLM is told how to bucket skills vs certifications vs licenses, but it
    still leaks ('CPR Certification' in skills, 'LPN' filed as a certification).
    This pass enforces the rules instead of hoping:
      • An RN/LPN/LVN entry in certifications[] is a professional licence —
        promote it into licenses[] (unless that licence type already exists).
      • Drop certifications that duplicate an existing licence.
      • skills[] loses anything that matches an extracted certification/licence
        or an academic degree token; a well-known cert found ONLY in skills[]
        (CPR, BLS, Driver's License…) is MOVED to certifications[], not lost.
    """
    license_types = {lt for lic in parsed.licenses if (lt := (lic.license_type or "").lower())}
    license_keys = {_cred_key(lic.name) for lic in parsed.licenses} | license_types

    kept_certs: list[CertificationItem] = []
    for cert in parsed.certifications:
        key = _cred_key(cert.name)
        if key in _PRACTICE_LICENSE_TYPES:
            if key not in license_types:
                parsed.licenses.append(
                    LicenseItem(
                        name=cert.name.upper(),
                        license_type=cert.name.upper(),
                        issued_date=cert.issued_date,
                        expiry_date=cert.expiry_date,
                    )
                )
                license_types.add(key)
                license_keys.add(key)
            continue
        if key in license_keys:
            continue
        kept_certs.append(cert)
    parsed.certifications = kept_certs

    cert_keys = {_cred_key(c.name) for c in parsed.certifications}
    kept_skills: list[str] = []
    for skill in parsed.skills:
        key = _cred_key(skill)
        if key and (key in cert_keys or key in license_keys or key in _DEGREE_SKILL_TOKENS):
            continue
        if key in _CERT_SKILL_TOKENS:
            parsed.certifications.append(
                CertificationItem(name=_CERT_SUFFIX_RE.sub("", skill.strip()))
            )
            cert_keys.add(key)
            continue
        kept_skills.append(skill)
    parsed.skills = kept_skills


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


def _norm_institution_key(institution: str | None) -> str:
    return (institution or "").strip().lower()


def _repair_education(items: list[EducationItem]) -> list[EducationItem]:
    """Reattach orphaned degrees to their institution and drop split-header stubs.

    Résumés list one school header followed by several degree/date lines
    ("ECPI University" / "Associates in Nursing: 2018" / "BSN: 2019"). The extractor
    can split that into a degree-less institution entry plus sibling entries whose
    institution came back blank ("Unknown Institution"). This:
      1. carries the most recent real institution forward onto those placeholder
         entries — a degree written under a school belongs to that school; and
      2. removes the now-redundant degree-less header stub when a sibling entry
         carries a degree for the same institution,
    so the ECPI header + two blank degree lines collapse into two clean, correctly
    attributed degree entries. Order is preserved.
    """
    placeholders = {"", "unknown institution", "unknown"}

    last_real: str | None = None
    for edu in items:
        if _norm_institution_key(edu.institution) not in placeholders:
            last_real = edu.institution.strip()
        elif last_real:
            edu.institution = last_real

    institutions_with_degree = {
        _norm_institution_key(edu.institution) for edu in items if edu.degree
    }
    kept: list[EducationItem] = []
    for edu in items:
        is_header_stub = not (edu.degree or edu.graduation_year or edu.start_year or edu.gpa)
        if is_header_stub and _norm_institution_key(edu.institution) in institutions_with_degree:
            continue  # a bare school header whose degrees are captured on sibling rows
        kept.append(edu)
    return kept


def _normalize_education(edu: EducationItem) -> None:
    if edu.degree:
        edu.degree = _DEGREE_MAP.get(edu.degree.lower().strip(), edu.degree)


def _infer_country(exp: ExperienceItem) -> None:
    """Backfill a US country when the role states a US state (+ZIP) but no country.

    Fires only on an unambiguous US signal — a state field that is a US postal
    abbreviation, or a "State ZIP" tail in the location line — so an international
    address is never mislabeled. Never overrides a country the résumé stated.
    """
    if exp.country:
        return
    state = (exp.state or "").strip().upper()
    if state in _US_STATE_ABBREVS:
        exp.country = _US_COUNTRY
        return
    if exp.location and _US_STATE_ZIP_RE.search(exp.location):
        m = _US_STATE_ZIP_RE.search(exp.location)
        if m and m.group(1).upper() in _US_STATE_ABBREVS:
            exp.country = _US_COUNTRY


# A part that reads like a street line (starts with a number, or names a street
# type / unit) rather than a city — used to decide where the street ends.
_STREET_HINT_RE = re.compile(
    r"^\d|\b(?:st|street|ave|avenue|road|rd|blvd|boulevard|dr|drive|lane|ln|way|"
    r"court|ct|circle|cir|place|pl|square|sq|terrace|trail|hwy|highway|parkway|"
    r"pkwy|suite|ste|apt|apartment|unit|floor|fl|building|bldg|#)\b",
    re.IGNORECASE,
)
# "NY 14203" / "NY 14203-1234" — a US state abbreviation followed by a ZIP.
_STATE_ZIP_TAIL_RE = re.compile(r"^([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$")
_ZIP_ONLY_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def _looks_like_street(part: str) -> bool:
    return bool(_STREET_HINT_RE.search(part.strip()))


def _refine_location_to_street(exp: ExperienceItem) -> None:
    """Reduce a full address in `location` to just the street line.

    An experience entry keeps city / state / zip_code / country as their own
    fields, so a `location` of "818 Ellicott Street, Buffalo, NY 14203" duplicates
    them. Split the full line: backfill any missing city/state/zip from the tail
    (never overriding an extracted value) and set `location` to the street only
    ("818 Ellicott Street"). Conservative — only acts on an unambiguous US-style
    "…, City, ST ZIP" tail; an international or unsplittable line is left as-is so
    no data is lost.
    """
    loc = (exp.location or "").strip()
    if not loc or "," not in loc:
        return  # already a bare street/city, or nothing to split

    parts = [p.strip() for p in loc.split(",") if p.strip()]
    state = zip_code = city = None

    tail = parts[-1]
    m = _STATE_ZIP_TAIL_RE.match(tail)
    if m and m.group(1).upper() in _US_STATE_ABBREVS:
        state, zip_code = m.group(1).upper(), m.group(2)
        parts = parts[:-1]
    elif tail.upper() in _US_STATE_ABBREVS:
        state = tail.upper()
        parts = parts[:-1]
    elif _ZIP_ONLY_RE.match(tail):
        zip_code = tail
        parts = parts[:-1]
        if parts and parts[-1].upper() in _US_STATE_ABBREVS:
            state = parts[-1].upper()
            parts = parts[:-1]

    # Only carve out a city when we found a real state/zip tail — otherwise we
    # cannot reliably tell a trailing street fragment ("Building B") from a city.
    if not (state or zip_code):
        return

    if len(parts) >= 2:
        city = parts[-1]
        parts = parts[:-1]
    elif len(parts) == 1 and not _looks_like_street(parts[0]):
        city = parts[0]
        parts = []

    # A missing comma can glue a suite/unit onto the city ("Suite 300 Williamsville").
    # The suite belongs to the street; keep only the trailing token as the city.
    if city and _looks_like_street(city):
        words = city.split()
        if len(words) >= 2:
            parts.append(" ".join(words[:-1]))
            city = words[-1]

    street = ", ".join(parts).strip(" ,") or None

    if city and not exp.city:
        exp.city = city
    if state and not exp.state:
        exp.state = state
    if zip_code and not exp.zip_code:
        exp.zip_code = zip_code
    exp.location = street


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

    if exp.profession:
        exp.profession = _fix_credential_case(exp.profession)

    # Map the role's credential to its platform profession id. Confidence is 1.0 on
    # a catalog hit (exact name/alias), 0.0 when unknown or no catalog is loaded.
    profession_id = get_catalog().profession_id_for(exp.profession)
    exp.profession_id = profession_id
    exp.profession_confidence = 1.0 if profession_id else 0.0

    # A stated trauma LEVEL means the site IS a trauma facility — backfill the
    # flag the model commonly leaves null when it only captured the level
    # (e.g. "Level 1 Trauma" with trauma_facility=None). Never override an
    # explicit "No"/"N/A" already extracted.
    if exp.trauma_level and exp.trauma_facility is None:
        exp.trauma_facility = "Yes"

    # Reduce a full-address `location` to the street line, backfilling
    # city/state/zip from the tail (they are their own fields on an experience).
    _refine_location_to_street(exp)

    # Backfill the country from an unambiguous US state/ZIP signal (deterministic;
    # the model is told not to guess it).
    _infer_country(exp)

    # Map each per-role specialty to a catalog id + confidence via the tiered
    # matcher (deterministic tiers 1–3; the AI tier runs later in the pipeline).
    # Dedup-by-canonical-name, order preserved.
    if exp.specialties:
        raw_specialties = [(sm.raw or sm.name) for sm in exp.specialties]
        # Scope the id lookup to this role's credential so a name shared across
        # professions (e.g. "ICU") resolves to the right profession's id.
        exp.specialties = specialty_matcher.match_batch(raw_specialties, exp.profession)

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
