from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200():
    """Health endpoint must always respond - never 500."""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    # In CI without AWS, status is "degraded" - that's expected.
    # In a healthy production env it returns "ok". Accept either.
    assert body["status"] in {"ok", "degraded"}
    assert "version" in body
    assert "environment" in body
    assert "dependencies" in body
