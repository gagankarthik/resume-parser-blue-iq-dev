"""
FastAPI dependency injection:
  - API key format validation (before DynamoDB)
  - API key authentication (DynamoDB lookup)
  - Rate limit enforcement (DynamoDB sliding window)
"""

from fastapi import Depends
from fastapi.security import APIKeyHeader

from app.core.exceptions import http_401, http_403, http_429
from app.core.logging import get_logger
from app.core.rate_limiter import check_and_increment
from app.core.security import hash_api_key, key_display_prefix, validate_key_format
from app.db import dynamodb as db

log = get_logger(__name__)

_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_api_key_record(api_key: str = Depends(_api_key_scheme)) -> dict:
    """
    Validate X-API-Key:
      1. Presence check
      2. Format check (rp_live_... pattern) — no DynamoDB call on bad format
      3. DynamoDB lookup
      4. Status check (active / revoked)
    """
    if not api_key:
        raise http_401("Missing X-API-Key header")

    # Format check — reject malformed keys before touching DynamoDB
    if not validate_key_format(api_key):
        log.warning("auth_invalid_key_format", prefix=api_key[:8])
        raise http_401("Invalid API key format")

    key_hash = hash_api_key(api_key)
    record = db.get_api_key(key_hash)

    if not record:
        log.warning("auth_key_not_found", prefix=key_display_prefix(api_key))
        raise http_401("API key not recognised")

    if record.get("status") != "active":
        log.warning(
            "auth_key_revoked",
            prefix=key_display_prefix(api_key),
            company_id=record.get("company_id"),
        )
        raise http_403("API key has been revoked")

    # Attach hash so downstream can use it (e.g. rate limiter) without re-hashing
    record["key_hash"] = key_hash
    return record


def enforce_rate_limit(record: dict = Depends(get_api_key_record)) -> dict:
    """Check rate limits and pass the key record through for downstream use."""
    allowed, reason = check_and_increment(
        key_hash=record["key_hash"],
        limit_per_minute=int(record.get("rate_limit_per_minute", 30)),
        limit_per_day=int(record.get("rate_limit_per_day", 1000)),
    )
    if not allowed:
        log.warning(
            "rate_limit_exceeded",
            company_id=record.get("company_id"),
            reason=reason,
        )
        raise http_429(reason)
    return record
