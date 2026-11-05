"""
Account-scoped self-serve endpoints (Bearer session token).

Everything here is scoped to the authenticated account's company_id - a user can
only see and manage their own keys and usage.

  GET    /api/v1/account/keys                 list my keys
  POST   /api/v1/account/keys                 issue a key (raw shown once)
  POST   /api/v1/account/keys/{hash}/revoke   revoke one of my keys
  GET    /api/v1/account/usage                my usage + token stats
"""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.dependencies import _evict_key_cache, get_current_account
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.application import usage as usage_service
from app.services.application.companies import issue_api_key

router = APIRouter(prefix="/account", tags=["Account"])
log = get_logger(__name__)


class KeyCreate(BaseModel):
    name: str = Field(default="", max_length=80, description="Optional label to tell keys apart")


@router.get("/keys", summary="List my API keys")
async def list_keys(company_id: str = Depends(get_current_account)) -> list[dict]:
    keys = db.list_api_keys_for_company(company_id)
    return [
        {
            "key_hash": k.get("key_hash"),
            "key_prefix": k.get("key_prefix"),
            "name": k.get("name", ""),
            "status": k.get("status"),
            "created_at": k.get("created_at"),
        }
        for k in keys
    ]


@router.post("/keys", status_code=201, summary="Issue an API key")
async def create_key(payload: KeyCreate, company_id: str = Depends(get_current_account)) -> dict:
    issued = issue_api_key(company_id, name=payload.name)
    log.info("account_key_issued", company_id=company_id, key_prefix=issued["key_prefix"])
    return {
        "api_key": issued["api_key"],  # shown once
        "key_prefix": issued["key_prefix"],
        "name": payload.name,
        "status": "active",
        "created_at": issued["created_at"],
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
    return await asyncio.to_thread(usage_service.company_usage, company_id, days)
