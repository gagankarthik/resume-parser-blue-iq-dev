"""
Gateway-budget integration tests for POST /api/v1/resume/parse.

Regression cover for the 504 the console hit when parsing a resume.

The console reaches this API through a Next.js route handler on AWS Amplify
Hosting, whose SSR compute has a HARD 30s request timeout - not configurable, no
quota to raise, and Next's `maxDuration` is not honored there
(aws-amplify/amplify-hosting#3223, #3508). A complete synchronous parse does not
fit that: a *typical* two-role resume's single-shot AI pass measures ~20s, before
extraction, normalization and transfer. So the console blocked, Amplify severed
the connection, and the browser got a bodyless 504 - no data, no job id, nothing
to poll.

The two-sided contract these tests pin:

  * A caller behind a tight gateway (the console) sends `async_only`, gets a job
    id back immediately, and polls. No AI on the request path, nothing to 504.
  * A DIRECT caller (the paying integration, behind CloudFront's 60s) keeps the
    synchronous fast path: JSON inline when the parse fits the budget, and a
    promote-to-async poll URL when it doesn't. It never just blocks.

Unlike the unit tests, these drive the real HTTP endpoint through the REAL
pipeline (only the AI call, storage, and dispatch are stubbed) - so the budget
logic itself is what's under test, not a mock of it.
"""

import time

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.resume as resume
import app.services.application.resume_service as resume_service
from app.main import app
from app.models.schemas import ParsedResumeAI, PersonalInfo
from app.services import pipeline

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40

# Amplify Hosting's hard SSR request timeout. Exceed it -> bare 504, no body.
AMPLIFY_HARD_CEILING_S = 30

_RESUME_TEXT = (
    "Katherine Driscoll, RN\nkatherine@example.com\n(555) 234-5678\n"
    + ("Registered Nurse — ICU. Managed patient care. " * 200)
)


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps, "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


def _stub_io(monkeypatch) -> dict:
    """Stub everything around the pipeline: auth, file validation, storage, dispatch.

    The pipeline itself runs for real - that's the point.
    """
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume, "validate_file", lambda fn, content: "docx")
    monkeypatch.setattr(resume, "classify", lambda fn, content: ("docx", False))
    monkeypatch.setattr(resume_service.db, "write_audit_log", lambda **kw: None)
    monkeypatch.setattr(resume.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(
        resume.s3_client, "upload_temp_file",
        lambda job_id, filename, content: f"temp/{job_id}/{filename}",
    )

    # Real extraction is irrelevant here; we care about the AI step's budget.
    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: (pipeline.ExtractionStrategy.DOCX, False),
    )
    monkeypatch.setattr(pipeline.docx_extractor, "extract", lambda content: _RESUME_TEXT)

    dispatched: dict = {}

    async def _fake_dispatch(settings, background_tasks, payload):
        dispatched.update(payload)

    monkeypatch.setattr(resume_service, "dispatch_async", _fake_dispatch)
    return dispatched


