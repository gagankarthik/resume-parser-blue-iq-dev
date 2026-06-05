"""
Schema-level tests for ParsedResumeAI list coercion and the awards/publications
fields. Validators must SANITIZE (never raise) so malformed LLM output cannot
crash the pipeline.
"""

from app.models.schemas import ParsedResumeAI


def test_awards_and_publications_extracted():
    parsed = ParsedResumeAI.model_validate(
        {
            "skills": ["ICU"],
            "awards": ["DAISY Award (2023)", "Employee of the Year (2021)"],
            "publications": ["Smith J. (2022). Reducing CLABSI rates. J Nursing Care."],
        }
    )
    assert parsed.awards == ["DAISY Award (2023)", "Employee of the Year (2021)"]
    assert len(parsed.publications) == 1


def test_awards_default_empty():
    parsed = ParsedResumeAI.model_validate({"skills": []})
    assert parsed.awards == []
    assert parsed.publications == []


def test_awards_null_coerced_to_list():
    # LLM may emit null instead of [] — must coerce, not crash.
    parsed = ParsedResumeAI.model_validate({"awards": None, "publications": None})
    assert parsed.awards == []
    assert parsed.publications == []


def test_awards_strips_blanks_and_non_strings():
    parsed = ParsedResumeAI.model_validate(
        {"awards": ["  Honor  ", "", None, 123, "  "]}
    )
    assert parsed.awards == ["Honor", "123"]


def test_references_default_empty():
    parsed = ParsedResumeAI.model_validate({"skills": []})
    assert parsed.references == []


# ── Work History form fields on ExperienceItem ───────────────────────────────

def test_experience_work_history_fields_round_trip():
    parsed = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "Fort Sanders Regional Medical Center",
                    "role": "RN - Med Surg/Tele",
                    "start_date": "2026-01-01",
                    "end_date": "2026-04-30",
                    "city": "Knoxville",
                    "state": "Tennessee",
                    "country": "United States",
                    "zip_code": "37916",
                    "profession": "RN",
                    "specialties": ["Med Surg/ Tele"],
                    "charting_system": "Epic",
                    "shift": "Nights",
                }
            ]
        }
    )
    exp = parsed.experience[0]
    assert exp.city == "Knoxville"
    assert exp.zip_code == "37916"
    assert exp.profession == "RN"
    assert exp.specialties == ["Med Surg/ Tele"]  # canonicalized later by the normalizer
    assert exp.charting_system == "Epic"


def test_experience_new_fields_default_null():
    exp = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "Y"}]}
    ).experience[0]
    assert exp.city is None
    assert exp.profession is None
    assert exp.specialties == []
    assert exp.teaching_facility is None


def test_experience_yes_no_na_coercion():
    exp = ParsedResumeAI.model_validate(
        {
            "experience": [
                {
                    "company": "X",
                    "role": "Y",
                    "teaching_facility": "yes",
                    "magnet_facility": "N/A",
                    "trauma_facility": "maybe",  # not yes/no/na → null
                }
            ]
        }
    ).experience[0]
    assert exp.teaching_facility == "Yes"
    assert exp.magnet_facility == "N/A"
    assert exp.trauma_facility is None


def test_experience_specialties_canonicalized_by_normalizer():
    from app.services.normalization.normalizer import normalize

    parsed = ParsedResumeAI.model_validate(
        {"experience": [{"company": "X", "role": "RN", "specialties": ["Med Surg/ Tele", "ICU"]}]}
    )
    normalize(parsed)
    assert parsed.experience[0].specialties == ["Med Surg / Tele", "Intensive Care Unit"]
