from app.models.schemas import ExperienceItem, ParsedResumeAI, PersonalInfo
from app.services.scoring.confidence_scorer import score


def test_full_personal_info_scores_high():
    parsed = ParsedResumeAI(
        personal_info=PersonalInfo(
            full_name="John Doe",
            email="john@example.com",
            phone="+1 555 123 4567",
            location="New York",
            linkedin_url="linkedin.com/in/johndoe",
        )
    )
    scores = score(parsed)
    assert scores.personal_info >= 0.8


def test_empty_resume_scores_low():
    parsed = ParsedResumeAI()
    scores = score(parsed)
    assert scores.overall < 0.3


def test_overall_is_weighted_average():
    parsed = ParsedResumeAI(
        personal_info=PersonalInfo(full_name="Jane Smith", email="jane@example.com"),
        experience=[
            ExperienceItem(company="Acme", role="Engineer", start_date="2020-01")
        ],
        skills=["Python", "FastAPI", "AWS", "Docker", "Kubernetes"],
    )
    scores = score(parsed)
    assert 0.0 <= scores.overall <= 1.0


def test_catalog_mapping_averages_entity_confidences():
    # A role whose entities all resolved to platform ids at high confidence yields a
    # high catalog_mapping; unmatched entities (0.0) pull it down.
    exp = ExperienceItem(
        company="Mercy Hospital", role="RN", profession="RN", state="NY", country="United States",
    )
    exp.profession_id, exp.profession_confidence = "1", 1.0
    exp.facility_id, exp.facility_confidence = "500", 1.0
    exp.country_id, exp.country_confidence = "1", 1.0
    exp.state_id, exp.state_confidence = "35", 1.0
    parsed = ParsedResumeAI(experience=[exp])
    assert score(parsed).catalog_mapping == 1.0

    exp2 = ExperienceItem(company="Unknown Clinic", role="RN", profession="RN", state="ZZ")
    # nothing resolved -> confidences stay 0.0
    parsed2 = ParsedResumeAI(experience=[exp2])
    assert score(parsed2).catalog_mapping == 0.0


def test_catalog_mapping_zero_when_no_experience():
    assert score(ParsedResumeAI()).catalog_mapping == 0.0
