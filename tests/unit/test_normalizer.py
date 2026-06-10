"""
Date and credential/specialty normalization tests.

Generic skill aliases (JS → JavaScript, postgres → PostgreSQL, etc.) were
intentionally removed when the parser became healthcare-only. Skill
normalization now uses the healthcare_taxonomy module — see
test_healthcare_normalizer.py for that coverage.
"""

from app.models.schemas import ParsedResumeAI
from app.services.normalization.normalizer import (
    _normalize_date,
    _normalize_skills,
    _strip_name_credentials,
    normalize,
)

# ── Date normalization (MM/DD/YYYY; partial precision preserved) ──────────────
# Never invent a missing day or month — a month/year value stays month/year.

def test_date_iso_full_to_us():
    assert _normalize_date("2024-02-16") == "02/16/2024"


def test_date_iso_month_keeps_precision():
    # Month/year only → MM/YYYY (NOT padded to a fabricated day).
    assert _normalize_date("2023-05") == "05/2023"


def test_date_us_numeric_keeps_day():
    assert _normalize_date("2/16/2024") == "02/16/2024"


def test_date_month_name_with_day():
    assert _normalize_date("February 16, 2024") == "02/16/2024"


def test_date_month_name_no_day():
    assert _normalize_date("January 2023") == "01/2023"


def test_date_month_name_ordinal_day():
    assert _normalize_date("July 21st, 2019") == "07/21/2019"


def test_date_slash_month_year():
    assert _normalize_date("05/2023") == "05/2023"


def test_date_year_only():
    assert _normalize_date("2023") == "2023"


def test_date_present_passthrough():
    assert _normalize_date("Present") == "Present"


def test_date_unparseable_is_none():
    assert _normalize_date("sometime last year") is None


# ── Name credential stripping ─────────────────────────────────────────────────

def test_strip_name_comma_credentials():
    assert _strip_name_credentials("Jane Smith, RN BSN") == "Jane Smith"


def test_strip_name_space_credentials():
    assert _strip_name_credentials("John Doe RN") == "John Doe"


def test_strip_name_keeps_plain_name():
    assert _strip_name_credentials("Maria Garcia") == "Maria Garcia"


def test_strip_name_preserves_last_first():
    # "Last, First" must survive — the tail is not credential-like.
    assert _strip_name_credentials("Smith, Jane") == "Smith, Jane"


# ── Name credentials are PRESERVED, not just stripped ─────────────────────────

def test_name_credentials_recovered_into_credentials_field():
    # Post-nominals stripped from the name must land in personal_info.credentials
    # (regression: "RN, BSN" were dropped entirely).
    parsed = ParsedResumeAI.model_validate(
        {"personal_info": {"full_name": "Stephanie Cinfio, RN, BSN"}}
    )
    normalize(parsed)
    assert parsed.personal_info.full_name == "Stephanie Cinfio"
    assert parsed.personal_info.credentials == ["RN", "BSN"]


def test_name_credentials_merge_with_model_supplied_dedup():
    parsed = ParsedResumeAI.model_validate(
        {"personal_info": {"full_name": "Jane Driscoll MPH BSN RN CCRN",
                           "credentials": ["RN"]}}
    )
    normalize(parsed)
    assert parsed.personal_info.full_name == "Jane Driscoll"
    # Model-supplied first, then recovered ones, case-insensitively de-duped.
    assert parsed.personal_info.credentials == ["RN", "MPH", "BSN", "CCRN"]


def test_plain_name_yields_no_credentials():
    parsed = ParsedResumeAI.model_validate(
        {"personal_info": {"full_name": "Maria Garcia"}}
    )
    normalize(parsed)
    assert parsed.personal_info.credentials == []


# ── Unknown / blank role backfill (travel-assignment sub-entries) ─────────────

def test_unknown_role_backfilled_from_profession():
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "VT Psychiatric Care", "role": "", "profession": "RN"}]}
    )
    normalize(parsed)
    # "" → "Unknown" by the schema validator → backfilled + expanded from profession.
    assert parsed.experience[0].role == "Registered Nurse"


