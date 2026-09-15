"""The SQS client is built once per region, not once per enqueue (C3)."""

from app.workers import dispatch


class _Settings:
    aws_region = "us-east-2"
    worker_queue_url = "https://sqs.example/q"


def test_sqs_client_is_cached(monkeypatch):
    built: list[str] = []

    class _Fake:
        def send_message(self, **kw):
            return {}

    def _client(service, **kw):
        built.append(service)
        return _Fake()

    monkeypatch.setattr(dispatch.boto3, "client", _client)
    cache = getattr(dispatch, "_sqs_client_for", None)
    if cache is not None:
        cache.cache_clear()

    for _ in range(3):
        assert dispatch.enqueue_job(_Settings(), {"job_id": "j"}) is True

    assert built == ["sqs"]
    if cache is not None:
        cache.cache_clear()
