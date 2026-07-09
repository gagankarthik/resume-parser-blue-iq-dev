"""
Admin / product-platform endpoints (gated by X-Admin-Token).

Consumed server-to-server by the product platform (resume-parser-ui-blue-iq-dev)
for company onboarding, API-key management, and usage/token stats. Never called
from a browser directly — the platform's backend holds the admin token.

  POST   /api/v1/admin/companies                      create a company (onboarding)
  GET    /api/v1/admin/companies                      list companies
  GET    /api/v1/admin/companies/{company_id}         get one company
  POST   /api/v1/admin/companies/{company_id}/keys    issue an API key (raw shown once)
  GET    /api/v1/admin/companies/{company_id}/keys    list a company's keys (no raw)
  POST   /api/v1/admin/keys/{key_hash}/revoke         revoke a key
  GET    /api/v1/admin/companies/{company_id}/usage   usage + token stats
  GET    /api/v1/admin/companies/{company_id}/logs    recent activity (per-key attributed)
"""

import re
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from ulid import ULID

from app.api.dependencies import _evict_key_cache, require_admin
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.security import (
    generate_api_key,
    generate_webhook_secret,
    key_display_prefix,
)
from app.core.url_validator import UnsafeWebhookURLError, validate_webhook_url
from app.db import dynamodb as db

_VALID_EVENTS = {"parse.completed", "parse.failed", "batch.completed"}

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], tags=["Admin"])
log = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    plan: str = "free"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "company")[:40]


# Whitelist serializer — NEVER leak password_hash (or other internal fields).
_PUBLIC_FIELDS = ("company_id", "name", "email", "plan", "status", "created_at", "active_key_count")


def _public(company: dict) -> dict:
    return {k: company[k] for k in _PUBLIC_FIELDS if k in company}


# ── Companies ─────────────────────────────────────────────────────────────────

@router.post("/companies", status_code=201, summary="Create a company (onboarding)")
async def create_company(payload: CompanyCreate) -> dict:
    if db.get_company_by_email(payload.email):
        raise api_error(422, ErrorCode.INVALID_REQUEST, "A company with this email already exists")

    company_id = f"{_slug(payload.name)}-{secrets.token_hex(3)}"
    db.create_company(
        company_id=company_id,
        name=payload.name,
        email=str(payload.email),
        plan=payload.plan,
    )
    log.info("company_created", company_id=company_id)
    return {
        "company_id": company_id,
        "name": payload.name,
        "email": str(payload.email),
        "plan": payload.plan,
        "status": "active",
    }


@router.get("/companies", summary="List companies")
async def list_companies() -> list[dict]:
    return [_public(c) for c in db.list_companies()]


# Declared before /companies/{company_id} so "lookup" isn't captured as an id.
@router.get("/companies/lookup", summary="Find a company by email")
async def lookup_company(email: str) -> dict:
    company = db.get_company_by_email(email)
    if not company:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    return _public(company)


@router.get("/companies/{company_id}", summary="Get a company")
async def get_company(company_id: str) -> dict:
    company = db.get_company(company_id)
    if not company:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    keys = db.list_api_keys_for_company(company_id)
    company["active_key_count"] = sum(1 for k in keys if k.get("status") == "active")
    return _public(company)


class CompanyUpdate(BaseModel):
    plan: str | None = Field(default=None, min_length=1, max_length=40)
    status: str | None = Field(default=None)


_VALID_COMPANY_STATUS = {"active", "disabled"}


@router.patch("/companies/{company_id}", summary="Update a company (plan / status)")
async def update_company(company_id: str, payload: CompanyUpdate) -> dict:
    """Activate/deactivate a company or change its plan. Deactivating sets
    status='disabled', which the API-key auth path then rejects (the org's keys
    stop working until it is reactivated)."""
    if payload.plan is None and payload.status is None:
        raise api_error(422, ErrorCode.INVALID_REQUEST, "Provide plan and/or status to update")
    if payload.status is not None and payload.status not in _VALID_COMPANY_STATUS:
        raise api_error(
            422, ErrorCode.INVALID_REQUEST,
            f"status must be one of {sorted(_VALID_COMPANY_STATUS)}",
        )

    updated = db.update_company(company_id, {"plan": payload.plan, "status": payload.status})
    if not updated:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")

    log.info("company_updated", company_id=company_id, plan=payload.plan, status=payload.status)
    keys = db.list_api_keys_for_company(company_id)
    updated["active_key_count"] = sum(1 for k in keys if k.get("status") == "active")
    return _public(updated)


