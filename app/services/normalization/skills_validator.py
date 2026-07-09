"""
Skills validation against the healthcare taxonomy.

After normalization, every skill string is checked against the canonical
taxonomy (specialties, professions/credentials, and common clinical
certifications). The result tells enterprise clients which skills the parser
could ground in a known healthcare term ("recognized") and which are free-form
or out-of-taxonomy ("unrecognized") — useful for flagging records that need
human review.

This is read-only and deterministic: it derives entirely from the parsed
skills, so it can be recomputed on demand (including after an async job is
reloaded from DynamoDB) without storing anything extra.
"""

import re

from app.models.schemas import ParsedResumeAI, SkillsValidation
from app.services.normalization.healthcare_taxonomy import (
    get_specialty_group,
    resolve_specialty,
)

# Common clinical certifications/credentials that appear in skills lists.
# These are recognized as valid healthcare credentials even though they are not
# specialties or professions.
KNOWN_CERTIFICATIONS: frozenset[str] = frozenset(
    c.lower()
    for c in (
        "BLS", "ACLS", "PALS", "NRP", "CCRN", "CEN", "TNCC", "ENPC", "OCN",
        "CNOR", "CPN", "CMSRN", "PCCN", "RNC", "RNC-NIC", "RNC-OB", "CWOCN",
        "CHPN", "CDE", "CRRN", "SANE", "ONS", "ACLS-EP", "STABLE", "CPR",
    )
)

# Real clinical skills rarely equal a bare specialty name — they read like
# "EKG Rhythms", "Telemetry monitoring", "IV/PICC", "Neonatal health monitoring".
# Recognizing only exact specialties/certs left these at 0% recognized, which
# looks broken. A skill that CONTAINS one of these clinical terms (as a whole
# word) is a genuine healthcare skill and is recognized. Terms are precise
# clinical nouns/acronyms — deliberately not generic words like "monitoring" or
# "assessment" — to avoid false positives.
CLINICAL_SKILL_TERMS: frozenset[str] = frozenset({
    # Cardiac / monitoring
    "ekg", "ecg", "telemetry", "cardiac", "hemodynamic", "arrhythmia", "rhythm",
    "rhythms", "defibrillation", "cardioversion", "pacemaker", "arterial line",
    # Respiratory
    "ventilator", "ventilation", "intubation", "extubation", "tracheostomy",
    "trach", "bipap", "cpap", "oxygenation", "capnography", "nebulizer",
    "suctioning", "abg", "airway",
    # Vascular access / infusion. NOTE: no bare "iv" — as a whole word it also
    # matches the Roman numeral in "Level IV" / "Grade IV", inflating recognition.
    # The real skills carry a qualifier, so match those forms instead.
    "iv therapy", "iv insertion", "iv access", "iv push", "peripheral iv",
    "iv/picc", "picc", "central line", "catheter", "catheterization", "foley",
    "phlebotomy", "venipuncture", "infusion", "cannulation", "port-a-cath",
    # Populations / clinical areas
    "neonatal", "pediatric", "geriatric", "obstetric", "maternal", "perinatal",
    "nicu", "picu", "icu", "ccu", "sicu", "micu", "pacu", "telemetry",
    # Procedures / therapies
    "wound care", "ostomy", "dialysis", "chemotherapy", "transfusion",
    "medication administration", "triage", "suturing", "casting", "titrating",
    "titration", "drips", "sedation", "resuscitation", "glucose monitoring",
    "specimen", "phlebotomy", "wound", "dressing",
    # Charting / systems
    "epic", "cerner", "meditech", "emr", "ehr", "charting", "pyxis", "picis",
})
_CLINICAL_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(t) for t in sorted(CLINICAL_SKILL_TERMS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _is_clinical_skill(name: str) -> bool:
    """True when a free-form skill contains a known clinical term as a whole word
    (e.g. 'Telemetry monitoring' → 'telemetry'; 'IV/PICC' → 'iv'/'picc')."""
    return bool(_CLINICAL_RE.search(name))

def validate_skills(parsed: ParsedResumeAI) -> SkillsValidation:
    """
    Classify each parsed skill against the healthcare taxonomy.

    Recognized  → matched a canonical specialty, profession/credential, a known
                  certification, or a taxonomy abbreviation.
    Unrecognized → free-form skill with no taxonomy match.

    Deduplicates case-insensitively while preserving first-seen order.
    """
    recognized: list[str] = []
    unrecognized: list[str] = []
    groups: dict[str, str] = {}
    seen: set[str] = set()

    for skill in parsed.skills:
        name = skill.strip()
        if not name:
            continue
        key = name.lower()

        canonical = resolve_specialty(name)
        resolved = canonical if canonical is not None else name

        # Dedup on the resolved value so "ICU" and "Intensive Care Unit" collapse.
        dedup_key = resolved.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        if canonical is not None:
            recognized.append(canonical)
            group = get_specialty_group(canonical)
            if group:
                groups[canonical] = group
        elif key in KNOWN_CERTIFICATIONS:
            recognized.append(name)
            groups[name] = "Certification"
        elif _is_clinical_skill(name):
            recognized.append(name)
            groups[name] = "Clinical Skill"
        else:
            unrecognized.append(name)

    total = len(recognized) + len(unrecognized)
    ratio = round(len(recognized) / total, 2) if total else 0.0

    return SkillsValidation(
        total=total,
        recognized_count=len(recognized),
        unrecognized_count=len(unrecognized),
        recognized_ratio=ratio,
        recognized=recognized,
        unrecognized=unrecognized,
        groups=groups,
    )
