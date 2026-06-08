"""
Unit test for the platform-wide admin stats aggregation.

Calls the route function directly (the require_admin dependency lives on the
router, so it isn't exercised here) with the DynamoDB layer monkeypatched, to
verify the cross-company aggregation math.
"""

import pytest

from app.api.v1.endpoints import admin


@pytest.fixture
def _stub_db(monkeypatch):
    companies = [
        {"company_id": "acme", "name": "Acme Health", "plan": "pro", "status": "active"},
        {"company_id": "globex", "name": "Globex", "plan": "free", "status": "active"},
        {"company_id": "init", "name": "Initech", "plan": "free", "status": "disabled"},
    ]
    keys = {
        "acme": [{"status": "active"}, {"status": "active"}, {"status": "revoked"}],
        "globex": [{"status": "active"}],
        "init": [],
    }
    logs = {
        "acme": [
            {"status": "completed", "ai_tokens_used": 1000, "ocr_used": True, "duration_ms": 4000, "timestamp": "2026-06-07T10:00:00"},
            {"status": "completed", "ai_tokens_used": 500, "ocr_used": False, "duration_ms": 2000, "timestamp": "2026-06-07T11:00:00"},
            {"status": "failed", "ai_tokens_used": 0, "ocr_used": False, "duration_ms": 0, "timestamp": "2026-06-08T09:00:00"},
        ],
        "globex": [
            {"status": "completed", "ai_tokens_used": 300, "ocr_used": False, "duration_ms": 1000, "timestamp": "2026-06-08T12:00:00"},
        ],
        "init": [],
    }
    monkeypatch.setattr(admin.db, "list_companies", lambda: companies)
    monkeypatch.setattr(admin.db, "list_api_keys_for_company", lambda cid: keys.get(cid, []))
    monkeypatch.setattr(admin.db, "get_audit_logs_for_company", lambda cid, since: logs.get(cid, []))


async def test_platform_stats_aggregates_across_companies(_stub_db):
    out = await admin.platform_stats(days=30)

    assert out["companies"] == {"total": 3, "active": 2}
    assert out["active_keys"] == 3  # 2 (acme) + 1 (globex) + 0 (init)

    t = out["totals"]
    assert t["jobs"] == 4
    assert t["completed"] == 3
    assert t["failed"] == 1
    assert t["ocr_jobs"] == 1
    assert t["tokens_used"] == 1800
    # avg over the 3 non-zero durations (4000, 2000, 1000)
    assert t["avg_duration_ms"] == round((4000 + 2000 + 1000) / 3)

    # by_day series is sorted and split correctly
    by_day = {d["date"]: d for d in out["by_day"]}
    assert by_day["2026-06-07"]["jobs"] == 2
    assert by_day["2026-06-07"]["tokens"] == 1500
    assert by_day["2026-06-08"]["jobs"] == 2  # acme failed + globex completed

    # per-company breakdown sorted by volume (acme heaviest)
    rows = out["companies_list"]
    assert rows[0]["company_id"] == "acme"
    assert rows[0]["jobs"] == 3
    assert rows[0]["tokens"] == 1500
    assert rows[0]["last_active"] == "2026-06-08T09:00:00"
    assert {r["company_id"] for r in rows} == {"acme", "globex", "init"}


async def test_platform_stats_clamps_days(_stub_db):
    assert (await admin.platform_stats(days=9999))["window_days"] == 365
    assert (await admin.platform_stats(days=0))["window_days"] == 1
