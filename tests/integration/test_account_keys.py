"""Account API keys: the optional `name` label was accepted and silently dropped (C5)."""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.account as account
from app.core.config import get_settings
from app.core.security import create_account_token
from app.main import app

client = TestClient(app)


def test_key_label_is_persisted_and_returned(monkeypatch):
    token = create_account_token("acme-1", get_settings().auth_secret)
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)
    stored: list[dict] = []
    def _create(**kw):
        stored.append(kw)
        return {**kw, "status": "active", "created_at": "2026-09-15T00:00:00+00:00"}

    monkeypatch.setattr(account.db, "create_api_key", _create)
    monkeypatch.setattr(account.db, "list_api_keys_for_company", lambda company_id: [
        {**stored[0], "status": "active", "created_at": "2026-09-15T00:00:00+00:00"}])

    created = client.post("/api/v1/account/keys", json={"name": "CI pipeline"},
                          headers={"Authorization": f"Bearer {token}"})

    assert created.status_code == 201
    assert created.json()["name"] == "CI pipeline"
    assert stored[0]["name"] == "CI pipeline"

    listed = client.get("/api/v1/account/keys", headers={"Authorization": f"Bearer {token}"})
    assert listed.json()[0]["name"] == "CI pipeline"
