"""
Integration tests for POST /api/v1/resume/batch.

Batch submit does two blocking AWS calls per file (S3 put + DynamoDB put) before it
can return its 202. Those used to run in a sequential loop, so submit time grew
linearly with the batch and a large batch could burn the caller's gateway timeout
before the 202 was ever returned - the same failure that produced the 504 on
/resume/parse. Staging is now concurrent, and one unstageable file no longer sinks
the whole batch.

DynamoDB and S3 are mocked; the endpoint itself runs for real.
"""

import time

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.batch as batch
from app.main import app

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40

# A minimal valid DOCX (magic bytes are all validate_file checks here).
_DOCX = b"PK\x03\x04 fake docx"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps, "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


def _files(n: int) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (f"resume_{i}.docx", _DOCX, _DOCX_MIME)) for i in range(n)]


def _stub_batch_io(monkeypatch, *, upload_delay: float = 0.0) -> None:
    _authenticate(monkeypatch)
    monkeypatch.setattr(batch, "validate_file", lambda fn, content: "docx")

    def _upload(job_id, filename, content):
        if upload_delay:
            time.sleep(upload_delay)  # stand in for a real S3 round-trip
        return f"temp/{job_id}/{filename}"

    monkeypatch.setattr(batch.s3_client, "upload_temp_file", _upload)
    monkeypatch.setattr(batch.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(batch.db, "create_batch", lambda *a, **kw: None)

    # Neutralize BOTH dispatch paths, so these tests measure the endpoint and nothing
    # else regardless of how USE_LAMBDA_WORKER happens to be set in the environment.
    #
    # This matters more than it looks. Without the local stub, a CI run (where
    # use_lambda_worker is false) takes the BackgroundTasks path - and Starlette's
    # TestClient runs background tasks synchronously BEFORE client.post() returns. The
    # timing test below would then be timing a full local parse of 12 resumes against
    # real AWS, not the staging it means to measure.
    async def _no_local_processing(batch_id, jobs):
        return None

    monkeypatch.setattr(batch, "invoke_worker", lambda settings, job: True)
    monkeypatch.setattr(batch, "process_batch_locally", _no_local_processing)


def test_batch_pairs_each_job_with_its_filename(monkeypatch):
    """The caller must be able to match a result back to the file it came from -
    a bare job_ids array can't be lined up once files are skipped."""
    _stub_batch_io(monkeypatch)

    resp = client.post(
        "/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY}, files=_files(3),
    )
    body = resp.json()

    assert resp.status_code == 202
    assert body["total"] == 3
    assert [j["filename"] for j in body["jobs"]] == ["resume_0.docx", "resume_1.docx", "resume_2.docx"]
    # job_ids stays in lockstep with jobs (kept for existing integrations).
    assert [j["job_id"] for j in body["jobs"]] == body["job_ids"]
    assert body["poll_url"] == f"/api/v1/resume/batch/{body['batch_id']}"


def test_batch_stages_files_concurrently(monkeypatch):
    """THE REGRESSION. With staging serialized, 12 files x a 0.2s upload would take
    ~2.4s and scale linearly into the gateway timeout. Concurrent staging must finish
    in roughly the time of a single upload."""
    _stub_batch_io(monkeypatch, upload_delay=0.2)

    started = time.monotonic()
    resp = client.post(
        "/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY}, files=_files(12),
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 202
    assert resp.json()["total"] == 12
    # Serial would be ~2.4s. Allow generous slack for thread-pool scheduling.
    assert elapsed < 1.2, f"staging looks serialized: {elapsed:.2f}s for 12 files"


def test_one_unstageable_file_does_not_sink_the_batch(monkeypatch):
    """A single failed S3 put used to raise straight out of the handler and 500 the
    whole submission. It must be reported as skipped while the rest proceed."""
    _stub_batch_io(monkeypatch)

    def _flaky_upload(job_id, filename, content):
        if filename == "resume_1.docx":
            raise RuntimeError("S3 is having a moment")
        return f"temp/{job_id}/{filename}"

    monkeypatch.setattr(batch.s3_client, "upload_temp_file", _flaky_upload)

    resp = client.post(
        "/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY}, files=_files(3),
    )
    body = resp.json()

    assert resp.status_code == 202
    assert body["total"] == 2
    assert [j["filename"] for j in body["jobs"]] == ["resume_0.docx", "resume_2.docx"]
    assert [s["filename"] for s in body["skipped_files"]] == ["resume_1.docx"]


def test_batch_rejects_unsupported_files_without_dropping_the_rest(monkeypatch):
    """Validation failures are reported per file, not as a whole-batch error."""
    _stub_batch_io(monkeypatch)

    from app.core.exceptions import UnsupportedFileTypeError

    def _validate(filename, content):
        if filename.endswith(".txt"):
            raise UnsupportedFileTypeError("Unsupported file extension '.txt'")
        return "docx"

    monkeypatch.setattr(batch, "validate_file", _validate)

    resp = client.post(
        "/api/v1/resume/batch",
        headers={"X-API-Key": VALID_KEY},
        files=[
            ("files", ("good.docx", _DOCX, _DOCX_MIME)),
            ("files", ("notes.txt", b"just text", "text/plain")),
        ],
    )
    body = resp.json()

    assert resp.status_code == 202
    assert body["total"] == 1
    assert body["skipped"] == 1
    assert body["skipped_files"][0]["filename"] == "notes.txt"


def test_batch_with_no_valid_files_is_a_422(monkeypatch):
    _stub_batch_io(monkeypatch)

    from app.core.exceptions import UnsupportedFileTypeError

    monkeypatch.setattr(
        batch, "validate_file",
        lambda fn, content: (_ for _ in ()).throw(UnsupportedFileTypeError("nope")),
    )

    resp = client.post(
        "/api/v1/resume/batch",
        headers={"X-API-Key": VALID_KEY},
        files=[("files", ("notes.txt", b"x", "text/plain"))],
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error_code"] == "EMPTY_BATCH"