def test_unknown_role_backfilled_from_agency_when_no_profession():
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "Brattleboro", "role": "Unknown",
                         "agency_name": "Supplemental Healthcare"}]}
    )
    normalize(parsed)
    assert parsed.experience[0].role == "Supplemental Healthcare"


def test_known_role_not_overwritten():
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN - ICU", "profession": "RN"}]}
    )
    normalize(parsed)
    assert parsed.experience[0].role == "Registered Nurse - Intensive Care Unit"


# ── Credential casing + cross-bucket hygiene ──────────────────────────────────

def test_lowercased_credentials_restored_to_canonical_case():
    parsed = ParsedResumeAI.model_validate(
        {"personal_info": {"full_name": "Stephanie Cinfio", "credentials": ["rn", "bsn"]}}
    )
    normalize(parsed)
    assert parsed.personal_info.credentials == ["RN", "BSN"]


def test_lowercased_license_type_uppercased():
    parsed = ParsedResumeAI.model_validate(
        {"licenses": [{"name": "rn", "license_type": "rn", "state": "NY"}]}
    )
    normalize(parsed)
    assert parsed.licenses[0].license_type == "RN"
    assert parsed.licenses[0].name == "RN"


def test_practice_credential_cert_promoted_to_license():
    # "LPN" filed under certifications is a professional licence — promote it.
    parsed = ParsedResumeAI.model_validate(
        {"certifications": [{"name": "LPN"}, {"name": "BLS"}]}
    )
    normalize(parsed)
    assert [c.name for c in parsed.certifications] == ["BLS"]
    assert len(parsed.licenses) == 1
    assert parsed.licenses[0].license_type == "LPN"


def test_practice_credential_cert_not_duplicated_when_license_exists():
    parsed = ParsedResumeAI.model_validate(
        {
            "certifications": [{"name": "RN"}],
            "licenses": [{"name": "Registered Nurse License", "license_type": "RN", "state": "NY"}],
        }
    )
    normalize(parsed)
    assert parsed.certifications == []
    assert len(parsed.licenses) == 1          # not duplicated
    assert parsed.licenses[0].state == "NY"   # original licence untouched


def test_certifications_leaked_into_skills_are_removed():
    # Items already captured as certifications must not also appear as skills.
    parsed = ParsedResumeAI.model_validate(
        {
            "skills": ["ICU", "CPR Certification", "BLS Certification"],
            "certifications": [{"name": "CPR"}, {"name": "BLS"}],
        }
    )
    normalize(parsed)
    assert parsed.skills == ["Intensive Care Unit"]
    assert [c.name for c in parsed.certifications] == ["CPR", "BLS"]


def test_known_cert_only_in_skills_is_moved_not_lost():
    parsed = ParsedResumeAI.model_validate(
        {"skills": ["ICU", "PALS", "Driver's License"], "certifications": []}
    )
    normalize(parsed)
    assert parsed.skills == ["Intensive Care Unit"]
    assert [c.name for c in parsed.certifications] == ["PALS", "Driver's License"]


def test_degree_tokens_dropped_from_skills():
    parsed = ParsedResumeAI.model_validate({"skills": ["bsn", "ICU"]})
    normalize(parsed)
    assert parsed.skills == ["Intensive Care Unit"]


# ── Skill dedup (case-insensitive) ────────────────────────────────────────────

def test_skill_deduplication():
    """Same skill in different casing should be deduplicated."""
    skills = _normalize_skills(["Python", "python", "PYTHON"])
    assert len(skills) == 1


def test_unknown_skill_passes_through():
    """Skills not in the healthcare taxonomy are kept as-is."""
    skills = _normalize_skills(["Microsoft Office", "Spanish fluency"])
    assert "Microsoft Office" in skills
    assert "Spanish fluency" in skills


def test_healthcare_specialty_normalizes():
    """Healthcare abbreviations ARE normalized via the taxonomy."""
    skills = _normalize_skills(["ICU", "ER", "ACLS"])
    assert "Intensive Care Unit" in skills
    assert "Emergency Room" in skills
    # ACLS isn't in the abbreviation map — passes through unchanged
    assert "ACLS" in skills