def _post_resume() -> tuple[dict, int, float]:
    started = time.monotonic()
    resp = client.post(
        "/api/v1/resume/parse",
        headers={"X-API-Key": VALID_KEY},
        files={"file": ("jane.docx", b"PK\x03\x04 fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    return resp.json(), resp.status_code, time.monotonic() - started


def test_slow_resume_is_promoted_to_async_instead_of_hanging(monkeypatch):
    """THE 504 REGRESSION. A resume the AI cannot parse inside the sync budget must
    come back promptly as `processing` + a poll URL - not hold the connection until
    the gateway severs it."""
    dispatched = _stub_io(monkeypatch)

    # Squeeze the budget so the test doesn't have to burn the real ~17s AI window.
    # The mechanism under test is unchanged; only the clock is scaled.
    monkeypatch.setattr(pipeline, "_SYNC_WALL_BUDGET", 3)
    monkeypatch.setattr(pipeline, "_MIN_SYNC_AI_TIMEOUT", 1)
    monkeypatch.setattr(pipeline, "_SYNC_EXTRACT_RESERVE", 1)

    async def _slow_ai(_sections, _anchors):
        # Never finishes inside the budget - the dense-resume case.
        import asyncio
        await asyncio.sleep(60)
        raise AssertionError("should have been cut off by the sync budget")

    monkeypatch.setattr(pipeline.ai_parser, "parse", _slow_ai)

    body, status, elapsed = _post_resume()

    assert status == 200
    # Promoted, not severed: the caller gets a job it can poll to completion.
    assert body["status"] == "processing"
    assert body["poll_url"] == f"/api/v1/resume/job/{body['job_id']}"
    # ...and the file really was handed to the full-budget worker.
    assert dispatched["job_id"] == body["job_id"]
    # The whole point: it answered, and nowhere near the gateway's cut-off.
    assert elapsed < AMPLIFY_HARD_CEILING_S


def test_fast_resume_still_returns_parsed_json_inline(monkeypatch):
    """The fast path must be untouched: a resume that parses inside the budget still
    returns its JSON on the same request, with no promotion and no polling."""
    dispatched = _stub_io(monkeypatch)

    async def _fast_ai(_sections, _anchors):
        return ParsedResumeAI(personal_info=PersonalInfo(full_name="Katherine Driscoll")), 100

    monkeypatch.setattr(pipeline.ai_parser, "parse", _fast_ai)

    body, status, elapsed = _post_resume()

    assert status == 200
    assert body["status"] == "completed"
    assert body["partial"] is False
    assert body["data"]["personal_info"]["full_name"] == "Katherine Driscoll"
    assert body.get("poll_url") is None
    assert not dispatched  # never touched the worker
    assert elapsed < AMPLIFY_HARD_CEILING_S


def test_undecodable_pdf_promotes_without_running_ocr_inline(monkeypatch):
    """The second, independent 504 path: a digital PDF with a broken text layer used
    to trigger a 90s OCR pass INSIDE the sync request - three times the whole gateway
    ceiling. It must now promote to the worker instead."""
    dispatched = _stub_io(monkeypatch)
    monkeypatch.setattr(resume, "validate_file", lambda fn, content: "pdf")
    monkeypatch.setattr(resume, "classify", lambda fn, content: ("pdf", False))
    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: (pipeline.ExtractionStrategy.PDF, False),
    )
    # A text layer of undecodable CID glyphs - passes the length gate, unusable.
    monkeypatch.setattr(
        pipeline.pdf_extractor, "extract",
        lambda content: "(cid:12)(cid:9)(cid:44)(cid:31)" * 60,
    )

    def _ocr_must_not_run(*_a, **_k):
        raise AssertionError("OCR must never run inline on a synchronous request")

    monkeypatch.setattr(pipeline.ocr_extractor, "extract", _ocr_must_not_run)

    body, status, elapsed = _post_resume()

    assert status == 200
    assert body["status"] == "processing"
    assert body["poll_url"] == f"/api/v1/resume/job/{body['job_id']}"
    assert dispatched["job_id"] == body["job_id"]  # worker will OCR it on the full budget
    assert elapsed < AMPLIFY_HARD_CEILING_S


def test_async_only_returns_a_poll_url_without_ever_calling_the_ai(monkeypatch):
    """THE CONSOLE'S FIX. A caller behind a gateway too tight for a complete parse
    (Amplify: hard 30s) sends `async_only` and must get a job id back immediately -
    no AI call on the request path at all, so there is nothing to time out and
    nothing to 504.

    This is what makes the console correct: a *typical* resume's single-shot AI pass
    measures ~20s, so no sync budget can fit a complete parse inside 30s. The console
    must not block on one, and a probe it can never win would just burn tokens and
    re-parse from scratch on the worker."""
    dispatched = _stub_io(monkeypatch)

    async def _ai_must_not_run(_sections, _anchors):
        raise AssertionError("async_only must never invoke the AI on the request path")

    monkeypatch.setattr(pipeline.ai_parser, "parse", _ai_must_not_run)

    started = time.monotonic()
    resp = client.post(
        "/api/v1/resume/parse",
        headers={"X-API-Key": VALID_KEY},
        files={"file": ("jane.docx", b"PK\x03\x04 fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"async_only": "true"},
    )
    elapsed = time.monotonic() - started
    body = resp.json()

    assert resp.status_code == 200
    assert body["status"] == "processing"
    assert body["poll_url"] == f"/api/v1/resume/job/{body['job_id']}"
    assert dispatched["job_id"] == body["job_id"]
    # Immediate: a dispatch, not a parse. Nowhere near Amplify's cut-off.
    assert elapsed < 5


def test_sync_budget_still_fits_a_direct_caller(monkeypatch):
    """The paying integration calls the API directly (CloudFront, 60s), where a
    synchronous parse genuinely works - that fast path must survive the console's fix.
    Guard the budget against drifting over CloudFront's origin read timeout."""
    CLOUDFRONT_ORIGIN_CEILING_S = 60
    assert pipeline._SYNC_WALL_BUDGET <= CLOUDFRONT_ORIGIN_CEILING_S - 8
