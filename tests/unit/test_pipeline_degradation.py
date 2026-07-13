"""
Graceful-degradation tests for the parsing pipeline.

A failure in the AI parse step must NOT produce a silent total failure (no JSON).
Instead the pipeline returns a PARTIAL record built from rule-based contact
anchors, flagged with `partial=True` and a human-readable warning, so the caller
always receives structured JSON to review.
"""

import pytest

from app.core.exceptions import AIParsingError, ExtractionError
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


# ── Sync path: single-shot primary, section-only enrich on timeout ─────────────

@pytest.mark.asyncio
async def test_sync_uses_single_shot_primary(monkeypatch):
    """A SYNC request uses the fast single-shot parser as primary and must NOT run
    the full multi-agent orchestrator (whose per-role work stage gets cancelled on
    the tight sync budget and drops all experience)."""
    from app.models.schemas import ExperienceItem, ParsedResumeAI, PersonalInfo

    async def _orch(_text, _anchors, budget=None):  # must NOT run on sync
        raise AssertionError("full orchestrator must not run on the sync path")

    async def _single_shot(_sections, _anchors):
        return (
            ParsedResumeAI(
                personal_info=PersonalInfo(full_name="Sync User"),
                experience=[ExperienceItem(company="Acme", role="RN")],
            ),
            321,
        )

    monkeypatch.setattr(pipeline.orchestrator, "parse", _orch)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(
            job_id="s1", filename="r.docx", content=b"x", company_id="c1", sync=True
        )
    )
    assert result.partial is False
    assert result.parsed.personal_info.full_name == "Sync User"
    assert len(result.parsed.experience) == 1
    assert result.ai_tokens_used == 321


@pytest.mark.asyncio
async def test_sync_enrich_backfills_experience_on_timeout(monkeypatch):
    """When the sync single-shot times out, the section-only enrich pass supplies
    the semantic sections and the deterministic floor supplies work history — so
    experience is NEVER silently dropped. Flagged partial for review."""
    from app.models.schemas import EducationItem, ParsedResumeAI, PersonalInfo

    async def _boom_single_shot(_sections, _anchors):
        raise TimeoutError("single-shot timed out")

    async def _light(_text, _anchors, budget):
        # Semantic sections only — no experience (that comes from the floor).
        return (
            ParsedResumeAI(
                personal_info=PersonalInfo(full_name="Jane RN", headline="Registered Nurse"),
                education=[EducationItem(institution="Nursing School")],
                skills=["ICU"],
            ),
            77,
            [],
        )

    monkeypatch.setattr(pipeline.ai_parser, "parse", _boom_single_shot)
    monkeypatch.setattr(pipeline.orchestrator, "parse_light", _light)
    # The heuristic floor supplies the work history AND any section the enrich
    # agents dropped (here: certifications, when the CredentialsAgent times out).
    from app.models.schemas import CertificationItem, ExperienceItem
    monkeypatch.setattr(
        pipeline.heuristic_parser, "parse",
        lambda text, anchors: ParsedResumeAI(
            experience=[ExperienceItem(company="Hospital A", role="RN")],
            certifications=[CertificationItem(name="BLS")],
        ),
    )
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(
            job_id="s2", filename="r.docx", content=b"x", company_id="c1", sync=True
        )
    )
    assert result.partial is True
    # Semantic sections from the enrich pass (normalization may canonicalise the
    # skill name, e.g. "ICU" -> "Intensive Care Unit").
    assert result.parsed.personal_info.headline == "Registered Nurse"
    assert result.parsed.skills  # non-empty — recovered by the enrich pass
    # ...work history backfilled from the deterministic floor...
    assert len(result.parsed.experience) == 1
    assert result.parsed.experience[0].company == "Hospital A"
    # ...and certifications backfilled too when the enrich CredentialsAgent yields none.
    assert [c.name for c in result.parsed.certifications] == ["BLS"]
    assert any("human review" in w for w in result.warnings)


