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
