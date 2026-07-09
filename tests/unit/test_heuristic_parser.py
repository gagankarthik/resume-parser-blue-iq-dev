"""
Tests for the deterministic (no-LLM) heuristic parser — the fallback "floor".

It must recover real structure (experience/education/skills) without inventing
data, so a degraded parse carries more than contact anchors.
"""

from app.services.parsing import heuristic_parser
from app.services.parsing.rule_parser import RuleExtracted

_RESUME = """Ayad Muflahi
ayad@example.com
313-283-5671

Professional Summary
Registered technologist with 10 years of experience.

Experience
MRI/CT Technologist
DMC, Detroit MI
November 2021 - November 2024
- Operated Siemens and GE scanners
- Worked in ER and NICU
CT Technologist per diem
Garden City Hospital, MI
June 2024 - August 2024
- Operated CT scanners

Education
Bachelor of Science in Information Systems
Wayne State University
2011

Skills
MRI, CT, X-Ray, Siemens, EPIC

Certifications
ARRT
BLS
"""


def _parse(text, emails=None, phones=None):
    return heuristic_parser.parse(
        text, RuleExtracted(emails=emails or [], phones=phones or [])
    )


def test_extracts_name_experience_education_skills():
    p = _parse(_RESUME, emails=["ayad@example.com"], phones=["313-283-5671"])
    assert p.personal_info.full_name == "Ayad Muflahi"
    assert p.personal_info.email == "ayad@example.com"
    # Two dated roles recovered.
    assert len(p.experience) == 2
    roles = {e.role for e in p.experience}
    assert "MRI/CT Technologist" in roles
    assert p.experience[0].start_date == "11/2021"
    assert p.experience[0].end_date == "11/2024"
    # Education, skills, certs.
    assert any("Bachelor" in (e.degree or "") for e in p.education)
    assert any(e.graduation_year == 2011 for e in p.education)
    assert "MRI" in p.skills and "EPIC" in p.skills
    assert {c.name for c in p.certifications} >= {"ARRT", "BLS"}


def test_current_role_sets_present_and_is_current():
    text = "Experience\nStaff RN\nMercy Hospital\nJan 2020 - Present\n- Charge nurse\n"
    p = _parse(text)
    assert p.experience[0].end_date == "Present"
    assert p.experience[0].is_current is True


def test_never_invents_experience_without_dates():
    # Prose with no date ranges must not become fabricated job entries.
    text = "Experience\nResponsible for patient care and safety across multiple units.\n"
    p = _parse(text)
    assert p.experience == []


def test_empty_text_yields_empty_but_valid_record():
    p = _parse("")
    assert p.experience == []
    assert p.education == []
    assert p.skills == []
    assert p.personal_info.full_name is None


def test_contact_anchors_populate_personal_info():
    p = _parse("Some Person\n", emails=["a@b.com"], phones=["555-1234"])
    assert p.personal_info.email == "a@b.com"
    assert p.personal_info.phone == "555-1234"
