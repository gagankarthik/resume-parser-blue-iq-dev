"""Worker Lambda handler - SQS batch consumption, the direct-dict fallback, and
event-loop hygiene.

The API Lambda may serve worker events from the same warm container (the unified
handler routes non-HTTP events here), so the worker runs its jobs on a private
event loop and MUST leave a FRESH (open) loop installed afterwards - otherwise
Mangum's next HTTP cycle reuses the closed loop and every later HTTP request from
that container 502s with "RuntimeError: Event loop is closed".
"""

import asyncio
import json

import pytest

from app.handlers import worker_lambda


def _job():
    return {
        "job_id": "j1", "company_id": "c1", "s3_key": "temp/j1/r.pdf",
        "filename": "r.pdf", "file_size_bytes": 100,
    }


def _sqs_event(*jobs, message_ids=None):
    ids = message_ids or [f"m{i}" for i in range(len(jobs))]
    return {
        "Records": [
            {"eventSource": "aws:sqs", "messageId": mid, "body": json.dumps(job)}
            for mid, job in zip(ids, jobs)
        ]
    }


@pytest.fixture
def _stub_db(monkeypatch):
    """Job not yet finished, so processing proceeds."""
    monkeypatch.setattr(worker_lambda.db, "get_job", lambda job_id: None)


# -- Event-loop hygiene (direct-dict path) -------------------------------------

def test_worker_leaves_open_event_loop(monkeypatch, _stub_db):
    async def fake_process(**kwargs):
        return None

    monkeypatch.setattr(worker_lambda, "process_resume_async", fake_process)
    worker_lambda.handler(_job(), context=None)

    loop = asyncio.get_event_loop()
    assert not loop.is_closed()
    loop.close()


def test_worker_leaves_open_event_loop_even_on_failure(monkeypatch, _stub_db):
    async def boom(**kwargs):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(worker_lambda, "process_resume_async", boom)
    with pytest.raises(RuntimeError):
        worker_lambda.handler(_job(), context=None)

    loop = asyncio.get_event_loop()
    assert not loop.is_closed()
    loop.close()


# -- SQS batch consumption -----------------------------------------------------

def test_sqs_success_reports_no_failures(monkeypatch, _stub_db):
    processed = []

    async def fake_process(**kwargs):
        processed.append(kwargs["job_id"])

    monkeypatch.setattr(worker_lambda, "process_resume_async", fake_process)
    out = worker_lambda.handler(_sqs_event(_job()), context=None)

    assert out == {"batchItemFailures": []}
    assert processed == ["j1"]


def test_sqs_reports_only_the_failed_message(monkeypatch, _stub_db):
    """Message 1 blows up; only its id is redelivered - the other two are acked."""
    j0, j1, j2 = _job(), {**_job(), "job_id": "j2"}, {**_job(), "job_id": "j3"}

    async def fake_process(**kwargs):
        if kwargs["job_id"] == "j2":
            raise RuntimeError("infra fault")

    monkeypatch.setattr(worker_lambda, "process_resume_async", fake_process)
    event = _sqs_event(j0, j1, j2, message_ids=["m0", "m1", "m2"])
    out = worker_lambda.handler(event, context=None)

    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_sqs_malformed_message_goes_to_failures(monkeypatch, _stub_db):
    """A missing-field payload can never succeed - report it so SQS redelivers it
    toward the DLQ rather than silently dropping it."""
    monkeypatch.setattr(worker_lambda, "process_resume_async",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("should not run")))
    event = {
        "Records": [
            {"eventSource": "aws:sqs", "messageId": "bad", "body": json.dumps({"job_id": "x"})},
        ]
    }
    out = worker_lambda.handler(event, context=None)

    assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_sqs_skips_already_completed_job(monkeypatch):
    """SQS is at-least-once; a redelivery after completion must NOT re-run the
    pipeline (would re-emit a duplicate webhook)."""
    monkeypatch.setattr(worker_lambda.db, "get_job",
                        lambda job_id: {"status": "completed"})

    async def fake_process(**kwargs):
        raise AssertionError("terminal job should be skipped")

    monkeypatch.setattr(worker_lambda, "process_resume_async", fake_process)
    out = worker_lambda.handler(_sqs_event(_job()), context=None)

    assert out == {"batchItemFailures": []}  # acked, not reprocessed