@router.get("/companies/{company_id}/logs", summary="Recent activity logs")
async def company_logs(company_id: str, days: int = 30, limit: int = 100) -> list[dict]:
    """Recent audit-log entries for a company (most recent first). Audit records
    never contain résumé content — only operational metadata."""
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 500))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    logs = db.get_audit_logs_for_company(company_id, since)
    logs.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
    return [
        {
            "job_id": r.get("job_id"),
            "timestamp": r.get("timestamp"),
            "file_type": r.get("file_type"),
            "status": r.get("status"),
            "duration_ms": int(r.get("duration_ms", 0) or 0),
            "ocr_used": bool(r.get("ocr_used")),
            "ai_tokens_used": int(r.get("ai_tokens_used", 0) or 0),
            "error_code": r.get("error_code", ""),
            # Key that produced the job, so the platform can break usage down
            # per key. Empty for legacy/unattributed records.
            "key_hash": r.get("key_hash", ""),
            "key_prefix": r.get("key_prefix", ""),
        }
        for r in logs[:limit]
    ]


# ── API keys ──────────────────────────────────────────────────────────────────

@router.post("/companies/{company_id}/keys", status_code=201, summary="Issue an API key")
async def issue_key(company_id: str) -> dict:
    if not db.get_company(company_id):
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")

    raw_key, key_hash = generate_api_key()
    prefix = key_display_prefix(raw_key)
    db.create_api_key(key_hash=key_hash, key_prefix=prefix, company_id=company_id)
    log.info("api_key_issued", company_id=company_id, key_prefix=prefix)
    # api_key is returned ONCE — never retrievable again.
    return {
        "api_key": raw_key,
        "key_prefix": prefix,
        "company_id": company_id,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.get("/companies/{company_id}/keys", summary="List a company's keys")
async def list_keys(company_id: str) -> list[dict]:
    keys = db.list_api_keys_for_company(company_id)
    # Never expose key_hash (sensitive) to the dashboard.
    return [
        {
            "key_prefix": k.get("key_prefix"),
            "company_id": k.get("company_id"),
            "status": k.get("status"),
            "created_at": k.get("created_at"),
            # opaque handle for revoke (hash is required by the revoke endpoint)
            "key_hash": k.get("key_hash"),
        }
        for k in keys
    ]


@router.post("/keys/{key_hash}/revoke", summary="Revoke an API key")
async def revoke_key(key_hash: str) -> dict:
    existing = db.get_api_key(key_hash)
    if not existing:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Key not found")
    db.revoke_api_key(key_hash)
    # Evict the in-memory auth cache so the revoked key stops authenticating on
    # THIS instance immediately (other warm instances still expire within the TTL).
    _evict_key_cache(key_hash)
    return {"key_hash": key_hash, "status": "revoked"}


# ── Usage / stats ─────────────────────────────────────────────────────────────

@router.get("/companies/{company_id}/usage", summary="Usage & token stats")
async def usage(company_id: str, days: int = 30) -> dict:
    days = max(1, min(days, 365))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    logs = db.get_audit_logs_for_company(company_id, since)

    total = len(logs)
    completed = sum(1 for r in logs if r.get("status") == "completed")
    failed = sum(1 for r in logs if r.get("status") == "failed")
    tokens = sum(int(r.get("ai_tokens_used", 0) or 0) for r in logs)
    ocr_jobs = sum(1 for r in logs if r.get("ocr_used"))
    durations = [int(r.get("duration_ms", 0) or 0) for r in logs if r.get("duration_ms")]
    avg_duration = round(sum(durations) / len(durations)) if durations else 0

    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"jobs": 0, "tokens": 0})
    by_file_type: dict[str, int] = defaultdict(int)
    for r in logs:
        day = str(r.get("timestamp", ""))[:10]
        if day:
            by_day[day]["jobs"] += 1
            by_day[day]["tokens"] += int(r.get("ai_tokens_used", 0) or 0)
        by_file_type[str(r.get("file_type", "unknown"))] += 1

    return {
        "company_id": company_id,
        "window_days": days,
        "totals": {
            "jobs": total,
            "completed": completed,
            "failed": failed,
            "ocr_jobs": ocr_jobs,
            "tokens_used": tokens,
            "avg_duration_ms": avg_duration,
        },
        "by_day": [
            {"date": d, "jobs": v["jobs"], "tokens": v["tokens"]}
            for d, v in sorted(by_day.items())
        ],
        "by_file_type": dict(by_file_type),
    }


# ── Platform-wide stats (admin overview) ──────────────────────────────────────

