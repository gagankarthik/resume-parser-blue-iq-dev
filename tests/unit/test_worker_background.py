"""Worker (`process_resume_async`) failure semantics.

Pins three production-reachable bugs:
  * a result that could not be persisted still fired parse.completed while the row
    stayed `processing`; a BotoCoreError after a good parse failed the job instead
    of being retried (A11)
  * failed jobs exposed exception class names and raw botocore/OpenAI text as the
    public `error_code` / `error` (A6)
  * the S3 cleanup logged the object key, which embedded the candidate's filename (A5)
"""

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.exceptions import NotAResumeError
from app.workers import background, webhook_sender


class _Result:
    """The slice of PipelineResult the worker reads."""

    class _Model:
        def model_dump(self):
            return {"personal_info": {"full_name": "Jane"}}

    parsed = _Model()
    confidence = _Model()
    partial = False
    warnings: list[str] = []
    file_type = "pdf"
    duration_ms = 10
    ocr_used = False
    ai_tokens_used = 42


@pytest.fixture
def worker(monkeypatch):
    """Stub every side effect and record what the worker did."""
    state: dict = {"delivered": [], "failed": [], "completed": [], "audit": {}}
    monkeypatch.setattr(background, "_DB_RETRY_WAIT", 0)

    async def _deliver(company_id, event, payload):
        state["delivered"].append((event, payload))

    async def _ok(_inp):
        return _Result()

    monkeypatch.setattr(webhook_sender, "deliver_event", _deliver)
    monkeypatch.setattr(background, "run_pipeline", _ok)
    monkeypatch.setattr(background.s3_client, "download_file", lambda key: b"%PDF-1.7")
    monkeypatch.setattr(background.s3_client, "delete_file", lambda key: None)
    monkeypatch.setattr(background.db, "update_job_processing", lambda job_id: None)
    monkeypatch.setattr(background.db, "update_job_completed",
                        lambda job_id, result: state["completed"].append(result))
    monkeypatch.setattr(background.db, "update_job_failed",
                        lambda job_id, error, code: state["failed"].append((error, code)))
    monkeypatch.setattr(background.db, "write_audit_log", lambda **kw: state["audit"].update(kw))
    return state


async def _run(**kw):
    await background.process_resume_async(
        job_id="j1", company_id="acme-1", s3_key="temp/j1/source.pdf",
        filename="Jane_Doe_RN.pdf", file_size_bytes=8, **kw,
    )


# -- A11 ---------------------------------------------------------------------------

async def test_unpersisted_result_never_announces_completion(monkeypatch, worker):
    def _down(job_id, result):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem")

    monkeypatch.setattr(background.db, "update_job_completed", _down)
    await _run()

    events = [e for e, _ in worker["delivered"]]
    assert "parse.completed" not in events
    assert events == ["parse.failed"]
    assert worker["failed"] and worker["failed"][0][1] == "PARSE_FAILED"
    assert worker["audit"]["status"] == "failed"


async def test_transient_botocore_error_after_a_good_parse_is_retried(monkeypatch, worker):
    calls = {"n": 0}

    def _flaky(job_id, result):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EndpointConnectionError(endpoint_url="https://dynamodb")
        worker["completed"].append(result)

    monkeypatch.setattr(background.db, "update_job_completed", _flaky)
    await _run()

    assert worker["failed"] == []
    assert [e for e, _ in worker["delivered"]] == ["parse.completed"]
    assert worker["audit"]["status"] == "completed"


# -- A6 ----------------------------------------------------------------------------

async def test_internal_failure_text_is_not_public(monkeypatch, worker):
    async def _boom(_inp):
        raise ClientError(
            {"Error": {"Code": "AccessDenied",
                       "Message": "arn:aws:iam::123456789012:role/worker cannot read resume-parser-blue-iq-temp"}},
            "GetObject",
        )

    monkeypatch.setattr(background, "run_pipeline", _boom)
    await _run()

    error, code = worker["failed"][0]
    assert code == "PARSE_FAILED"
    assert "arn:aws" not in error and "ClientError" not in error
    event, payload = worker["delivered"][0]
    assert event == "parse.failed"
    assert payload["error_code"] == "PARSE_FAILED"
    assert "arn:aws" not in payload["error"]
    assert worker["audit"]["error_code"] == "PARSE_FAILED"


async def test_timeout_is_parse_timeout(monkeypatch, worker):
    async def _slow(_inp):
        raise TimeoutError

    monkeypatch.setattr(background, "run_pipeline", _slow)
    await _run()
    assert worker["failed"][0][1] == "PARSE_TIMEOUT"


async def test_gate_tokens_are_billed_on_a_rejected_document(monkeypatch, worker):
    async def _reject(_inp):
        exc = NotAResumeError("The document is not a resume (it looks like: invoice).")
        exc.tokens = 123
        raise exc

    monkeypatch.setattr(background, "run_pipeline", _reject)
    await _run()
    assert worker["audit"]["ai_tokens_used"] == 123
    assert worker["failed"][0] == ("The document is not a resume (it looks like: invoice).", "NOT_A_RESUME")


# -- A1 (worker side): batch bookkeeping still converges -------------------------

async def test_last_batch_job_fires_batch_completed(monkeypatch, worker):
    monkeypatch.setattr(background.db, "mark_batch_counted", lambda job_id: True)
    monkeypatch.setattr(background.db, "increment_batch_counter", lambda batch_id, field: True)
    monkeypatch.setattr(background.db, "get_batch",
                        lambda batch_id: {"total": 1, "completed": 1, "failed": 0})
    await _run(batch_id="B1")
    assert [e for e, _ in worker["delivered"]] == ["parse.completed", "batch.completed"]


# -- A5 ----------------------------------------------------------------------------

async def test_s3_cleanup_never_logs_the_key(monkeypatch, worker):
    logged: list = []

    class _Log:
        def __getattr__(self, _level):
            return lambda event, **kw: logged.append((event, kw))

    def _fail(key):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(background, "log", _Log())
    monkeypatch.setattr(background.s3_client, "delete_file", _fail)
    await background.process_resume_async(
        job_id="j1", company_id="acme-1", s3_key="temp/j1/Jane_Doe_RN.pdf",
        filename="Jane_Doe_RN.pdf", file_size_bytes=8,
    )
    assert "Jane" not in repr(logged)
