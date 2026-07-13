"""
FastAPI dependency injection - authentication and caching.

API key cache:
  In-memory dict with 5-minute TTL per Lambda instance.
  Reduces DynamoDB reads from 1/request to ~1/300 requests per instance.
  Revoked keys remain valid for up to TTL - acceptable trade-off.
  Cache is per-process; Lambda cold starts naturally clear it.
"""

import hmac
import time

from fastapi import Depends
from fastapi.security import APIKeyHeader

from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.rate_limit import check as check_rate_limit
from app.core.security import (
    hash_api_key,
    key_display_prefix,
    validate_key_format,
    verify_account_token,
)
from app.db import dynamodb as db

log = get_logger(__name__)

_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_admin_token_scheme = APIKeyHeader(name="X-Admin-Token", auto_error=False)
_bearer_scheme = APIKeyHeader(name="Authorization", auto_error=False)


def get_current_account(authorization: str = Depends(_bearer_scheme)) -> str:
    """Resolve the self-serve account (company_id) from a Bearer session token."""
    if not authorization:
        raise api_error(401, ErrorCode.MISSING_API_KEY, "Missing Authorization header")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    company_id = verify_account_token(token, get_settings().auth_secret)
    if not company_id:
        raise api_error(401, ErrorCode.INVALID_API_KEY, "Invalid or expired session")
    return company_id


# -- In-memory API key cache ---------------------------------------------------
_KEY_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes

# Company status is cached separately with a shorter TTL so an admin
# deactivation takes effect quickly (within _COMPANY_TTL_SECONDS) without adding
# a DynamoDB read to every authenticated request.
_COMPANY_STATUS_CACHE: dict[str, tuple[str, float]] = {}
_COMPANY_TTL_SECONDS = 60


def _get_cached_api_key(key_hash: str) -> dict | None:
    now = time.time()
    entry = _KEY_CACHE.get(key_hash)
    if entry:
        record, expiry = entry
        if now < expiry:
            return record
        del _KEY_CACHE[key_hash]

    record = db.get_api_key(key_hash)
    if record:
        _KEY_CACHE[key_hash] = (record, now + _CACHE_TTL_SECONDS)
    return record


def _company_is_active(company_id: str) -> bool:
    """True unless the owning company has been deactivated by an admin.

    A missing company (or missing status) defaults to active so legacy keys are
    never locked out by this check.
    """
    now = time.time()
    entry = _COMPANY_STATUS_CACHE.get(company_id)
    if entry and now < entry[1]:
        return entry[0] == "active"

    company = db.get_company(company_id)
    status = (company or {}).get("status", "active")
    _COMPANY_STATUS_CACHE[company_id] = (status, now + _COMPANY_TTL_SECONDS)
    return status == "active"


def _evict_key_cache(key_hash: str) -> None:
    """Call after revoking a key to evict it immediately."""
    _KEY_CACHE.pop(key_hash, None)


# -- Dependencies --------------------------------------------------------------

def get_api_key_record(api_key: str = Depends(_api_key_scheme)) -> dict:
    """
    Validate X-API-Key header.
    Order: presence -> format -> DynamoDB lookup -> status.
    """
    if not api_key:
        raise api_error(401, ErrorCode.MISSING_API_KEY, "Missing X-API-Key header")

    if not validate_key_format(api_key):
        log.warning("auth_bad_format", prefix=api_key[:8])
        raise api_error(401, ErrorCode.INVALID_API_KEY_FORMAT, "Invalid API key format")

    key_hash = hash_api_key(api_key)
    record = _get_cached_api_key(key_hash)

    if not record:
        log.warning("auth_key_not_found", prefix=key_display_prefix(api_key))
        raise api_error(401, ErrorCode.INVALID_API_KEY, "API key not recognised")

    if record.get("status") != "active":
        log.warning(
            "auth_key_revoked",
            prefix=key_display_prefix(api_key),
            company_id=record.get("company_id"),
        )
        raise api_error(403, ErrorCode.REVOKED_API_KEY, "API key has been revoked")

    if not _company_is_active(record["company_id"]):
        log.warning("auth_account_deactivated", company_id=record.get("company_id"))
        raise api_error(403, ErrorCode.ACCOUNT_DEACTIVATED, "This account has been deactivated")

    # Throttle per key AFTER the key is proven valid, so unauthenticated traffic
    # can never consume a tenant's quota.
    check_rate_limit(key_hash, company_id=record["company_id"])

    record["key_hash"] = key_hash
    return record


def require_admin(token: str = Depends(_admin_token_scheme)) -> None:
    """
    Gate the /api/v1/admin/* endpoints with a static admin bearer token
    (X-Admin-Token). Used by the product platform's server, never the browser.
    """
    expected = get_settings().admin_api_token
    if not expected:
        # No token configured -> admin surface is disabled.
        raise api_error(403, ErrorCode.REVOKED_API_KEY, "Admin API is not enabled")
    if not token or not hmac.compare_digest(token, expected):
        log.warning("admin_auth_failed")
        raise api_error(401, ErrorCode.INVALID_API_KEY, "Invalid admin token")
