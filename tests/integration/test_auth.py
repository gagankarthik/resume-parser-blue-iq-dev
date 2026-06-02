"""
Auth rejection tests — no DynamoDB needed (key is rejected before lookup).
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_missing_api_key_returns_401():
    resp = client.post("/api/v1/resume/parse")
    assert resp.status_code == 401


def test_invalid_api_key_returns_401(monkeypatch):
    # Patch db.get_api_key to return None (key not found)
    import app.db.dynamodb as db
    monkeypatch.setattr(db, "get_api_key", lambda key_hash: None)

    resp = client.post(
        "/api/v1/resume/parse",
        headers={"X-API-Key": "rp_live_invalid"},
        files={"file": ("test.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert resp.status_code == 401
