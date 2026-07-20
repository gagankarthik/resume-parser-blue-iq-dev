"""SQS enqueue helpers - single send, batched send, and failure surfacing.

The API path depends on `enqueue_*` returning failure honestly: a job that could
not be queued must be failed immediately, never left "processing" forever.
"""

import json

import pytest

from app.workers import dispatch


class _FakeSQS:
    def __init__(self, *, send_raises=False, failed_ids=None, batch_raises=False):
        self.send_raises = send_raises
        self.failed_ids = set(failed_ids or [])
        self.batch_raises = batch_raises
        self.sent = []
        self.batches = []

    def send_message(self, QueueUrl, MessageBody):
        if self.send_raises:
            raise RuntimeError("AccessDenied")
        self.sent.append(json.loads(MessageBody))
        return {"MessageId": "abc"}

    def send_message_batch(self, QueueUrl, Entries):
        if self.batch_raises:
            raise RuntimeError("transport down")
        self.batches.append(Entries)
        failed = [
            {"Id": e["Id"], "Code": "InternalError"}
            for e in Entries
            if json.loads(e["MessageBody"])["job_id"] in self.failed_ids
        ]
        return {"Successful": [], "Failed": failed}


class _Settings:
    aws_region = "us-east-2"
    worker_queue_url = "https://sqs.example/q"


@pytest.fixture
def fake_sqs(monkeypatch):
    fake = _FakeSQS()
    monkeypatch.setattr(dispatch, "_sqs_client", lambda settings: fake)
    return fake


def test_enqueue_job_ok(fake_sqs):
    assert dispatch.enqueue_job(_Settings(), {"job_id": "j1"}) is True
    assert fake_sqs.sent == [{"job_id": "j1"}]


def test_enqueue_job_failure_returns_false(monkeypatch):
    fake = _FakeSQS(send_raises=True)
    monkeypatch.setattr(dispatch, "_sqs_client", lambda settings: fake)
    assert dispatch.enqueue_job(_Settings(), {"job_id": "j1"}) is False


def test_enqueue_jobs_all_ok(fake_sqs):
    jobs = [{"job_id": f"j{i}"} for i in range(3)]
    failed = dispatch.enqueue_jobs(_Settings(), jobs)
    assert failed == set()
    assert len(fake_sqs.batches) == 1
    assert len(fake_sqs.batches[0]) == 3


def test_enqueue_jobs_chunks_over_ten(fake_sqs):
    jobs = [{"job_id": f"j{i}"} for i in range(23)]
    failed = dispatch.enqueue_jobs(_Settings(), jobs)
    assert failed == set()
    # 23 messages -> chunks of 10, 10, 3
    assert [len(b) for b in fake_sqs.batches] == [10, 10, 3]


def test_enqueue_jobs_surfaces_per_entry_failures(monkeypatch):
    fake = _FakeSQS(failed_ids={"j1"})
    monkeypatch.setattr(dispatch, "_sqs_client", lambda settings: fake)
    jobs = [{"job_id": "j0"}, {"job_id": "j1"}, {"job_id": "j2"}]
    failed = dispatch.enqueue_jobs(_Settings(), jobs)
    assert failed == {"j1"}


def test_enqueue_jobs_whole_chunk_error_fails_every_job(monkeypatch):
    fake = _FakeSQS(batch_raises=True)
    monkeypatch.setattr(dispatch, "_sqs_client", lambda settings: fake)
    jobs = [{"job_id": "j0"}, {"job_id": "j1"}]
    failed = dispatch.enqueue_jobs(_Settings(), jobs)
    assert failed == {"j0", "j1"}
