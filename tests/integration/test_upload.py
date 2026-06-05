"""
Integration tests for the presigned large-file upload flow:
  POST /api/v1/resume/upload-url
  POST /api/v1/resume/parse-uploaded

DynamoDB and S3 are mocked via monkeypatch (same approach as test_feedback.py).
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.resume as resume
from app.main import app
from app.models.schemas import ConfidenceScores, ParsedResumeAI
from app.services.pipeline import PipelineResult

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps,
        "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )


# ── upload-url ─────────────────────────────────────────────────────────────────

def test_upload_url_requires_api_key():
    resp = client.post("/api/v1/resume/upload-url", json={"filename": "cv.pdf"})
    assert resp.status_code == 401


def test_upload_url_rejects_unsupported_extension(monkeypatch):
    _authenticate(monkeypatch)
    resp = client.post(
        "/api/v1/resume/upload-url",
        headers={"X-API-Key": VALID_KEY},
        json={"filename": "notes.txt"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_url_happy_path(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(
        resume.s3_client,
        "create_presigned_upload",
        lambda job_id, filename, max_bytes, expires_in: {
            "key": f"temp/{job_id}/{filename}",
            "url": "https://bucket.s3.amazonaws.com/",
            "fields": {"key": f"temp/{job_id}/{filename}", "x-amz-server-side-encryption": "AES256"},
        },
    )
    captured: dict = {}
    monkeypatch.setattr(
        resume.db, "create_upload_job",
        lambda job_id, company_id, s3_key, filename: captured.update(
            job_id=job_id, company_id=company_id, s3_key=s3_key, filename=filename
        ),
    )

    resp = client.post(
        "/api/v1/resume/upload-url",
        headers={"X-API-Key": VALID_KEY},
        json={"filename": "jane.pdf"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"]
    assert body["upload_url"].startswith("https://")
    assert body["fields"]["x-amz-server-side-encryption"] == "AES256"
    assert body["parse_url"] == "/api/v1/resume/parse-uploaded"
    # Upload job persisted under the authenticated company.
    assert captured["company_id"] == "acme-1"
    assert captured["job_id"] == body["job_id"]
    assert captured["filename"] == "jane.pdf"


# ── parse-uploaded ───────────────────────────────────────────────────────────

def test_parse_uploaded_requires_api_key():
    resp = client.post("/api/v1/resume/parse-uploaded", json={"job_id": "J1"})
    assert resp.status_code == 401


def test_parse_uploaded_rejects_other_company(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda jid: {"company_id": "someone-else", "status": "pending_upload",
                     "s3_key": "temp/J1/cv.pdf", "filename": "cv.pdf"},
    )
    resp = client.post(
        "/api/v1/resume/parse-uploaded",
        headers={"X-API-Key": VALID_KEY},
        json={"job_id": "J1"},
    )
    assert resp.status_code == 404


def test_parse_uploaded_rejects_already_parsed(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda jid: {"company_id": "acme-1", "status": "completed",
                     "s3_key": "temp/J1/cv.pdf", "filename": "cv.pdf"},
    )
    resp = client.post(
        "/api/v1/resume/parse-uploaded",
        headers={"X-API-Key": VALID_KEY},
        json={"job_id": "J1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["error_code"] == "UPLOAD_ALREADY_PARSED"


def test_parse_uploaded_missing_file_returns_422(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda jid: {"company_id": "acme-1", "status": "pending_upload",
                     "s3_key": "temp/J1/cv.pdf", "filename": "cv.pdf"},
    )

    def _boom(key):
        raise Exception("NoSuchKey")

    monkeypatch.setattr(resume.s3_client, "download_file", _boom)
    resp = client.post(
        "/api/v1/resume/parse-uploaded",
        headers={"X-API-Key": VALID_KEY},
        json={"job_id": "J1"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["error_code"] == "UPLOAD_NOT_FOUND"


def test_parse_uploaded_async_path_dispatches(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda jid: {"company_id": "acme-1", "status": "pending_upload",
                     "s3_key": "temp/J1/scan.png", "filename": "scan.png"},
    )
    monkeypatch.setattr(resume.s3_client, "download_file", lambda key: b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(resume, "validate_file", lambda fn, content: "png")
    monkeypatch.setattr(resume, "classify", lambda fn, content: ("ocr", True))

    dispatched: dict = {}

    async def _fake_dispatch(settings, background_tasks, payload):
        dispatched.update(payload)

    monkeypatch.setattr(resume, "_dispatch_async", _fake_dispatch)

    resp = client.post(
        "/api/v1/resume/parse-uploaded",
        headers={"X-API-Key": VALID_KEY},
        json={"job_id": "J1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processing"
    assert body["poll_url"] == "/api/v1/resume/job/J1"
    assert dispatched["s3_key"] == "temp/J1/scan.png"


def test_parse_uploaded_sync_path_returns_result(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(
        resume.db, "get_job",
        lambda jid: {"company_id": "acme-1", "status": "pending_upload",
                     "s3_key": "temp/J1/cv.pdf", "filename": "cv.pdf"},
    )
    monkeypatch.setattr(resume.s3_client, "download_file", lambda key: b"%PDF-1.4 digital")
    monkeypatch.setattr(resume, "validate_file", lambda fn, content: "pdf")
    monkeypatch.setattr(resume, "classify", lambda fn, content: ("pdf", False))

    result = PipelineResult(
        parsed=ParsedResumeAI(skills=["ICU"]),
        confidence=ConfidenceScores(
            overall=0.9, personal_info=0.9, experience=0.9, education=0.9, skills=1.0
        ),
        file_type="pdf", ocr_used=False, ai_tokens_used=123, duration_ms=42,
    )

    async def _fake_pipeline(inp):
        return result

    monkeypatch.setattr(resume, "run_pipeline", _fake_pipeline)

    completed: dict = {}
    monkeypatch.setattr(resume.db, "update_job_completed", lambda jid, r: completed.update(jid=jid))
    monkeypatch.setattr(resume.db, "write_audit_log", lambda **kw: None)
    deleted: dict = {}
    monkeypatch.setattr(resume.s3_client, "delete_file", lambda key: deleted.update(key=key))

    resp = client.post(
        "/api/v1/resume/parse-uploaded",
        headers={"X-API-Key": VALID_KEY},
        json={"job_id": "J1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["data"]["skills"] == ["ICU"]
    assert body["skills_validation"]["total"] == 1
    # Sync path persists the result and deletes the temp file.
    assert completed["jid"] == "J1"
    assert deleted["key"] == "temp/J1/cv.pdf"