@router.get("/stats", summary="Platform-wide usage & company stats")
async def platform_stats(days: int = 30) -> dict:
    """Aggregate usage across ALL companies for the admin overview.

    Sums each company's audit logs over the window (using the per-company GSI),
    so the cost scales with the number of companies, not a full table scan.
    Returns headline totals, a by-day series, and a per-company breakdown sorted
    by volume (heaviest consumers first).
    """
    days = max(1, min(days, 365))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    companies = db.list_companies()

    totals = {"jobs": 0, "completed": 0, "failed": 0, "ocr_jobs": 0, "tokens_used": 0}
    duration_sum = 0
    duration_count = 0
    active_companies = 0
    active_keys = 0
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"jobs": 0, "tokens": 0})
    per_company: list[dict] = []

    for c in companies:
        cid = c.get("company_id")
        if not cid:
            continue
        if c.get("status", "active") == "active":
            active_companies += 1

        keys = db.list_api_keys_for_company(cid)
        active_keys += sum(1 for k in keys if k.get("status") == "active")

        logs = db.get_audit_logs_for_company(cid, since)
        c_tokens = sum(int(r.get("ai_tokens_used", 0) or 0) for r in logs)
        last_active = max((str(r.get("timestamp", "")) for r in logs), default="")

        totals["jobs"] += len(logs)
        totals["completed"] += sum(1 for r in logs if r.get("status") == "completed")
        totals["failed"] += sum(1 for r in logs if r.get("status") == "failed")
        totals["ocr_jobs"] += sum(1 for r in logs if r.get("ocr_used"))
        totals["tokens_used"] += c_tokens

        for r in logs:
            d = int(r.get("duration_ms", 0) or 0)
            if d:
                duration_sum += d
                duration_count += 1
            day = str(r.get("timestamp", ""))[:10]
            if day:
                by_day[day]["jobs"] += 1
                by_day[day]["tokens"] += int(r.get("ai_tokens_used", 0) or 0)

        per_company.append({
            "company_id": cid,
            "name": c.get("name") or cid,
            "email": c.get("email", ""),
            "plan": c.get("plan", ""),
            "status": c.get("status", "active"),
            "jobs": len(logs),
            "tokens": c_tokens,
            "active_keys": sum(1 for k in keys if k.get("status") == "active"),
            "last_active": last_active,
        })

    totals["avg_duration_ms"] = round(duration_sum / duration_count) if duration_count else 0
    per_company.sort(key=lambda x: (x["jobs"], x["tokens"]), reverse=True)

    return {
        "window_days": days,
        "companies": {"total": len(companies), "active": active_companies},
        "active_keys": active_keys,
        "totals": totals,
        "by_day": [
            {"date": d, "jobs": v["jobs"], "tokens": v["tokens"]}
            for d, v in sorted(by_day.items())
        ],
        "companies_list": per_company,
    }


# ── Webhooks ──────────────────────────────────────────────────────────────────
# Same store the API-key-scoped /webhooks surface uses; here it is managed
# server-to-server by the dashboard, scoped to an explicit company_id.

class WebhookCreate(BaseModel):
    url: str
    events: list[str] = Field(default_factory=list)


@router.get("/companies/{company_id}/webhooks", summary="List a company's webhooks")
async def list_webhooks(company_id: str) -> list[dict]:
    if not db.get_company(company_id):
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    # Never return hmac_secret — it is shown only once at creation.
    return [
        {
            "webhook_id": h.get("webhook_id"),
            "url": h.get("url"),
            "events": h.get("events", []),
            "status": h.get("status", "active"),
            "created_at": h.get("created_at", ""),
        }
        for h in db.list_webhooks(company_id)
    ]


@router.post("/companies/{company_id}/webhooks", status_code=201, summary="Register a webhook")
async def create_webhook(company_id: str, payload: WebhookCreate) -> dict:
    if not db.get_company(company_id):
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")

    events = [e for e in payload.events if e in _VALID_EVENTS]
    if not events:
        raise api_error(422, ErrorCode.INVALID_REQUEST, "Select at least one valid event")

    # SSRF guard: scheme + DNS resolution must be public (https-only in prod).
    try:
        validate_webhook_url(payload.url)
    except UnsafeWebhookURLError as exc:
        raise api_error(422, ErrorCode.INVALID_REQUEST, str(exc))

    webhook_id = str(ULID())
    secret = generate_webhook_secret()
    db.create_webhook(
        webhook_id=webhook_id,
        company_id=company_id,
        url=payload.url,
        hmac_secret=secret,
        events=events,
    )
    log.info("webhook_created", company_id=company_id, webhook_id=webhook_id)
    # hmac_secret is returned ONCE — never retrievable again.
    return {
        "webhook_id": webhook_id,
        "url": payload.url,
        "events": events,
        "hmac_secret": secret,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.delete(
    "/companies/{company_id}/webhooks/{webhook_id}",
    status_code=204,
    summary="Delete a webhook",
)
async def delete_webhook(company_id: str, webhook_id: str) -> None:
    if not db.get_webhook(company_id, webhook_id):
        raise api_error(404, ErrorCode.WEBHOOK_NOT_FOUND, "Webhook not found")
    db.delete_webhook(company_id, webhook_id)
