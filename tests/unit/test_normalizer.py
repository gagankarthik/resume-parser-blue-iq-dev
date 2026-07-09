"""
Date and credential/specialty normalization tests.

Generic skill aliases (JS → JavaScript, postgres → PostgreSQL, etc.) were
intentionally removed when the parser became healthcare-only. Skill
normalization now uses the healthcare_taxonomy module — see
test_healthcare_normalizer.py for that coverage.
"""

from app.models.schemas import ExperienceItem, ExtractionNote, ParsedResumeAI
from app.services.normalization.normalizer import (
    _normalize_date,
    _normalize_skills,
    _refine_location_to_street,
    _strip_name_credentials,
    normalize,
)


# ── Extraction-note confidence normalization ─────────────────────────────────
def test_extraction_note_zero_confidence_null_decision_becomes_high():
    parsed = ParsedResumeAI(extraction_notes=[
        ExtractionNote(field="experience[0].facility_beds", value=None,
                       confidence=0.0, reason="only in summary; 3 facilities"),
    ])
    normalize(parsed)
    # A confident decision to leave a field null must not read as 0.
    assert parsed.extraction_notes[0].confidence == 0.9


def test_extraction_note_zero_confidence_assigned_value_becomes_moderate():
    parsed = ParsedResumeAI(extraction_notes=[
        ExtractionNote(field="experience[0].facility_beds", value="64",
                       confidence=0.0, reason="attached from role block"),
    ])
    normalize(parsed)
    assert parsed.extraction_notes[0].confidence == 0.7


def test_extraction_note_model_confidence_respected():
    parsed = ParsedResumeAI(extraction_notes=[
        ExtractionNote(field="x", value=None, confidence=0.45, reason="judgment call"),
    ])
    normalize(parsed)
    assert parsed.extraction_notes[0].confidence == 0.45


# ── Location: reduce a full address to the street line ───────────────────────
def _loc(location, **kw):
    e = ExperienceItem(company="X", role="Y", location=location, **kw)
    _refine_location_to_street(e)
    return e


def test_location_reduced_to_street_and_parts_backfilled():
    e = _loc("818 Ellicott Street, Buffalo, NY 14203")
    assert e.location == "818 Ellicott Street"
    assert e.city == "Buffalo" and e.state == "NY" and e.zip_code == "14203"


def test_location_keeps_suite_on_street_not_city():
    # Missing comma glues the suite to the city in the source line.
    e = _loc("705 Maple Road, Suite 300 Williamsville, NY 14221")
    assert e.location == "705 Maple Road, Suite 300"
    assert e.city == "Williamsville" and e.zip_code == "14221"


def test_location_multiword_city_preserved():
    e = _loc("500 J Clyde Morris Blvd, Newport News, VA 23601")
    assert e.location == "500 J Clyde Morris Blvd"
    assert e.city == "Newport News" and e.state == "VA"


def test_location_city_state_only_has_no_street():
    e = _loc("Houston, TX")
    assert e.location is None
    assert e.city == "Houston" and e.state == "TX"


def test_location_never_overrides_extracted_parts():
    e = _loc("818 Ellicott Street, Buffalo, NY 14203", city="Buffalo Heights")
    assert e.location == "818 Ellicott Street"
    assert e.city == "Buffalo Heights"  # extracted value wins


def test_location_international_left_intact():
    # No US state/ZIP tail → we cannot split safely, so leave it as-is.
    e = _loc("135 Brush Hill Road, London, UK")
    assert e.location == "135 Brush Hill Road, London, UK"


def test_location_bare_street_untouched():
    e = _loc("818 Ellicott Street")
    assert e.location == "818 Ellicott Street"

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


# ── Trauma facility inferred from a stated trauma level ───────────────────────

def test_trauma_facility_backfilled_from_level():
    # Source marks "Level 1 Trauma" but the model left trauma_facility null —
    # a stated level means it IS a trauma facility.
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "Albany Medical Center", "role": "RN",
                         "trauma_level": "Level 1 Trauma"}]}
    )
    normalize(parsed)
    assert parsed.experience[0].trauma_facility == "Yes"


def test_trauma_facility_explicit_no_not_overridden():
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN",
                         "trauma_level": "Level II", "trauma_facility": "No"}]}
    )
    normalize(parsed)
    assert parsed.experience[0].trauma_facility == "No"


def test_trauma_facility_stays_null_without_level():
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN"}]}
    )
    normalize(parsed)
    assert parsed.experience[0].trauma_facility is None


# ── Description bullets: intra-bullet PDF line-wrap is collapsed ───────────────

