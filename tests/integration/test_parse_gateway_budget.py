"""
Gateway-budget integration tests for POST /api/v1/resume/parse.

Regression cover for the 504/timeout the console hit when parsing a resume.

The API now uses ONE uniform flow for every file: the request queues the file on
the full-budget async worker and returns a `job_id` + `poll_url` immediately - it
never parses on the request path. That is what makes every caller safe, including
those behind a tight gateway:

The console reaches this API through a Next.js route handler on AWS Amplify
Hosting, whose SSR compute has a HARD ~30s request timeout - not configurable, no
quota to raise, and Next's `maxDuration` is not honored there
(aws-amplify/amplify-hosting#3223, #3508). A synchronous parse could never fit
that: a *typical* two-role resume's single-shot AI pass measures ~20s, before
extraction, normalization and transfer. So instead of parsing inline, the request
just dispatches and answers immediately; the browser gets a job id it can poll,
and there is nothing on the request path to time out.

These tests drive the real HTTP endpoint (auth, storage and dispatch are stubbed)
and assert the contract: fast answer, no AI on the request path, file handed to
the worker, JSON retrieved by polling.
"""

import time

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.resume as resume
import app.services.application.resume_service as resume_service
from app.main import app
from app.services import pipeline

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40

# Amplify Hosting's hard SSR request timeout. Exceed it -> bare 504, no body.
AMPLIFY_HARD_CEILING_S = 30


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps, "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


def _stub_io(monkeypatch) -> dict:
    """Stub everything around the request: auth, file validation, storage, dispatch.

    Nothing parses on the request path anymore, so there is no pipeline to run here -
    the endpoint only validates, stores, and dispatches. We capture the dispatched
    payload to prove the file really was handed to the worker.
    """
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume, "validate_file", lambda fn, content: "docx")
    monkeypatch.setattr(resume.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(
        resume.s3_client, "upload_temp_file",
        lambda job_id, filename, content: f"temp/{job_id}/{filename}",
    )

    dispatched: dict = {}

    async def _fake_dispatch(settings, background_tasks, payload):
        dispatched.update(payload)

    monkeypatch.setattr(resume_service, "dispatch_async", _fake_dispatch)
    return dispatched


def _post_resume(data: dict | None = None) -> tuple[dict, int, float]:
    started = time.monotonic()
    resp = client.post(
        "/api/v1/resume/parse",
        headers={"X-API-Key": VALID_KEY},
        files={"file": ("jane.docx", b"PK\x03\x04 fake docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data=data or {},
    )
    return resp.json(), resp.status_code, time.monotonic() - started


def test_parse_returns_a_poll_url_immediately(monkeypatch):
    """Every file: the request answers immediately with a job id + poll URL and hands
    the file to the full-budget worker. No parse on the request path, so nothing can
    hold the connection open until the gateway severs it."""
    dispatched = _stub_io(monkeypatch)

    body, status, elapsed = _post_resume()

    assert status == 200
    assert body["status"] == "processing"
    assert body["poll_url"] == f"/api/v1/resume/job/{body['job_id']}"
    assert body.get("data") is None  # nothing parsed inline
    # The file really was handed to the worker.
    assert dispatched["job_id"] == body["job_id"]
    # Immediate: a dispatch, not a parse. Nowhere near any gateway's cut-off.
    assert elapsed < 5
    assert elapsed < AMPLIFY_HARD_CEILING_S


def test_parse_never_parses_on_the_request_path(monkeypatch):
    """THE 504 FIX. There is no synchronous parse to time out: even a file that would
    take a slow OCR or a dense multi-agent parse is only dispatched, never run inline.
    The actual parse happens in the worker (pipeline.run); it must never be reached on
    the request path."""
    dispatched = _stub_io(monkeypatch)

    async def _pipeline_must_not_run(*_a, **_k):
        raise AssertionError("no parsing may happen on the request path")

    monkeypatch.setattr(pipeline, "run", _pipeline_must_not_run)

    body, status, _ = _post_resume()

    assert status == 200
    assert body["status"] == "processing"
    assert body.get("data") is None
    assert dispatched["job_id"] == body["job_id"]


def test_dispatch_failure_surfaces_as_an_error(monkeypatch):
    """If the file cannot be queued to the worker, the caller gets a clear error
    rather than a job that will never complete."""
    _stub_io(monkeypatch)

    async def _boom(settings, background_tasks, payload):
        raise RuntimeError("SQS unavailable")

    monkeypatch.setattr(resume_service, "dispatch_async", _boom)

    body, status, _ = _post_resume()

    assert status == 503
    assert body["error"]["error_code"] == "WORKER_DISPATCH_FAILED"
