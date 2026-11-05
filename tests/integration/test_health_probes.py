"""Health probes (A13).

A probe that timed out left its boto3 call running on the executor thread (60s
connect timeout + retries), and every health check built brand-new clients.
"""

from fastapi.testclient import TestClient

import app.api.v1.endpoints.health as health
from app.main import app

client = TestClient(app)


def test_probe_clients_are_cached_and_fail_fast(monkeypatch):
    built: list[tuple[str, dict]] = []

    class _Fake:
        def describe_table(self, **kw):
            return {}

        def head_bucket(self, **kw):
            return {}

    def _client(service, **kw):
        built.append((service, kw))
        return _Fake()

    monkeypatch.setattr(health.boto3, "client", _client)
    cache = getattr(health, "_client", None)
    if cache is not None:
        cache.cache_clear()

    for _ in range(3):
        assert client.get("/api/v1/health").json()["dependencies"]["dynamodb"] == "ok"

    assert sorted(service for service, _ in built) == ["dynamodb", "s3"]
    for _, kw in built:
        config = kw["config"]
        assert config.connect_timeout <= 1
        assert config.read_timeout <= 2
        assert config.retries["total_max_attempts"] == 1  # no retries

    if cache is not None:
        cache.cache_clear()
