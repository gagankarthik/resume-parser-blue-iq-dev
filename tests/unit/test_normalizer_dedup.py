"""
Duplicate removal in normalization: repeats collapse into one entry, the kept entry
keeps every value either copy had, and entries that differ on an identifying detail
(dates, licence number) stay separate.
"""

from app.models.schemas import ParsedResumeAI
from app.services.normalization.normalizer import normalize


def test_repeated_role_merged_and_keeps_fields_from_both():
    p = ParsedResumeAI.model_validate({"experience": [
        {"company": "Mercy Hospital", "role": "Staff Nurse", "start_date": "01/2020",
         "end_date": "Present", "description": ["Managed 5 patients", "Charted in Epic"]},
        {"company": "MERCY HOSPITAL", "role": "Staff Nurse", "start_date": "01/2020",
         "end_date": "Present", "charting_system": "Epic",
         "description": ["Charted in Epic.", "Precepted new graduates"]},
    ]})
    normalize(p)
    assert len(p.experience) == 1
    role = p.experience[0]
    assert role.charting_system == "Epic"
    assert role.description == ["Managed 5 patients", "Charted in Epic", "Precepted new graduates"]


def test_same_facility_with_different_dates_stays_separate():
    p = ParsedResumeAI.model_validate({"experience": [
        {"company": "Mercy Hospital", "role": "Travel RN", "start_date": "01/2022", "end_date": "04/2022"},
        {"company": "Mercy Hospital", "role": "Travel RN", "start_date": "06/2023", "end_date": "09/2023"},
    ]})
    normalize(p)
    assert len(p.experience) == 2


def test_duplicate_bullets_within_a_role_removed():
    p = ParsedResumeAI.model_validate({"experience": [
        {"company": "Mercy", "role": "RN", "description": ["Titrated drips", "titrated drips ", "Titrated drips."]},
    ]})
    normalize(p)
    assert p.experience[0].description == ["Titrated drips"]


def test_repeated_education_merged():
    p = ParsedResumeAI.model_validate({"education": [
        {"institution": "State University", "degree": "BSN", "graduation_year": 2019},
        {"institution": "State University", "degree": "BSN", "location": "Albany, NY"},
    ]})
    normalize(p)
    assert len(p.education) == 1
    assert p.education[0].graduation_year == 2019
    assert p.education[0].location == "Albany, NY"


def test_repeated_certification_merged_but_renewals_kept():
    p = ParsedResumeAI.model_validate({"certifications": [
        {"name": "ACLS", "expiry_date": "05/2026"},
        {"name": "acls", "issuer": "American Heart Association"},
        {"name": "BLS", "expiry_date": "01/2024"},
        {"name": "BLS", "expiry_date": "01/2026"},
    ]})
    normalize(p)
    acls = [c for c in p.certifications if c.name.upper() == "ACLS"]
    bls = [c for c in p.certifications if c.name.upper() == "BLS"]
    assert len(acls) == 1 and acls[0].issuer == "American Heart Association"
    assert len(bls) == 2


def test_repeated_license_merged_but_different_numbers_kept():
    p = ParsedResumeAI.model_validate({"licenses": [
        {"name": "Registered Nurse License", "license_type": "RN", "state": "NY", "license_number": "123"},
        {"name": "RN License", "license_type": "RN", "state": "NY", "expiry_date": "12/2026"},
        {"name": "Registered Nurse License", "license_type": "RN", "state": "NY", "license_number": "999"},
    ]})
    normalize(p)
    assert len(p.licenses) == 2
    assert p.licenses[0].expiry_date == "12/2026"


def test_roles_with_placeholder_company_are_never_merged():
    """A blank company becomes "Unknown"; two such roles are not known to be the same
    job, and merging them would drop a role (invariant 4)."""
    p = ParsedResumeAI.model_validate({"experience": [
        {"company": "", "role": "Staff RN", "start_date": "01/2020", "description": ["ICU nights"]},
        {"company": "", "role": "Staff RN", "description": ["Clinic days"]},
    ]})
    normalize(p)
    assert len(p.experience) == 2


def test_dated_and_undated_copy_of_a_named_role_merge():
    """Pins the current rule: a blank date is not a disagreement, so the same named
    role listed once with dates and once without is one role."""
    p = ParsedResumeAI.model_validate({"experience": [
        {"company": "Mercy Hospital", "role": "Staff RN", "start_date": "01/2020", "end_date": "Present"},
        {"company": "Mercy Hospital", "role": "Staff RN", "description": ["Precepted new graduates"]},
    ]})
    normalize(p)
    assert len(p.experience) == 1
    assert p.experience[0].start_date == "01/2020"
    assert p.experience[0].description == ["Precepted new graduates"]


def test_merged_role_does_not_repeat_a_specialty():
    from app.models.schemas import SpecialtyMatch
    from app.services.normalization.normalizer import _merge_into

    kept = ParsedResumeAI.model_validate({"experience": [{"company": "Mercy", "role": "RN"}]}).experience[0]
    dup = kept.model_copy(deep=True)
    kept.specialties = [SpecialtyMatch(name="ICU", raw="ICU", specialty_id="56", matched=True, confidence=1.0)]
    dup.specialties = [SpecialtyMatch(name="ICU", raw="Intensive Care Unit", specialty_id="56", matched=True, confidence=0.95)]
    _merge_into(kept, dup)
    assert [s.specialty_id for s in kept.specialties] == ["56"]


def test_symbols_that_carry_meaning_are_not_collapsed():
    from app.services.normalization.normalizer import _dedup_text

    assert _dedup_text(["C++", "C#", "c++"]) == ["C++", "C#"]
    assert _dedup_text(["+++", "+++"]) == ["+++"]


def test_repeated_text_lists_deduplicated():
    p = ParsedResumeAI.model_validate({
        "skills": ["Epic", "EPIC", "Wound care"],
        "languages": ["Spanish", "spanish"],
        "awards": ["DAISY Award 2023", "DAISY Award 2023."],
        "professional_associations": ["ANA Member", "ANA member"],
    })
    normalize(p)
    assert p.skills.count("Epic") + p.skills.count("EPIC") == 1
    assert p.languages == ["Spanish"]
    assert p.awards == ["DAISY Award 2023"]
    assert p.professional_associations == ["ANA Member"]
