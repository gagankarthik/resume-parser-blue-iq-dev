"""
Tests for skills validation against the healthcare taxonomy.
Covers recognized specialties/professions/certifications, unrecognized
free-form skills, group mapping, dedup, and ratio computation.
"""

from app.models.schemas import ParsedResumeAI
from app.services.normalization.skills_validator import validate_skills


def _validate(skills: list[str]):
    return validate_skills(ParsedResumeAI(skills=skills))


# ── Recognized canonical specialties ──────────────────────────────────────────

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


# ── Certifications ────────────────────────────────────────────────────────────

def test_certification_recognized():
    result = _validate(["ACLS"])
    assert result.recognized == ["ACLS"]
    assert result.unrecognized == []


def test_certification_case_insensitive():
    result = _validate(["bls"])
    assert result.recognized == ["bls"]


# ── Unrecognized free-form ────────────────────────────────────────────────────

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


# ── Group mapping ─────────────────────────────────────────────────────────────

def test_group_mapping_populated():
    result = _validate(["Intensive Care Unit", "Neonatal Intensive Care Unit"])
    assert result.groups["Intensive Care Unit"] == "ICU"
    assert result.groups["Neonatal Intensive Care Unit"] == "Nursery"


def test_unrecognized_not_in_groups():
    result = _validate(["Patient Advocacy"])
    assert result.groups == {}


# ── Dedup & edge cases ────────────────────────────────────────────────────────

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
