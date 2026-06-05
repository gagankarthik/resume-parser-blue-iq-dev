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
        if canonical is not None:
            resolved = canonical
        elif key in KNOWN_CERTIFICATIONS:
            resolved = name
        else:
            resolved = name

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
