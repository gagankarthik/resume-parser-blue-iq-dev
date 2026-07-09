"""
Account-scoped self-serve endpoints (Bearer session token).

Everything here is scoped to the authenticated account's company_id — a user can
only see and manage their own keys and usage.

  GET    /api/v1/account/keys                 list my keys
  POST   /api/v1/account/keys                 issue a key (raw shown once)
  POST   /api/v1/account/keys/{hash}/revoke   revoke one of my keys
  GET    /api/v1/account/usage                my usage + token stats
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.dependencies import _evict_key_cache, get_current_account
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.security import generate_api_key, key_display_prefix
from app.db import dynamodb as db

router = APIRouter(prefix="/account", tags=["Account"])
log = get_logger(__name__)


class KeyCreate(BaseModel):
    name: str = Field(default="", max_length=80)


@router.get("/keys", summary="List my API keys")
async def list_keys(company_id: str = Depends(get_current_account)) -> list[dict]:
    keys = db.list_api_keys_for_company(company_id)
    return [
        {
            "key_hash": k.get("key_hash"),
            "key_prefix": k.get("key_prefix"),
            "status": k.get("status"),
            "created_at": k.get("created_at"),
        }
        for k in keys
    ]


@router.post("/keys", status_code=201, summary="Issue an API key")
async def create_key(payload: KeyCreate, company_id: str = Depends(get_current_account)) -> dict:
    raw_key, key_hash = generate_api_key()
    prefix = key_display_prefix(raw_key)
    db.create_api_key(key_hash=key_hash, key_prefix=prefix, company_id=company_id)
    log.info("account_key_issued", company_id=company_id, key_prefix=prefix)
    return {
        "api_key": raw_key,  # shown once
        "key_prefix": prefix,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.post("/keys/{key_hash}/revoke", summary="Revoke one of my keys")
async def revoke_key(key_hash: str, company_id: str = Depends(get_current_account)) -> dict:
    existing = db.get_api_key(key_hash)
    if not existing or existing.get("company_id") != company_id:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Key not found")
    db.revoke_api_key(key_hash)
    # Evict the in-memory auth cache so the revoked key stops authenticating on
    # THIS instance immediately (other warm instances still expire within the TTL).
    _evict_key_cache(key_hash)
    return {"key_hash": key_hash, "status": "revoked"}


@router.get("/usage", summary="My usage & token stats")
async def usage(days: int = 30, company_id: str = Depends(get_current_account)) -> dict:
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
        "by_day": [{"date": d, "jobs": v["jobs"], "tokens": v["tokens"]} for d, v in sorted(by_day.items())],
        "by_file_type": dict(by_file_type),
    }