# ── Sync path must fit the gateway ceiling (504 regression) ───────────────────
#
# A synchronous parse that outlives the caller's gateway is severed into a bodyless
# 504 — no data, no job id, nothing to poll. For a DIRECT API caller that gateway is
# our own CloudFront (60s origin read timeout), which is what _SYNC_WALL_BUDGET is
# sized against. Callers behind a tighter gateway (the console, on Amplify's hard
# 30s) cannot be saved by any budget value and must send `async_only` instead.

_CLOUDFRONT_ORIGIN_CEILING = 60


def test_sync_budget_fits_under_the_direct_caller_ceiling():
    """The sync wall budget bounds the whole pipeline run. It must leave room under
    CloudFront's 60s origin read timeout for the request/response transfer AND the S3
    upload + worker dispatch of the promote-to-async handoff that follows a probe."""
    _HANDOFF_AND_TRANSFER_HEADROOM = 8
    assert pipeline._SYNC_WALL_BUDGET <= _CLOUDFRONT_ORIGIN_CEILING - _HANDOFF_AND_TRANSFER_HEADROOM

    # The reserves are carved OUT of that budget, so each must fit inside it and still
    # leave a usable AI window — otherwise every sync parse degrades on arrival.
    assert pipeline._SYNC_EXTRACT_RESERVE < pipeline._SYNC_WALL_BUDGET
    assert pipeline._SYNC_ENRICH_RESERVE < pipeline._SYNC_WALL_BUDGET
    assert pipeline._SYNC_WALL_BUDGET - pipeline._SYNC_ENRICH_RESERVE >= pipeline._MIN_SYNC_AI_TIMEOUT


@pytest.mark.asyncio
async def test_sync_extraction_is_cut_off_by_the_wall_budget(monkeypatch):
    """Extraction used to run OUTSIDE the sync budget, on its own 60s/90s caps — so a
    slow step could burn the whole gateway ceiling before the AI parse even began.
    It must now be clamped to the time the budget can actually afford."""
    import time as _time

    def _slow_extract(_content):
        _time.sleep(5)  # far longer than the clamped budget below allows
        return _LONG_TEXT

    _stub_extraction(monkeypatch)
    monkeypatch.setattr(pipeline.docx_extractor, "extract", _slow_extract)
    # Shrink the budget so the clamp bites immediately instead of after ~8 real seconds.
    monkeypatch.setattr(pipeline, "_SYNC_WALL_BUDGET", 1)
    monkeypatch.setattr(pipeline, "_SYNC_EXTRACT_RESERVE", 0)
    monkeypatch.setattr(pipeline, "_MIN_EXTRACT_TIMEOUT", 0.2)

    started = _time.monotonic()
    with pytest.raises(ExtractionError):
        await pipeline.run(
            pipeline.PipelineInput(
                job_id="s5", filename="r.docx", content=b"x", company_id="c1",
                sync=True, sync_probe=True,
            )
        )
    # Cut off by the budget, not by the extractor's own 60s cap.
    assert _time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_sync_does_not_run_ocr_inline_and_degrades_for_promotion(monkeypatch):
    """A digital PDF with an undecodable text layer must NOT trigger an inline OCR
    pass on the sync path — OCR is budgeted at 90s, three times the entire gateway
    ceiling, so starting it guarantees a 504. It must degrade to a flagged partial,
    which the endpoints promote to the async worker."""
    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: (pipeline.ExtractionStrategy.PDF, False),
    )
    # A text layer that looks like broken CID-encoded fonts.
    monkeypatch.setattr(
        pipeline.pdf_extractor, "extract",
        lambda content: "(cid:12)(cid:9)(cid:44)(cid:7)(cid:31)(cid:88)" * 40,
    )

    def _ocr_must_not_run(*_a, **_k):
        raise AssertionError("OCR must not run inline on the sync path")

    monkeypatch.setattr(pipeline.ocr_extractor, "extract", _ocr_must_not_run)

    async def _ai_must_not_run(_sections, _anchors):
        raise AssertionError("the AI parse must not run on an undecodable text layer")

    monkeypatch.setattr(pipeline.ai_parser, "parse", _ai_must_not_run)

    result = await pipeline.run(
        pipeline.PipelineInput(
            job_id="s3", filename="scan.pdf", content=b"x", company_id="c1",
            sync=True, sync_probe=True,
        )
    )

    # partial=True is the signal the endpoints use to promote to the async worker.
    assert result.partial is True
    assert result.ocr_used is False
    assert any("OCR" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_async_still_runs_ocr_inline_for_a_broken_text_layer(monkeypatch):
    """The async worker has no gateway ceiling, so it must still recover a broken
    text layer with a real OCR pass — that path is unchanged."""
    from app.models.schemas import ParsedResumeAI, PersonalInfo

    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: (pipeline.ExtractionStrategy.PDF, False),
    )
    monkeypatch.setattr(
        pipeline.pdf_extractor, "extract",
        lambda content: "(cid:12)(cid:9)(cid:44)(cid:7)(cid:31)(cid:88)" * 40,
    )
    monkeypatch.setattr(
        pipeline.ocr_extractor, "extract",
        lambda content, filename, force: ("Jane Smith\njane@example.com\nRegistered Nurse", True),
    )

    async def _single_shot(_sections, _anchors):
        return ParsedResumeAI(personal_info=PersonalInfo(full_name="Jane Smith")), 5

    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    monkeypatch.setattr(pipeline.orchestrator, "parse", _single_shot)  # short text → single-shot anyway

    result = await pipeline.run(
        pipeline.PipelineInput(job_id="a1", filename="scan.pdf", content=b"x", company_id="c1")
    )

    assert result.ocr_used is True
    assert result.partial is False
    assert result.parsed.personal_info.full_name == "Jane Smith"


