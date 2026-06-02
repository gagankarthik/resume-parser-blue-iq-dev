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
