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
"""

import re
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import require_admin
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.security import generate_api_key, key_display_prefix
from app.db import dynamodb as db

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
    return db.list_companies()


# Declared before /companies/{company_id} so "lookup" isn't captured as an id.
@router.get("/companies/lookup", summary="Find a company by email")
async def lookup_company(email: str) -> dict:
    company = db.get_company_by_email(email)
    if not company:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    return company


@router.get("/companies/{company_id}", summary="Get a company")
async def get_company(company_id: str) -> dict:
    company = db.get_company(company_id)
    if not company:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Company not found")
    keys = db.list_api_keys_for_company(company_id)
    company["active_key_count"] = sum(1 for k in keys if k.get("status") == "active")
    return company


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
