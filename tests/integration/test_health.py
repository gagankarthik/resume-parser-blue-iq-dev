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


def test_health_reports_async_dispatch_mode():
    """The dispatch path must be verifiable from outside. A deployment with no
    worker queue parses in-process and holds the HTTP response open for the whole
    parse; before this was reported, the only symptom was slow submits."""
    body = client.get("/api/v1/health").json()
    assert body["dependencies"]["worker"] in {"queue", "in-process"}