def test_description_embedded_newline_collapsed():
    # A PDF line-wrap inside one bullet must become a single space, not a
    # literal newline left in the array item.
    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN",
                         "description": ["Charted vitals ensuring\naccurate documentation"]}]}
    )
    normalize(parsed)
    assert parsed.experience[0].description == ["Charted vitals ensuring accurate documentation"]


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


# ── Education repair: orphaned degrees reattach to their institution ──────────

def test_education_reattaches_orphaned_degrees_and_drops_header_stub():
    """One school header + multiple degree lines → each degree keeps the school;
    the degree-less header stub is dropped (no 'Unknown Institution')."""
    parsed = ParsedResumeAI.model_validate({
        "education": [
            {"institution": "ECPI University", "degree": None, "field_of_study": "Nursing"},
            {"institution": "", "degree": "Associates in Nursing", "graduation_year": 2018},
            {"institution": "", "degree": "Bachelor of Science in Nursing", "graduation_year": 2019},
        ]
    })
    normalize(parsed)
    assert [(e.institution, e.degree, e.graduation_year) for e in parsed.education] == [
        ("ECPI University", "Associate Degree in Nursing", 2018),  # grammar canonicalized
        ("ECPI University", "Bachelor of Science in Nursing", 2019),
    ]


def test_education_keeps_standalone_institution_without_siblings():
    """A lone school header with no degree is NOT dropped when nothing reattaches."""
    parsed = ParsedResumeAI.model_validate({
        "education": [{"institution": "State University", "field_of_study": "Nursing"}]
    })
    normalize(parsed)
    assert len(parsed.education) == 1
    assert parsed.education[0].institution == "State University"


# ── Country inference: unambiguous US signal only ────────────────────────────

def test_country_inferred_from_us_state_abbrev():
    parsed = ParsedResumeAI.model_validate({
        "experience": [{"company": "Oishei", "role": "RN", "state": "NY",
                        "location": "818 Ellicott Street, Buffalo, NY 14203"}]
    })
    normalize(parsed)
    assert parsed.experience[0].country == "United States"


def test_country_inferred_from_location_state_zip_when_state_field_blank():
    parsed = ParsedResumeAI.model_validate({
        "experience": [{"company": "Riverside", "role": "RN",
                        "location": "500 J Clyde Morris Blvd, Newport News, VA 23601"}]
    })
    normalize(parsed)
    assert parsed.experience[0].country == "United States"


def test_country_not_overridden_when_stated():
    parsed = ParsedResumeAI.model_validate({
        "experience": [{"company": "NHS Trust", "role": "RN", "state": "London",
                        "country": "United Kingdom"}]
    })
    normalize(parsed)
    assert parsed.experience[0].country == "United Kingdom"


def test_country_not_inferred_without_us_signal():
    parsed = ParsedResumeAI.model_validate({
        "experience": [{"company": "Clinic", "role": "RN", "city": "Toronto"}]
    })
    normalize(parsed)
    assert parsed.experience[0].country is None


# ── Profession id mapping on experience ──────────────────────────────────────

def test_profession_id_and_confidence_mapped(tmp_path):
    import json

    from app.services.normalization import specialty_catalog
    path = tmp_path / "cat.json"
    path.write_text(json.dumps([
        {"id": "82", "specialty": "NICU", "profession": "RN", "profession_id": "1"},
    ]), encoding="utf-8")
    specialty_catalog.reload(str(path))
    try:
        parsed = ParsedResumeAI.model_validate({
            "experience": [{"company": "X", "role": "RN", "profession": "RN"}]
        })
        normalize(parsed)
        exp = parsed.experience[0]
        assert exp.profession_id == "1"
        assert exp.profession_confidence == 1.0
        # Facility mapping is reserved (awaiting the client dataset) → null / 0.0.
        assert exp.facility_id is None and exp.facility_confidence == 0.0
    finally:
        specialty_catalog.reload("")


def test_profession_id_null_when_unknown(tmp_path):
    import json

    from app.services.normalization import specialty_catalog
    path = tmp_path / "cat.json"
    path.write_text(json.dumps([
        {"id": "82", "specialty": "NICU", "profession": "RN", "profession_id": "1"},
    ]), encoding="utf-8")
    specialty_catalog.reload(str(path))
    try:
        parsed = ParsedResumeAI.model_validate({
            "experience": [{"company": "X", "role": "Chef", "profession": "Chef"}]
        })
        normalize(parsed)
        assert parsed.experience[0].profession_id is None
        assert parsed.experience[0].profession_confidence == 0.0
    finally:
        specialty_catalog.reload("")
