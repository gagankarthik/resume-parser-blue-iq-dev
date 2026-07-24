"""Map corrected feedback paths to the agent that produced them."""

from app.services.refinement.field_map import (
    CREDENTIALS,
    EDUCATION,
    PERSONAL,
    SUPPLEMENTAL,
    WORK,
    agent_for_path,
)


def test_personal_info_paths():
    assert agent_for_path("personal_info.full_name") == PERSONAL
    assert agent_for_path("personal_info.email") == PERSONAL


def test_experience_paths_indexed():
    assert agent_for_path("experience[0].role") == WORK
    assert agent_for_path("experience[3].description[2]") == WORK
    assert agent_for_path("experience[1].specialties[0].name") == WORK


def test_education_paths():
    assert agent_for_path("education[0].degree") == EDUCATION


def test_credentials_group_shares_one_agent():
    assert agent_for_path("skills[2]") == CREDENTIALS
    assert agent_for_path("certifications[0].name") == CREDENTIALS
    assert agent_for_path("licenses[0].state") == CREDENTIALS
    assert agent_for_path("professional_associations[1]") == CREDENTIALS


def test_supplemental_paths():
    assert agent_for_path("projects[0].name") == SUPPLEMENTAL
    assert agent_for_path("languages[0]") == SUPPLEMENTAL
    assert agent_for_path("awards[0]") == SUPPLEMENTAL


def test_unmapped_paths_return_none():
    # Derived / matcher-owned / unknown roots are not an agent's prompt signal.
    assert agent_for_path("confidence.overall") is None
    assert agent_for_path("compliance.flags[0]") is None
    assert agent_for_path("") is None