@pytest.mark.asyncio
async def test_sync_skips_an_ai_call_it_cannot_afford(monkeypatch):
    """When extraction has eaten the budget, the sync path must not open an AI call
    that cannot land inside the ceiling — it degrades immediately so the caller can
    still promote to async while there is time to dispatch."""
    async def _ai_must_not_run(_sections, _anchors):
        raise AssertionError("must not start an AI call the budget cannot afford")

    monkeypatch.setattr(pipeline.ai_parser, "parse", _ai_must_not_run)
    _stub_extraction(monkeypatch)
    # Pretend the wall budget is already spent by the time extraction finishes.
    monkeypatch.setattr(pipeline, "_SYNC_WALL_BUDGET", 0)

    result = await pipeline.run(
        pipeline.PipelineInput(
            job_id="s4", filename="r.docx", content=b"x", company_id="c1",
            sync=True, sync_probe=True,
        )
    )

    assert result.partial is True
    assert any("human review" in w for w in result.warnings)


# ── A lost work history must never be reported as a clean parse ───────────────


@pytest.mark.asyncio
async def test_orchestrator_losing_work_history_falls_back_to_single_shot(monkeypatch):
    """Production bug: the orchestrator's work stage failed, every other section
    succeeded, and the résumé came back status="completed", partial=false with
    experience=[] — a nurse silently recorded as having never worked.

    The orchestrator now fails in that case, so the async path falls back to the
    single-shot parser, which reads the whole résumé in one call and recovers the
    roles. The caller gets a complete record, not a jobless one."""
    from app.models.schemas import ExperienceItem, ParsedResumeAI, PersonalInfo

    async def _orch_loses_work(_text, _anchors, budget=None):
        raise AIParsingError("Work-history extraction failed and recovered no roles")

    async def _single_shot(_sections, _anchors):
        return (
            ParsedResumeAI(
                personal_info=PersonalInfo(full_name="Jane Smith"),
                experience=[ExperienceItem(company="Mercy Hospital", role="RN - NICU")],
            ),
            500,
        )

    monkeypatch.setattr(pipeline.orchestrator, "parse", _orch_loses_work)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _single_shot)
    _stub_extraction(monkeypatch)

    result = await pipeline.run(
        pipeline.PipelineInput(job_id="w1", filename="r.docx", content=b"x", company_id="c1")
    )

    # The work history is back...
    assert [e.company for e in result.parsed.experience] == ["Mercy Hospital"]
    # ...and it's a genuinely clean parse, not a partial.
    assert result.partial is False


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
