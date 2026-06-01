"""
DynamoDB-backed sliding-window rate limiter.

Two windows are checked on each request:
  • per-minute  — key: {key_hash}#min#{YYYY-MM-DDTHH:MM}
  • per-day     — key: {key_hash}#day#{YYYY-MM-DD}

DynamoDB TTL auto-expires the counters; no manual cleanup needed.
"""

import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _window_keys(key_hash: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    minute_key = f"{key_hash}#min#{now.strftime('%Y-%m-%dT%H:%M')}"
    day_key = f"{key_hash}#day#{now.strftime('%Y-%m-%d')}"
    return minute_key, day_key


def _ttl_for_minute() -> int:
    return int(time.time()) + 120   # 2-minute buffer after the minute ends


def _ttl_for_day() -> int:
    return int(time.time()) + 90000  # ~25 hours


def _increment(table: object, window_key: str, ttl: int) -> int:
    """Atomically increment counter; return new value."""
    resp = table.update_item(  # type: ignore[attr-defined]
        Key={"window_key": window_key},
        UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = if_not_exists(#t, :ttl)",
        ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
        ExpressionAttributeValues={":zero": 0, ":one": 1, ":ttl": ttl},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["count"])


def check_and_increment(
    key_hash: str,
    limit_per_minute: int,
    limit_per_day: int,
) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    Increments counters only when the request is allowed.
    """
    settings = get_settings()
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url

    dynamodb = boto3.resource("dynamodb", **kwargs)
    table = dynamodb.Table(settings.dynamodb_table_rate_limits)

    minute_key, day_key = _window_keys(key_hash)

    try:
        # Peek current counts without incrementing
        min_item = table.get_item(Key={"window_key": minute_key}).get("Item", {})
        day_item = table.get_item(Key={"window_key": day_key}).get("Item", {})

        current_min = int(min_item.get("count", 0))
        current_day = int(day_item.get("count", 0))

        if current_min >= limit_per_minute:
            return False, f"Rate limit: {limit_per_minute} requests/minute exceeded"
        if current_day >= limit_per_day:
            return False, f"Rate limit: {limit_per_day} requests/day exceeded"

        # Both within limits — now increment
        _increment(table, minute_key, _ttl_for_minute())
        _increment(table, day_key, _ttl_for_day())
        return True, ""

    except ClientError as exc:
        log.error("rate_limiter_error", error=str(exc))
        return True, ""  # fail open to avoid blocking on DynamoDB errors
