"""
DynamoDB-backed sliding-window rate limiter — atomic, race-condition-free.

Two windows per request:
  • per-minute  key: {key_hash}#min#{YYYY-MM-DDTHH:MM}
  • per-day     key: {key_hash}#day#{YYYY-MM-DD}

Strategy:
  1. Attempt a conditional ADD (increment only if below limit).
  2. On ConditionCheckFailedException → limit exceeded.
  3. Return (allowed, reason, remaining_minute, remaining_day).

The conditional update guarantees atomicity — no separate peek needed.
DynamoDB TTL auto-expires counters; no manual cleanup.
"""

import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _window_keys(key_hash: str) -> tuple[str, str, int, int]:
    now = datetime.now(timezone.utc)
    min_key = f"{key_hash}#min#{now.strftime('%Y-%m-%dT%H:%M')}"
    day_key = f"{key_hash}#day#{now.strftime('%Y-%m-%d')}"
    ttl_min = int(time.time()) + 120
    ttl_day = int(time.time()) + 90_000
    return min_key, day_key, ttl_min, ttl_day


def _atomic_increment(table: Any, window_key: str, ttl: int, limit: int) -> tuple[bool, int]:
    """
    Atomically increment counter, failing if already at limit.
    Returns (success, new_count).
    success=False means the limit was already reached.
    """
    try:
        resp = table.update_item(
            Key={"window_key": window_key},
            UpdateExpression=(
                "SET #c = if_not_exists(#c, :zero) + :one, "
                "#t = if_not_exists(#t, :ttl)"
            ),
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":ttl": ttl,
                ":limit": limit,
            },
            ReturnValues="ALL_NEW",
        )
        new_count = int(resp["Attributes"].get("count", 1))
        return True, new_count
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Limit reached — read current count for "remaining" header
            item = table.get_item(Key={"window_key": window_key}).get("Item", {})
            return False, int(item.get("count", limit))
        log.error("rate_limiter_dynamodb_error", error=str(exc), window_key=window_key)
        return True, 0  # fail open on unexpected DynamoDB errors


def check_and_increment(
    key_hash: str,
    limit_per_minute: int,
    limit_per_day: int,
) -> tuple[bool, str, int, int]:
    """
    Check both rate-limit windows atomically.

    Returns (allowed, reason, remaining_minute, remaining_day).
    remaining_* is what's left AFTER this request (0 when at limit).
    Increments only the windows that haven't been blocked.
    """
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url

    dynamodb = boto3.resource("dynamodb", **kwargs)
    table = dynamodb.Table(settings.dynamodb_table_rate_limits)

    min_key, day_key, ttl_min, ttl_day = _window_keys(key_hash)

    # Check per-minute window first
    min_ok, min_count = _atomic_increment(table, min_key, ttl_min, limit_per_minute)
    if not min_ok:
        remaining_day_item = table.get_item(Key={"window_key": day_key}).get("Item", {})
        remaining_day = max(limit_per_day - int(remaining_day_item.get("count", 0)), 0)
        return (
            False,
            f"Rate limit exceeded: {limit_per_minute} requests/minute",
            0,
            remaining_day,
        )

    # Check per-day window
    day_ok, day_count = _atomic_increment(table, day_key, ttl_day, limit_per_day)
    if not day_ok:
        # Roll back the minute increment (best-effort)
        try:
            table.update_item(
                Key={"window_key": min_key},
                UpdateExpression="SET #c = #c - :one",
                ConditionExpression="#c > :zero",
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues={":one": 1, ":zero": 0},
            )
        except ClientError:
            pass  # rollback failure is acceptable; count is slightly off
        return (
            False,
            f"Daily limit exceeded: {limit_per_day} requests/day",
            max(limit_per_minute - min_count + 1, 0),
            0,
        )

    remaining_min = max(limit_per_minute - min_count, 0)
    remaining_day = max(limit_per_day - day_count, 0)
    return True, "", remaining_min, remaining_day
