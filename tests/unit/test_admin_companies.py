"""
Admin company management: update (plan/status), per-org logs, and the
deactivated-company enforcement in the API-key auth path.
"""

import pytest
from fastapi import HTTPException

from app.api import dependencies
from app.api.v1.endpoints import admin

# ── PATCH /admin/companies/{id} ───────────────────────────────────────────────

async def test_update_company_changes_status(monkeypatch):
    captured: dict = {}

    def fake_update(cid, updates):
        captured["cid"], captured["updates"] = cid, updates
        return {"company_id": cid, "name": "Acme", "email": "a@x.com",
                "plan": "pro", "status": updates.get("status") or "active"}

    monkeypatch.setattr(admin.db, "update_company", fake_update)
    monkeypatch.setattr(admin.db, "list_api_keys_for_company", lambda cid: [{"status": "active"}])

    out = await admin.update_company("acme", admin.CompanyUpdate(status="disabled"))
    assert out["status"] == "disabled"
    assert out["active_key_count"] == 1
    assert captured["updates"]["status"] == "disabled"
    # Whitelist serializer must not leak internal fields.
    assert "password_hash" not in out


async def test_update_company_rejects_bad_status():
    with pytest.raises(HTTPException) as ei:
        await admin.update_company("acme", admin.CompanyUpdate(status="paused"))
    assert ei.value.status_code == 422


async def test_update_company_requires_a_field():
    with pytest.raises(HTTPException) as ei:
        await admin.update_company("acme", admin.CompanyUpdate())
    assert ei.value.status_code == 422


async def test_update_company_not_found(monkeypatch):
    monkeypatch.setattr(admin.db, "update_company", lambda cid, updates: None)
    with pytest.raises(HTTPException) as ei:
        await admin.update_company("nope", admin.CompanyUpdate(plan="pro"))
    assert ei.value.status_code == 404


# ── GET /admin/companies/{id}/logs ────────────────────────────────────────────

async def test_company_logs_shapes_sorts_and_coerces(monkeypatch):
    logs = [
        {"job_id": "1", "timestamp": "2026-06-07T10:00:00", "file_type": "pdf",
         "status": "completed", "duration_ms": "1000", "ocr_used": False,
         "ai_tokens_used": "500", "error_code": ""},
        {"job_id": "2", "timestamp": "2026-06-08T10:00:00", "file_type": "docx",
         "status": "failed", "duration_ms": 0, "ocr_used": True,
         "ai_tokens_used": 0, "error_code": "PARSE_FAILED"},
    ]
    monkeypatch.setattr(admin.db, "get_audit_logs_for_company", lambda cid, since: logs)

    out = await admin.company_logs("acme", days=30, limit=10)
    assert [r["job_id"] for r in out] == ["2", "1"]      # most recent first
    assert out[1]["ai_tokens_used"] == 500               # coerced to int
    assert out[0]["error_code"] == "PARSE_FAILED"


async def test_company_logs_respects_limit(monkeypatch):
    logs = [{"job_id": str(i), "timestamp": f"2026-06-0{i}T10:00:00"} for i in range(1, 6)]
    monkeypatch.setattr(admin.db, "get_audit_logs_for_company", lambda cid, since: logs)
    out = await admin.company_logs("acme", days=30, limit=2)
    assert len(out) == 2


# ── Deactivation enforcement in API-key auth ──────────────────────────────────

def test_company_is_active_blocks_disabled(monkeypatch):
    dependencies._COMPANY_STATUS_CACHE.clear()
    monkeypatch.setattr(dependencies.db, "get_company", lambda cid: {"status": "disabled"})
    assert dependencies._company_is_active("acme") is False


def test_company_is_active_allows_active(monkeypatch):
    dependencies._COMPANY_STATUS_CACHE.clear()
    monkeypatch.setattr(dependencies.db, "get_company", lambda cid: {"status": "active"})
    assert dependencies._company_is_active("acme") is True


def test_company_is_active_defaults_active_when_missing(monkeypatch):
    # A missing company must never lock out an otherwise-valid key.
    dependencies._COMPANY_STATUS_CACHE.clear()
    monkeypatch.setattr(dependencies.db, "get_company", lambda cid: None)
    assert dependencies._company_is_active("ghost") is True
