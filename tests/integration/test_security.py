"""
Security-control integration tests: rate limiting, request body-size guard,
and security response headers.
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
from app.core.config import get_settings
from app.main import app

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40


def _authenticate(monkeypatch, company_id: str = "acme-1") -> None:
    monkeypatch.setattr(
        deps, "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limit_returns_429_with_retry_after(monkeypatch):
    _authenticate(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)

    # A GET that only needs auth (no file body) — cheapest way to exercise the limit.
    import app.api.v1.endpoints.resume as resume
    monkeypatch.setattr(resume.db, "get_job", lambda job_id: None)

    def call():
        return client.get("/api/v1/resume/job/abc", headers={"X-API-Key": VALID_KEY})

    # First 3 pass auth (404 job-not-found); the 4th is throttled.
    assert [call().status_code for _ in range(3)] == [404, 404, 404]
    throttled = call()
    assert throttled.status_code == 429
    assert throttled.json()["error"]["error_code"] == "RATE_LIMITED"
    assert int(throttled.headers["Retry-After"]) >= 1


def test_rate_limit_disabled_never_throttles(monkeypatch):
    _authenticate(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    import app.api.v1.endpoints.resume as resume
    monkeypatch.setattr(resume.db, "get_job", lambda job_id: None)

    codes = {
        client.get("/api/v1/resume/job/x", headers={"X-API-Key": VALID_KEY}).status_code
        for _ in range(10)
    }
    assert codes == {404}


# ── Request body-size guard ───────────────────────────────────────────────────

def test_oversized_body_rejected_before_read(monkeypatch):
    settings = get_settings()
    # Shrink the ceiling so a small body trips it (mb=0 → limit = overhead only).
    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    monkeypatch.setattr(settings, "max_request_overhead_bytes", 500)

    resp = client.post(
        "/api/v1/resume/upload-url",
        headers={"X-API-Key": VALID_KEY, "Content-Type": "application/json"},
        content=b"x" * 1000,   # 1000 bytes > 500-byte limit
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["error_code"] == "REQUEST_TOO_LARGE"


# ── Security headers ──────────────────────────────────────────────────────────

def test_security_headers_present_on_every_response():
    resp = client.post("/api/v1/resume/parse")   # 401, but headers still applied
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "X-Request-ID" in resp.headers


def test_csp_and_hsts_set_in_production(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    resp = client.post("/api/v1/resume/parse")
    assert "Content-Security-Policy" in resp.headers
    assert "Strict-Transport-Security" in resp.headers
