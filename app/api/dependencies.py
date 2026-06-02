"""
FastAPI dependency injection — authentication, caching, rate limiting.

API key cache:
  In-memory dict with 5-minute TTL per Lambda instance.
  Reduces DynamoDB reads from 1/request to ~1/300 requests per instance.
  Revoked keys remain valid for up to TTL — acceptable trade-off.
  Cache is per-process; Lambda cold starts naturally clear it.

Rate limit headers returned on every authenticated response:
  X-RateLimit-Limit-Minute
  X-RateLimit-Remaining-Minute
  X-RateLimit-Limit-Day
  X-RateLimit-Remaining-Day
"""

import time

from fastapi import Depends, Response
from fastapi.security import APIKeyHeader

from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.rate_limiter import check_and_increment
from app.core.security import hash_api_key, key_display_prefix, validate_key_format
from app.db import dynamodb as db

log = get_logger(__name__)

_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── In-memory API key cache ───────────────────────────────────────────────────
_KEY_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


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


def _evict_key_cache(key_hash: str) -> None:
    """Call after revoking a key to evict it immediately."""
    _KEY_CACHE.pop(key_hash, None)


# ── Dependencies ──────────────────────────────────────────────────────────────

def get_api_key_record(api_key: str = Depends(_api_key_scheme)) -> dict:
    """
    Validate X-API-Key header.
    Order: presence → format → DynamoDB lookup → status.
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

    record["key_hash"] = key_hash
    return record


def enforce_rate_limit(
    response: Response,
    record: dict = Depends(get_api_key_record),
) -> dict:
    """
    Check sliding-window rate limits and set X-RateLimit-* response headers.
    Headers are set even when the request is rejected so clients can backoff.
    """
    limit_min = int(record.get("rate_limit_per_minute", 30))
    limit_day = int(record.get("rate_limit_per_day", 1000))

    allowed, reason, rem_min, rem_day = check_and_increment(
        key_hash=record["key_hash"],
        limit_per_minute=limit_min,
        limit_per_day=limit_day,
    )

    response.headers["X-RateLimit-Limit-Minute"]     = str(limit_min)
    response.headers["X-RateLimit-Remaining-Minute"] = str(max(rem_min, 0))
    response.headers["X-RateLimit-Limit-Day"]        = str(limit_day)
    response.headers["X-RateLimit-Remaining-Day"]    = str(max(rem_day, 0))

    if not allowed:
        log.warning(
            "rate_limit_exceeded",
            company_id=record.get("company_id"),
            reason=reason,
        )
        raise api_error(429, ErrorCode.RATE_LIMIT_EXCEEDED, reason)

    return record
