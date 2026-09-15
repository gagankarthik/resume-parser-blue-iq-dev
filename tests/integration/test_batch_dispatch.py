"""Batch dispatch correctness.

  * A1 - jobs that failed to enqueue were marked failed on their row but never
         counted on the batch, so `processing` stayed > 0 until the TTL and
         batch.completed never fired
  * A2 - the batch worker payload was hand-built without `endpoint`, so every
         production batch audit record said endpoint=""
  * C1 - skipped files carry a machine-readable error_code
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.batch as batch
from app.core import file_validator
from app.core.config import get_settings
from app.main import app
from app.workers import webhook_sender

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40
_DOCX = b"PK\x03\x04 fake docx"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _stub(monkeypatch, *, enqueue_failures: str = "none") -> dict:
    state: dict = {"payloads": [], "failed": [], "increments": [], "delivered": []}
    monkeypatch.setattr(deps, "_get_cached_api_key",
                        lambda key_hash: {"company_id": "acme-1", "status": "active", "key_prefix": "rp_live_aa"})
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)
    monkeypatch.setattr(file_validator, "validate_file", lambda fn, content: "docx")
    monkeypatch.setattr(batch, "validate_file", lambda fn, content: "docx", raising=False)
    monkeypatch.setattr(get_settings(), "worker_queue_url", "https://sqs.example/q")
    monkeypatch.setattr(batch.s3_client, "upload_temp_file",
                        lambda job_id, filename, content: f"temp/{job_id}/source.docx")
    monkeypatch.setattr(batch.db, "create_job", lambda *a, **kw: None)
    monkeypatch.setattr(batch.db, "create_batch", lambda *a, **kw: None)
    monkeypatch.setattr(batch.s3_client, "delete_file", lambda key: state.setdefault("deleted", []).append(key))

    def _enqueue(settings, payloads):
        state["payloads"].extend(payloads)
        return {p["job_id"] for p in payloads} if enqueue_failures == "all" else set()

    monkeypatch.setattr(batch, "enqueue_jobs", _enqueue)
    monkeypatch.setattr(batch.db, "update_job_failed",
                        lambda job_id, error, code: state["failed"].append((job_id, code)))
    monkeypatch.setattr(batch.db, "mark_batch_counted", lambda job_id: True)

    def _increment(batch_id, field):
        state["increments"].append(field)
        return len(state["increments"]) == 2

    monkeypatch.setattr(batch.db, "increment_batch_counter", _increment)
    monkeypatch.setattr(batch.db, "get_batch", lambda batch_id: {"total": 2, "completed": 0, "failed": 2})

    async def _deliver(company_id, event, payload):
        state["delivered"].append((event, payload))

    monkeypatch.setattr(webhook_sender, "deliver_event", _deliver)
    return state


def _files(n: int):
    return [("files", (f"resume_{i}.docx", _DOCX, _DOCX_MIME)) for i in range(n)]


def test_enqueue_failures_are_counted_and_complete_the_batch(monkeypatch):
    state = _stub(monkeypatch, enqueue_failures="all")

    resp = client.post("/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY}, files=_files(2))

    assert resp.status_code == 202
    assert [code for _, code in state["failed"]] == ["WORKER_DISPATCH_FAILED"] * 2
    assert state["increments"] == ["failed", "failed"]
    assert [e for e, _ in state["delivered"]] == ["batch.completed"]
    assert len(state["deleted"]) == 2  # no resume bytes left for a worker that never comes


def test_batch_worker_payload_carries_the_endpoint(monkeypatch):
    state = _stub(monkeypatch)

    resp = client.post("/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY}, files=_files(2))

    assert resp.status_code == 202
    assert len(state["payloads"]) == 2
    for payload in state["payloads"]:
        assert payload["endpoint"] == "POST /resume/batch"
        assert payload["force_textract"] is False
        assert payload["batch_id"] == resp.json()["batch_id"]
        assert payload["key_prefix"] == "rp_live_aa"


def test_skipped_files_carry_an_error_code(monkeypatch):
    _stub(monkeypatch)

    resp = client.post(
        "/api/v1/resume/batch", headers={"X-API-Key": VALID_KEY},
        files=[("files", ("good.docx", _DOCX, _DOCX_MIME)), ("files", ("empty.pdf", b"", "application/pdf"))],
    )

    skipped = resp.json()["skipped_files"]
    assert skipped[0]["filename"] == "empty.pdf"
    assert skipped[0]["error_code"] == "NOT_A_RESUME"
    assert not skipped[0]["reason"].startswith("NOT_A_RESUME")
