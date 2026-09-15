"""Submit / retry / poll failure paths on the single-file resume endpoints.

  * A3  - the retry limit was never enforced (nothing incremented the original row)
  * A4  - a result too large for DynamoDB 500'd every poll
  * A10 - a dispatch failure left the job `pending` forever and the file in S3
  * A12 - an S3 throttle/permission error on parse-uploaded read as UPLOAD_NOT_FOUND
  * L1  - an over-long filename 500'd upload-url (S3 key > 1024 bytes)
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from moto import mock_aws

import app.api.dependencies as deps
import app.api.v1.endpoints.resume as resume
import app.services.application.resume_service as resume_service
from app.core.config import get_settings
from app.db import _client
from app.main import app
from app.storage import s3_client

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40
PDF = b"%PDF-1.4 minimal"


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(deps, "_get_cached_api_key",
                        lambda key_hash: {"company_id": company_id, "status": "active"})
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


@pytest.fixture
def jobs_table(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamodb_endpoint_url", "")
    with mock_aws():
        _client._get_dynamodb_resource.cache_clear()
        table = boto3.resource("dynamodb", region_name=settings.aws_region).create_table(
            TableName=settings.dynamodb_table_jobs,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table
    _client._get_dynamodb_resource.cache_clear()


def _stub_dispatch(monkeypatch) -> list[dict]:
    dispatched: list[dict] = []

    async def _fake(settings, background_tasks, payload):
        dispatched.append(dict(payload))

    monkeypatch.setattr(resume_service, "dispatch_async", _fake)
    return dispatched


# -- A3 ----------------------------------------------------------------------------

def test_retry_limit_is_enforced_on_the_original_job(monkeypatch, jobs_table):
    _authenticate(monkeypatch)
    monkeypatch.setattr(get_settings(), "max_retry_count", 3)
    monkeypatch.setattr(s3_client, "upload_temp_file", lambda job_id, filename, content: f"temp/{job_id}/source.pdf")
    _stub_dispatch(monkeypatch)
    jobs_table.put_item(Item={"job_id": "ORIG", "company_id": "acme-1", "status": "completed", "retry_count": 0})

    def _retry():
        return client.post("/api/v1/resume/ORIG/retry", headers={"X-API-Key": VALID_KEY},
                           files={"file": ("cv.pdf", PDF, "application/pdf")})

    counts = [_retry().json()["retry_count"] for _ in range(3)]
    assert counts == [1, 2, 3]

    fourth = _retry()
    assert fourth.status_code == 422
    assert fourth.json()["error"]["error_code"] == "RETRY_LIMIT_REACHED"


# -- A4 ----------------------------------------------------------------------------

def test_poll_of_an_oversized_result_returns_the_stub(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume.db, "get_job", lambda job_id: {
        "job_id": job_id, "company_id": "acme-1", "status": "completed",
        "result": {"data": None, "confidence": {"overall": 0.9}, "partial": False,
                   "warnings": ["result_omitted: too large"], "result_too_large": True},
    })

    resp = client.get("/api/v1/resume/job/J1", headers={"X-API-Key": VALID_KEY})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["data"] is None and body["confidence"] is None and body["skills_validation"] is None
    assert body["warnings"] == ["result_omitted: too large"]


# -- A10 ---------------------------------------------------------------------------

def _record_cleanup(monkeypatch) -> dict:
    seen: dict = {"failed": [], "deleted": []}
    monkeypatch.setattr(resume_service.db, "update_job_failed",
                        lambda job_id, error, code: seen["failed"].append((job_id, code)))
    monkeypatch.setattr(s3_client, "delete_file", lambda key: seen["deleted"].append(key))
    return seen


def test_parse_dispatch_failure_fails_the_job_and_removes_the_file(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume_service.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(s3_client, "upload_temp_file", lambda job_id, filename, content: f"temp/{job_id}/source.pdf")
    seen = _record_cleanup(monkeypatch)

    async def _boom(settings, background_tasks, payload):
        raise RuntimeError("SQS unavailable")

    monkeypatch.setattr(resume_service, "dispatch_async", _boom)

    resp = client.post("/api/v1/resume/parse", headers={"X-API-Key": VALID_KEY},
                       files={"file": ("cv.pdf", PDF, "application/pdf")})

    assert resp.status_code == 503
    assert resp.json()["error"]["error_code"] == "WORKER_DISPATCH_FAILED"
    assert [code for _, code in seen["failed"]] == ["WORKER_DISPATCH_FAILED"]
    assert len(seen["deleted"]) == 1 and seen["deleted"][0].endswith("/source.pdf")


def test_parse_uploaded_dispatch_failure_points_at_a_new_upload_url(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume.db, "get_job", lambda jid: {
        "company_id": "acme-1", "status": "pending_upload",
        "s3_key": "temp/J1/source.pdf", "filename": "cv.pdf"})
    monkeypatch.setattr(s3_client, "download_file", lambda key: PDF)
    monkeypatch.setattr(resume.db, "claim_upload_job", lambda jid: True)
    seen = _record_cleanup(monkeypatch)

    async def _boom(settings, background_tasks, payload):
        raise RuntimeError("SQS unavailable")

    monkeypatch.setattr(resume_service, "dispatch_async", _boom)

    resp = client.post("/api/v1/resume/parse-uploaded", headers={"X-API-Key": VALID_KEY},
                       json={"job_id": "J1"})

    assert resp.status_code == 503
    assert "upload URL" in resp.json()["error"]["detail"]
    assert seen["failed"] == [("J1", "WORKER_DISPATCH_FAILED")]
    assert seen["deleted"] == ["temp/J1/source.pdf"]


def test_enqueue_failure_removes_the_staged_file(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(get_settings(), "worker_queue_url", "https://sqs.example/q")
    monkeypatch.setattr(resume_service.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(s3_client, "upload_temp_file", lambda job_id, filename, content: f"temp/{job_id}/source.pdf")
    monkeypatch.setattr(resume_service, "enqueue_job", lambda settings, payload: False)
    seen = _record_cleanup(monkeypatch)

    resp = client.post("/api/v1/resume/parse", headers={"X-API-Key": VALID_KEY},
                       files={"file": ("cv.pdf", PDF, "application/pdf")})

    # Unchanged contract: the submit answers, the poll reports the failure.
    assert resp.status_code == 200
    assert [code for _, code in seen["failed"]] == ["WORKER_DISPATCH_FAILED"]
    assert len(seen["deleted"]) == 1


# -- A12 ---------------------------------------------------------------------------

def _pending_upload(monkeypatch) -> None:
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume.db, "get_job", lambda jid: {
        "company_id": "acme-1", "status": "pending_upload",
        "s3_key": "temp/J1/source.pdf", "filename": "cv.pdf"})


def test_parse_uploaded_storage_outage_is_503_not_upload_not_found(monkeypatch):
    _pending_upload(monkeypatch)

    def _throttled(key):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "slow down"}}, "GetObject")

    monkeypatch.setattr(s3_client, "download_file", _throttled)
    resp = client.post("/api/v1/resume/parse-uploaded", headers={"X-API-Key": VALID_KEY},
                       json={"job_id": "J1"})
    assert resp.status_code == 503
    assert resp.json()["error"]["error_code"] == "SERVICE_UNAVAILABLE"


def test_parse_uploaded_missing_object_is_upload_not_found(monkeypatch):
    _pending_upload(monkeypatch)

    def _missing(key):
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")

    monkeypatch.setattr(s3_client, "download_file", _missing)
    resp = client.post("/api/v1/resume/parse-uploaded", headers={"X-API-Key": VALID_KEY},
                       json={"job_id": "J1"})
    assert resp.status_code == 422
    assert resp.json()["error"]["error_code"] == "UPLOAD_NOT_FOUND"


# -- L1 ----------------------------------------------------------------------------

def test_upload_url_rejects_an_overlong_filename(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(s3_client, "create_presigned_upload",
                        lambda job_id, filename, max_bytes, expires_in: {"key": "k", "url": "u", "fields": {}})
    monkeypatch.setattr(resume.db, "create_upload_job", lambda *a: None)

    resp = client.post("/api/v1/resume/upload-url", headers={"X-API-Key": VALID_KEY},
                       json={"filename": "a" * 300 + ".pdf"})
    assert resp.status_code == 422
    assert resp.json()["error"]["error_code"] == "VALIDATION_ERROR"


def test_multipart_filename_is_clamped_before_it_is_stored(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(resume_service.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(s3_client, "upload_temp_file", lambda job_id, filename, content: f"temp/{job_id}/source.pdf")
    dispatched = _stub_dispatch(monkeypatch)

    resp = client.post("/api/v1/resume/parse", headers={"X-API-Key": VALID_KEY},
                       files={"file": ("b" * 400 + ".pdf", PDF, "application/pdf")})

    assert resp.status_code == 200
    assert len(dispatched[0]["filename"]) <= 255
    assert dispatched[0]["filename"].endswith(".pdf")
