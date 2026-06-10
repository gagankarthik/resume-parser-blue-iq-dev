"""The unified Lambda serves BOTH worker self-invokes and HTTP requests from
the same warm container. The worker handler runs its job on a private event
loop; it must leave a FRESH (open) loop installed afterwards, or Mangum's
HTTP cycle reuses the closed loop and every later HTTP request from that
container 502s with "RuntimeError: Event loop is closed".
"""

import asyncio

import pytest

from app.handlers import worker_lambda


def _event():
    return {
        "job_id": "j1", "company_id": "c1", "s3_key": "temp/j1/r.pdf",
        "filename": "r.pdf", "file_size_bytes": 100,
    }


def test_worker_leaves_open_event_loop(monkeypatch):
    async def fake_process(**kwargs):
        return None

    monkeypatch.setattr(worker_lambda, "process_resume_async", fake_process)
    worker_lambda.handler(_event(), context=None)

    loop = asyncio.get_event_loop()
    assert not loop.is_closed()
    loop.close()


def test_worker_leaves_open_event_loop_even_on_failure(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(worker_lambda, "process_resume_async", boom)
    with pytest.raises(RuntimeError):
        worker_lambda.handler(_event(), context=None)

    loop = asyncio.get_event_loop()
    assert not loop.is_closed()
    loop.close()
