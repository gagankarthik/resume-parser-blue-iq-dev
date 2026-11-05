"""
Admin / product-platform endpoints (gated by X-Admin-Token).

Consumed server-to-server by the product platform (resume-parser-ui-blue-iq-dev)
for company onboarding, API-key management, and usage/token stats. Never called
from a browser directly - the platform's backend holds the admin token.

  POST   /api/v1/admin/companies                      create a company (onboarding)
  GET    /api/v1/admin/companies                      list companies
  GET    /api/v1/admin/companies/{company_id}         get one company
  POST   /api/v1/admin/companies/{company_id}/keys    issue an API key (raw shown once)
  GET    /api/v1/admin/companies/{company_id}/keys    list a company's keys (no raw)
  POST   /api/v1/admin/keys/{key_hash}/revoke         revoke a key
  GET    /api/v1/admin/companies/{company_id}/usage   usage + token stats
  GET    /api/v1/admin/companies/{company_id}/logs    recent activity (per-key attributed)
"""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import _evict_key_cache, require_admin
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.url_validator import UnsafeWebhookURLError
from app.db import dynamodb as db
from app.models.schemas.api import WEBHOOK_EVENTS
from app.services.application import usage as usage_service
from app.services.application.companies import issue_api_key, new_company_id
from app.services.application.webhooks import register_webhook

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], tags=["Admin"])
log = get_logger(__name__)


# -- Schemas -------------------------------------------------------------------

class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    plan: str = "free"


# Whitelist serializer - NEVER leak password_hash (or other internal fields).
_PUBLIC_FIELDS = ("company_id", "name", "email", "plan", "status", "created_at", "active_key_count")


def _public(company: dict) -> dict:
    return {k: company[k] for k in _PUBLIC_FIELDS if k in company}


# -- Companies -----------------------------------------------------------------

@router.post("/companies", status_code=201, summary="Create a company (onboarding)")
async def create_company(payload: CompanyCreate) -> dict:
    if db.get_company_by_email(payload.email):
        raise api_error(422, ErrorCode.INVALID_REQUEST, "A company with this email already exists")

    company_id = new_company_id(payload.name)
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
    status='disabled', which both auth paths (API key and account session) then
    reject (the org's keys stop working until it is reactivated)."""
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
    never contain resume content - only operational metadata."""
    _, since = usage_service.clamp_window(days)
    limit = max(1, min(limit, 500))
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


# -- API keys ------------------------------------------------------------------

@router.post("/companies/{company_id}/keys", status_code=201, summary="Issue an API key")
async def issue_key(company_id: str) -> dict:
    if not db.get_company(company_id):
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")

    issued = issue_api_key(company_id)
    log.info("api_key_issued", company_id=company_id, key_prefix=issued["key_prefix"])
    # api_key is returned ONCE - never retrievable again.
    return {
        "api_key": issued["api_key"],
        "key_prefix": issued["key_prefix"],
        "company_id": company_id,
        "status": "active",
        "created_at": issued["created_at"],
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


# -- Usage / stats -------------------------------------------------------------

@router.get("/companies/{company_id}/usage", summary="Usage & token stats")
async def usage(company_id: str, days: int = 30) -> dict:
    return await asyncio.to_thread(usage_service.company_usage, company_id, days)


# -- Platform-wide stats (admin overview) --------------------------------------

@router.get("/stats", summary="Platform-wide usage & company stats")
async def platform_stats(days: int = 30) -> dict:
    """Aggregate usage across ALL companies for the admin overview.

    Two queries per company, so it runs off the event loop.
    """
    return await asyncio.to_thread(usage_service.platform_stats, days)


# -- Webhooks ------------------------------------------------------------------
# Same store the API-key-scoped /webhooks surface uses; here it is managed
# server-to-server by the dashboard, scoped to an explicit company_id.

class WebhookCreate(BaseModel):
    url: str
    events: list[str] = Field(default_factory=list)


@router.get("/companies/{company_id}/webhooks", summary="List a company's webhooks")
async def list_webhooks(company_id: str) -> list[dict]:
    if not db.get_company(company_id):
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    # Never return hmac_secret - it is shown only once at creation.
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

    # Unlike the API-key route (which rejects unknown events), the dashboard route
    # drops them and only requires one valid event to remain.
    events = [e for e in payload.events if e in WEBHOOK_EVENTS]
    if not events:
        raise api_error(422, ErrorCode.INVALID_REQUEST, "Select at least one valid event")

    try:
        hook = await asyncio.to_thread(register_webhook, company_id, payload.url, events)
    except UnsafeWebhookURLError as exc:
        raise api_error(422, ErrorCode.INVALID_REQUEST, str(exc)) from exc
    log.info("webhook_created", company_id=company_id, webhook_id=hook["webhook_id"])
    # hmac_secret is returned ONCE - never retrievable again.
    return {
        k: hook[k]
        for k in ("webhook_id", "url", "events", "hmac_secret", "status", "created_at")
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
