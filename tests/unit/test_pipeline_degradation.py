"""
Graceful-degradation tests for the parsing pipeline.

A failure in the AI parse step must NOT produce a silent total failure (no JSON).
Instead the pipeline returns a PARTIAL record built from rule-based contact
anchors, flagged with `partial=True` and a human-readable warning, so the caller
always receives structured JSON to review.
"""

import pytest

from app.core.exceptions import AIParsingError
from app.services import pipeline
from app.services.parsing.rule_parser import RuleExtracted


def test_fallback_from_anchors_populates_contact_only():
    anchors = RuleExtracted(
        emails=["jane@example.com"],
        phones=["(555) 234-5678"],
        linkedin_urls=["https://linkedin.com/in/jane"],
    )
    parsed = pipeline._fallback_from_anchors(anchors)

    assert parsed.personal_info.email == "jane@example.com"
    assert parsed.personal_info.phone == "(555) 234-5678"
    assert parsed.personal_info.linkedin_url == "https://linkedin.com/in/jane"
    # Nothing is invented for the sections the AI would have filled.
    assert parsed.experience == []
    assert parsed.education == []
    assert parsed.skills == []


def test_fallback_from_empty_anchors_is_valid_empty_record():
    parsed = pipeline._fallback_from_anchors(RuleExtracted())
    assert parsed.personal_info.email is None
    assert parsed.experience == []


# Long enough to clear the multi_agent_min_chars gate so the orchestrator path runs.
_LONG_TEXT = "Katherine Driscoll\njane@example.com\n(555) 234-5678\n" + ("Experience bullet line. " * 200)


def _stub_extraction(monkeypatch, text=_LONG_TEXT):
    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: (pipeline.ExtractionStrategy.DOCX, False),
    )
    monkeypatch.setattr(pipeline.docx_extractor, "extract", lambda content: text)


@pytest.mark.asyncio
async def test_pipeline_degrades_to_partial_on_ai_failure(monkeypatch):
    """When BOTH the orchestrator and single-shot parse fail, run() returns a
    flagged partial result, not an error."""

    async def _boom_orch(_text, _anchors, budget=None):
        raise AIParsingError("orchestrator empty")

    async def _boom(_sections, _anchors):
        raise AIParsingError("Model returned empty structured output")

    monkeypatch.setattr(pipeline.orchestrator, "parse", _boom_orch)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _boom)
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(
            job_id="j1", filename="resume.docx", content=b"x", company_id="c1"
        )
    )

    assert result.partial is True
    assert result.warnings and "human review" in result.warnings[0]
    assert result.parsed.personal_info.email == "jane@example.com"
    assert result.confidence.overall < 0.5


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_single_shot_when_orchestrator_fails(monkeypatch):
    """Orchestrator failure must transparently fall back to the single-shot parser
    (a clean, non-partial result) — not degrade to anchors-only."""
    from app.models.schemas import ParsedResumeAI, PersonalInfo

    async def _boom_orch(_text, _anchors, budget=None):
        raise AIParsingError("orchestrator down")

    async def _single_shot(_sections, _anchors):
        return ParsedResumeAI(personal_info=PersonalInfo(full_name="Jane Smith")), 1234

    monkeypatch.setattr(pipeline.orchestrator, "parse", _boom_orch)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(job_id="j2", filename="r.docx", content=b"x", company_id="c1")
    )

    assert result.partial is False
    assert result.parsed.personal_info.full_name == "Jane Smith"
    assert result.ai_tokens_used == 1234


@pytest.mark.asyncio
async def test_short_resume_skips_orchestrator_for_speed(monkeypatch):
    """The complexity gate must keep short résumés on the fast single-shot path."""
    from app.models.schemas import ParsedResumeAI, PersonalInfo

    async def _orch(_text, _anchors, budget=None):  # must NOT run for a short résumé
        raise AssertionError("orchestrator must not run below multi_agent_min_chars")

    async def _single_shot(_sections, _anchors):
        return ParsedResumeAI(personal_info=PersonalInfo(full_name="Jane Smith")), 42

    monkeypatch.setattr(pipeline.orchestrator, "parse", _orch)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    _stub_extraction(monkeypatch, text="Jane Smith\njane@example.com\n(555) 234-5678\nRN")

    result = await pipeline.run(
        pipeline.PipelineInput(job_id="j4", filename="r.docx", content=b"x", company_id="c1")
    )
    assert result.parsed.personal_info.full_name == "Jane Smith"
    assert result.ai_tokens_used == 42


# ── Surname/email mismatch review flag ───────────────────────────────────────

def _parsed_with(name, email):
    from app.models.schemas import ParsedResumeAI, PersonalInfo
    return ParsedResumeAI(personal_info=PersonalInfo(full_name=name, email=email))


def test_surname_mismatch_flagged_for_hyphenated_email():
    # "Ricafort-Moulds" truncated to "Ricafort" in the body, but the email keeps it.
    w = pipeline._surname_mismatch_warning(
        _parsed_with("Rubie Ricafort", "rubie.ricafortmoulds@example.com")
    )
    assert w is not None and "surname" in w


def test_surname_match_not_flagged():
    assert pipeline._surname_mismatch_warning(
        _parsed_with("Jane Smith", "jane.smith@example.com")
    ) is None


def test_surname_with_trailing_digits_not_flagged():
    assert pipeline._surname_mismatch_warning(
        _parsed_with("Jane Smith", "jane.smith1985@example.com")
    ) is None


def test_short_credential_suffix_not_flagged():
    # "...smithrn" — a 2-letter credential tail must not trip the flag.
    assert pipeline._surname_mismatch_warning(
        _parsed_with("Jane Smith", "janesmithrn@example.com")
    ) is None


def test_firstname_after_surname_not_flagged():
    # "Last, First" style local part: the residue is the first name, not a surname.
    assert pipeline._surname_mismatch_warning(
        _parsed_with("Jane Smith", "smithjane@example.com")
    ) is None


@pytest.mark.asyncio
async def test_pipeline_uses_orchestrator_when_enabled(monkeypatch):
    """Happy path: orchestrator result is used and its warnings propagate."""
    from app.models.schemas import ParsedResumeAI, PersonalInfo

    async def _orch(_text, _anchors, budget=None):
        return (
            ParsedResumeAI(personal_info=PersonalInfo(full_name="Jane Smith")),
            999,
            ["Role 'X': extracted 2 of 3 expected duty bullets — review."],
        )

    async def _single_shot(_sections, _anchors):  # should NOT be called
        raise AssertionError("single-shot parser must not run when orchestrator succeeds")

    monkeypatch.setattr(pipeline.orchestrator, "parse", _orch)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(job_id="j3", filename="r.docx", content=b"x", company_id="c1")
    )

    assert result.partial is False
    assert result.ai_tokens_used == 999
    assert result.warnings and "review" in result.warnings[0]
