"""Webhook registration returns the timestamp that was actually stored (C2).

Both the admin and the API-key route built `created_at` a second time after the
write, so the response never matched the stored record.
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
from app.api.v1.endpoints import admin
from app.db import webhooks as webhooks_repo
from app.main import app

PUBLIC_URL = "https://93.184.216.34/hooks/resume"


def _capture_puts(monkeypatch) -> list[dict]:
    items: list[dict] = []

    class _Table:
        def put_item(self, Item):  # noqa: N803 - boto3 naming
            items.append(Item)

    class _Resource:
        def Table(self, name):  # noqa: N802 - boto3 naming
            return _Table()

    monkeypatch.setattr(webhooks_repo, "_get_dynamodb", lambda settings=None: _Resource())
    return items


async def test_admin_registration_returns_the_stored_created_at(monkeypatch):
    items = _capture_puts(monkeypatch)
    monkeypatch.setattr(admin.db, "get_company", lambda company_id: {"company_id": company_id})

    out = await admin.create_webhook(
        "acme-1", admin.WebhookCreate(url=PUBLIC_URL, events=["parse.completed", "bogus"]))

    assert out["created_at"] == items[0]["created_at"]
    assert out["events"] == ["parse.completed"]           # admin still filters
    assert out["hmac_secret"] == items[0]["hmac_secret"]


def test_api_key_registration_returns_the_stored_created_at(monkeypatch):
    items = _capture_puts(monkeypatch)
    monkeypatch.setattr(deps, "_get_cached_api_key",
                        lambda key_hash: {"company_id": "acme-1", "status": "active"})
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: True)

    resp = TestClient(app).post(
        "/api/v1/webhooks", headers={"X-API-Key": "rp_live_" + "a" * 40},
        json={"url": PUBLIC_URL, "events": ["parse.failed"]},
    )

    assert resp.status_code == 201
    assert resp.json()["created_at"] == items[0]["created_at"]
