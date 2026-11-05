"""Usage rollups over audit-log records - one implementation behind the account
usage view, the admin per-company usage view and the platform-wide stats.

Blocking (DynamoDB queries) - call the loaders off the event loop.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.db import dynamodb as db


def clamp_window(days: int) -> tuple[int, str]:
    """Clamp a look-back window to 1-365 days; return it with its ISO start time."""
    days = max(1, min(days, 365))
    return days, (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _tokens(record: dict) -> int:
    return int(record.get("ai_tokens_used", 0) or 0)


def rollup(logs: list[dict]) -> dict:
    """Totals, a date-sorted by-day series and a by-file-type count for audit records."""
    durations = [d for d in (int(r.get("duration_ms", 0) or 0) for r in logs) if d]
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"jobs": 0, "tokens": 0})
    by_file_type: dict[str, int] = defaultdict(int)
    for r in logs:
        day = str(r.get("timestamp", ""))[:10]
        if day:
            by_day[day]["jobs"] += 1
            by_day[day]["tokens"] += _tokens(r)
        by_file_type[str(r.get("file_type", "unknown"))] += 1

    return {
        "totals": {
            "jobs": len(logs),
            "completed": sum(1 for r in logs if r.get("status") == "completed"),
            "failed": sum(1 for r in logs if r.get("status") == "failed"),
            "ocr_jobs": sum(1 for r in logs if r.get("ocr_used")),
            "tokens_used": sum(_tokens(r) for r in logs),
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        },
        "by_day": [
            {"date": d, "jobs": v["jobs"], "tokens": v["tokens"]}
            for d, v in sorted(by_day.items())
        ],
        "by_file_type": dict(by_file_type),
    }


def company_usage(company_id: str, days: int) -> dict:
    """Usage + token stats for one company over the last `days`."""
    days, since = clamp_window(days)
    logs = db.get_audit_logs_for_company(company_id, since)
    return {"company_id": company_id, "window_days": days, **rollup(logs)}


def platform_stats(days: int) -> dict:
    """Usage across ALL companies for the admin overview.

    Sums each company's audit logs over the window (using the per-company GSI),
    so the cost scales with the number of companies, not a full table scan.
    Returns headline totals, a by-day series, and a per-company breakdown sorted
    by volume (heaviest consumers first).
    """
    days, since = clamp_window(days)
    companies = db.list_companies()

    all_logs: list[dict] = []
    per_company: list[dict] = []
    active_companies = 0
    active_keys = 0

    for c in companies:
        cid = c.get("company_id")
        if not cid:
            continue
        if c.get("status", "active") == "active":
            active_companies += 1

        keys = db.list_api_keys_for_company(cid)
        company_active_keys = sum(1 for k in keys if k.get("status") == "active")
        active_keys += company_active_keys

        logs = db.get_audit_logs_for_company(cid, since)
        all_logs.extend(logs)
        per_company.append({
            "company_id": cid,
            "name": c.get("name") or cid,
            "email": c.get("email", ""),
            "plan": c.get("plan", ""),
            "status": c.get("status", "active"),
            "jobs": len(logs),
            "tokens": sum(_tokens(r) for r in logs),
            "active_keys": company_active_keys,
            "last_active": max((str(r.get("timestamp", "")) for r in logs), default=""),
        })

    per_company.sort(key=lambda x: (x["jobs"], x["tokens"]), reverse=True)
    summary = rollup(all_logs)

    return {
        "window_days": days,
        "companies": {"total": len(companies), "active": active_companies},
        "active_keys": active_keys,
        "totals": summary["totals"],
        "by_day": summary["by_day"],
        "companies_list": per_company,
    }
