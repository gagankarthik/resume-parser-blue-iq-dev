"""
Tests for skills validation against the healthcare taxonomy.
Covers recognized specialties/professions/certifications, unrecognized
free-form skills, group mapping, dedup, and ratio computation.
"""

from app.models.schemas import ParsedResumeAI
from app.services.normalization.skills_validator import validate_skills


def _validate(skills: list[str]):
    return validate_skills(ParsedResumeAI(skills=skills))


# -- Recognized canonical specialties ------------------------------------------

def test_canonical_specialty_recognized():
    result = _validate(["Intensive Care Unit"])
    assert result.recognized == ["Intensive Care Unit"]
    assert result.unrecognized == []
    assert result.recognized_ratio == 1.0


def test_specialty_abbreviation_recognized():
    # Skills that were never normalized still validate via the abbreviation map.
    result = _validate(["ICU"])
    assert result.recognized == ["Intensive Care Unit"]
    assert result.recognized_count == 1


def test_profession_full_name_recognized():
    result = _validate(["Registered Nurse"])
    assert result.recognized == ["Registered Nurse"]


def test_profession_abbreviation_recognized():
    result = _validate(["RN"])
    assert result.recognized == ["Registered Nurse"]


# -- Certifications ------------------------------------------------------------

def test_certification_recognized():
    result = _validate(["ACLS"])
    assert result.recognized == ["ACLS"]
    assert result.unrecognized == []


def test_certification_case_insensitive():
    result = _validate(["bls"])
    assert result.recognized == ["bls"]


# -- Unrecognized free-form ----------------------------------------------------

def test_unrecognized_freeform_skill():
    result = _validate(["Patient Advocacy"])
    assert result.unrecognized == ["Patient Advocacy"]
    assert result.recognized == []
    assert result.recognized_ratio == 0.0


def test_mixed_recognized_and_unrecognized():
    result = _validate(["Intensive Care Unit", "Patient Advocacy"])
    assert result.total == 2
    assert result.recognized_count == 1
    assert result.unrecognized_count == 1
    assert result.recognized_ratio == 0.5


# -- Free-form clinical skills recognized by clinical-term containment ----------

def test_clinical_skill_phrases_recognized():
    """Real nursing skills read like phrases, not bare specialties - they must
    still be recognized so validation isn't a misleading 0%."""
    result = _validate([
        "Neonatal health monitoring", "EKG Rhythms", "IV/PICC",
        "NICU medical terminology & equipment", "Telemetry monitoring",
    ])
    assert result.recognized_count == 5
    assert result.unrecognized == []
    assert result.recognized_ratio == 1.0
    assert result.groups["EKG Rhythms"] == "Clinical Skill"


def test_non_clinical_skill_stays_unrecognized():
    # Generic/administrative skills must NOT be mislabeled as clinical.
    result = _validate(["Time Management", "Microsoft Excel", "Driving"])
    assert result.recognized == []
    assert result.unrecognized_count == 3


def test_clinical_term_requires_whole_word():
    # "derivative" contains "iv" but not as a whole word -> not clinical.
    result = _validate(["Derivative analysis"])
    assert result.unrecognized == ["Derivative analysis"]


# -- Group mapping -------------------------------------------------------------

def test_group_mapping_populated():
    result = _validate(["Intensive Care Unit", "Neonatal Intensive Care Unit"])
    assert result.groups["Intensive Care Unit"] == "ICU"
    assert result.groups["Neonatal Intensive Care Unit"] == "Nursery"


def test_unrecognized_not_in_groups():
    result = _validate(["Patient Advocacy"])
    assert result.groups == {}


# -- Dedup & edge cases --------------------------------------------------------

def test_dedup_case_insensitive():
    result = _validate(["ICU", "icu", "Intensive Care Unit"])
    assert result.total == 1
    assert result.recognized == ["Intensive Care Unit"]


def test_blank_skills_ignored():
    result = _validate(["", "   ", "ICU"])
    assert result.total == 1


def test_empty_skills():
    result = _validate([])
    assert result.total == 0
    assert result.recognized_ratio == 0.0
    assert result.recognized == []
    assert result.unrecognized == []


# -- Spreadsheet coverage (2.11.26 updates) ------------------------------------

def test_punctuation_variant_recognized():
    # Spreadsheet uses "Med Surg/ Tele"; taxonomy stores "Med Surg / Tele".
    result = _validate(["Med Surg/ Tele"])
    assert result.recognized == ["Med Surg / Tele"]
    assert result.groups["Med Surg / Tele"] == "Med Surg / Tele"


def test_hyphen_for_endash_recognized():
    # Resume hyphen should match the en-dash canonical.
    result = _validate(["Ultrasound Tech - General"])
    assert result.recognized == ["Ultrasound Tech – General"]
    assert result.groups["Ultrasound Tech – General"] == "Imaging Tech"


def test_credential_prefix_setting_recognized():
    result = _validate(["OT - Acute Care"])
    assert result.recognized == ["Occupational Therapist – Acute Care"]
    assert result.groups["Occupational Therapist – Acute Care"] == "OT"


def test_pediatric_specialty_grouped_pediatrics():
    result = _validate(["Pediatric Med Surg", "PICU"])
    assert result.groups["Pediatric Med Surg"] == "Pediatrics"
    assert result.groups["Pediatric Intensive Care Unit"] == "Pediatrics"


def test_sterile_processing_spt_recognized():
    result = _validate(["Sterile Processing Tech (SPT)"])
    assert result.recognized == ["Sterile Processing Technician"]
    assert result.groups["Sterile Processing Technician"] == "Sterile Processing"


def test_paren_social_worker_recognized():
    result = _validate(["LCSW (Licensed Clinical Social Worker)"])
    assert result.recognized == ["Licensed Clinical Social Worker"]
    assert result.groups["Licensed Clinical Social Worker"] == "Social Worker"


def test_dietary_group_populated():
    result = _validate(["Dietician"])
    assert result.groups["Dietician"] == "Dietary"


# -- Common shorthand / synonym variants seen on real resumes ------------------

def test_ampersand_matches_and():
    # "Labor & Delivery" must resolve like "Labor and Delivery".
    result = _validate(["Labor & Delivery"])
    assert result.recognized == ["Labor and Delivery"]
    assert result.groups["Labor and Delivery"] == "Labor and Delivery"


def test_l_and_d_abbreviation_recognized():
    result = _validate(["L&D"])
    assert result.recognized == ["Labor and Delivery"]


def test_critical_care_shorthand_recognized():
    result = _validate(["Critical Care"])
    assert result.recognized == ["Critical Care Unit"]
    assert result.groups["Critical Care Unit"] == "ICU"


def test_psych_synonym_recognized():
    for term in ("Psych", "Psychiatric"):
        result = _validate([term])
        assert result.recognized == ["Behavioral Health"]


def test_step_down_spacing_variant_recognized():
    result = _validate(["Step Down"])
    assert result.recognized == ["Stepdown"]
