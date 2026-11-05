"""
NOT_A_RESUME end to end: one error code for an empty file and for a document that
is not a resume, on every surface a client sees it.

  * an empty upload is rejected on the request itself (HTTP 422)
  * a document the worker rejects fails the job with error_code NOT_A_RESUME,
    which the poll response and the parse.failed webhook both carry
"""

import pytest
from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.resume as resume
from app.core.exceptions import NotAResumeError
from app.main import app
from app.workers import background, webhook_sender

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps, "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


@pytest.mark.parametrize("content", [b"", b"   \n"])
def test_empty_upload_rejected_with_not_a_resume(monkeypatch, content):
    _authenticate(monkeypatch)
    monkeypatch.setattr(
        resume.s3_client, "upload_temp_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("an empty file must not be stored")),
    )

    resp = client.post(
        "/api/v1/resume/parse",
        headers={"X-API-Key": VALID_KEY},
        files={"file": ("resume.pdf", content, "application/pdf")},
    )

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["error_code"] == "NOT_A_RESUME"
    assert "not a resume" in err["hint"]


def test_poll_exposes_error_code_for_a_failed_job(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda job_id: {
            "job_id": job_id, "company_id": "acme-1", "status": "failed",
            "error": "The document is not a resume (it looks like: invoice).",
            "error_code": "NOT_A_RESUME",
        },
    )

    resp = client.get("/api/v1/resume/job/01J3K5M2N4P6Q8R0S2T4U6V8W0",
                      headers={"X-API-Key": VALID_KEY})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "NOT_A_RESUME"
    assert body["data"] is None


@pytest.mark.asyncio
async def test_worker_stores_and_webhooks_not_a_resume(monkeypatch):
    failed: dict = {}
    delivered: list[tuple[str, dict]] = []

    async def _reject(_inp):
        raise NotAResumeError("The document is not a resume (it looks like: job description).")

    async def _deliver(company_id, event, payload):
        delivered.append((event, payload))

    monkeypatch.setattr(background, "run_pipeline", _reject)
    monkeypatch.setattr(webhook_sender, "deliver_event", _deliver)
    monkeypatch.setattr(background.s3_client, "download_file", lambda key: b"%PDF-1.7")
    monkeypatch.setattr(background.s3_client, "delete_file", lambda key: None)
    monkeypatch.setattr(background.db, "update_job_processing", lambda job_id: None)
    monkeypatch.setattr(
        background.db, "update_job_failed",
        lambda job_id, error, error_code: failed.update(error=error, error_code=error_code),
    )
    audit: dict = {}
    monkeypatch.setattr(background.db, "write_audit_log", lambda **kw: audit.update(kw))

    await background.process_resume_async(
        job_id="j1", company_id="acme-1", s3_key="temp/j1/jd.pdf",
        filename="jd.pdf", file_size_bytes=8,
    )

    assert failed["error_code"] == "NOT_A_RESUME"
    assert audit["error_code"] == "NOT_A_RESUME" and audit["status"] == "failed"
    assert delivered == [(
        "parse.failed",
        {"job_id": "j1", "error": failed["error"], "error_code": "NOT_A_RESUME"},
    )]
