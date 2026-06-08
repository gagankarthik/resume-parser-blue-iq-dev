"""
Integration tests for POST /api/v1/resume/{job_id}/feedback.

DynamoDB is mocked: the API-key cache lookup and feedback write are patched.
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.db.dynamodb as db
from app.main import app

client = TestClient(app)

VALID_KEY = "rp_live_" + "a" * 40
ENDPOINT = "/api/v1/resume/JOB123/feedback"


def _authenticate(monkeypatch, company_id: str = "acme-1"):
    """Make the API key resolve to an active record (bypasses the cache + DynamoDB)."""
    monkeypatch.setattr(
        deps,
        "_get_cached_api_key",
        lambda key_hash: {"company_id": company_id, "status": "active"},
    )
    # The auth path also checks the owning company isn't deactivated.
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)


def test_feedback_requires_api_key():
    resp = client.post(ENDPOINT, json={"original": {}, "updated": {}})
    assert resp.status_code == 401


def test_feedback_accepted_and_computes_diff(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(db, "get_job", lambda job_id: None)  # job expired — still accepted

    captured: dict = {}
    monkeypatch.setattr(db, "create_feedback", lambda **kw: captured.update(kw))

    resp = client.post(
        ENDPOINT,
        headers={"X-API-Key": VALID_KEY},
        json={
            "original": {"name": "Jane", "skills": ["ICU"]},
            "updated": {"name": "Jane RN", "skills": ["ICU", "CCRN"]},
            "profile_id": "gig-1",
        },
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["job_id"] == "JOB123"
    assert body["changed"] is True
    assert set(body["changed_fields"]) == {"name", "skills[1]"}
    assert body["feedback_id"]
    # Persisted under the authenticated company, with the original job linkage.
    assert captured["company_id"] == "acme-1"
    assert captured["job_id"] == "JOB123"
    assert captured["profile_id"] == "gig-1"


def test_feedback_changed_flag_derived_false_when_identical(monkeypatch):
    _authenticate(monkeypatch)
    monkeypatch.setattr(db, "get_job", lambda job_id: None)
    monkeypatch.setattr(db, "create_feedback", lambda **kw: None)

    payload = {"a": 1, "b": ["x"]}
    resp = client.post(
        ENDPOINT,
        headers={"X-API-Key": VALID_KEY},
        json={"original": payload, "updated": payload},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["changed"] is False
    assert body["changed_fields"] == []


def test_feedback_rejects_job_owned_by_other_company(monkeypatch):
    _authenticate(monkeypatch, company_id="acme-1")
    monkeypatch.setattr(db, "get_job", lambda job_id: {"company_id": "someone-else"})

    resp = client.post(
        ENDPOINT,
        headers={"X-API-Key": VALID_KEY},
        json={"original": {}, "updated": {}},
    )
    assert resp.status_code == 404


def test_feedback_validation_error_when_fields_missing(monkeypatch):
    _authenticate(monkeypatch)
    resp = client.post(ENDPOINT, headers={"X-API-Key": VALID_KEY}, json={"original": {}})
    assert resp.status_code == 422
