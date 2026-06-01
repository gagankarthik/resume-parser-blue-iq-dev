"""
All DynamoDB operations in one place.

Tables:
  api_keys      — pk: key_hash
  rate_limits   — pk: window_key   (managed by rate_limiter.py)
  jobs          — pk: job_id       (async job tracking, TTL 1h)
  webhooks      — pk: company_id, sk: webhook_id
  audit_logs    — pk: job_id, sk: timestamp
"""

import time
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _get_dynamodb(settings=None):
    if settings is None:
        settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
    return boto3.resource("dynamodb", **kwargs)


# ── API Keys ──────────────────────────────────────────────────────────────────

def get_api_key(key_hash: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    resp = table.get_item(Key={"key_hash": key_hash})
    return resp.get("Item")


def create_api_key(
    key_hash: str,
    key_prefix: str,
    company_id: str,
    rate_limit_per_minute: int,
    rate_limit_per_day: int,
) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    table.put_item(
        Item={
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "company_id": company_id,
            "status": "active",
            "rate_limit_per_minute": rate_limit_per_minute,
            "rate_limit_per_day": rate_limit_per_day,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def revoke_api_key(key_hash: str) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    table.update_item(
        Key={"key_hash": key_hash},
        UpdateExpression="SET #s = :revoked",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":revoked": "revoked"},
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────

def create_job(job_id: str, company_id: str) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    table.put_item(
        Item={
            "job_id": job_id,
            "company_id": company_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(time.time()) + settings.job_result_ttl_seconds,
        }
    )


def update_job_processing(job_id: str) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, started_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "processing",
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )


def update_job_completed(job_id: str, result: dict) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, completed_at = :t, #r = :r",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={
            ":s": "completed",
            ":t": datetime.now(timezone.utc).isoformat(),
            ":r": result,
        },
    )


def update_job_failed(job_id: str, error: str, error_code: str) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, completed_at = :t, #e = :e, error_code = :ec",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":s": "failed",
            ":t": datetime.now(timezone.utc).isoformat(),
            ":e": error,
            ":ec": error_code,
        },
    )


def get_job(job_id: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    resp = table.get_item(Key={"job_id": job_id})
    return resp.get("Item")


# ── Webhooks ──────────────────────────────────────────────────────────────────

def create_webhook(
    webhook_id: str,
    company_id: str,
    url: str,
    hmac_secret: str,
    events: list[str],
) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_webhooks)
    table.put_item(
        Item={
            "company_id": company_id,
            "webhook_id": webhook_id,
            "url": url,
            "hmac_secret": hmac_secret,
            "events": events,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def list_webhooks(company_id: str) -> list[dict]:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_webhooks)
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("company_id").eq(company_id)
    )
    return resp.get("Items", [])


def get_webhook(company_id: str, webhook_id: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_webhooks)
    resp = table.get_item(Key={"company_id": company_id, "webhook_id": webhook_id})
    return resp.get("Item")


def delete_webhook(company_id: str, webhook_id: str) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_webhooks)
    table.delete_item(Key={"company_id": company_id, "webhook_id": webhook_id})


def get_active_webhooks_for_event(company_id: str, event: str) -> list[dict]:
    """Return all active webhooks subscribed to a given event."""
    all_hooks = list_webhooks(company_id)
    return [
        h for h in all_hooks
        if h.get("status") == "active" and event in h.get("events", [])
    ]


# ── Audit Logs ────────────────────────────────────────────────────────────────

def write_audit_log(
    job_id: str,
    company_id: str,
    file_type: str,
    file_size_bytes: int,
    status: str,
    duration_ms: int,
    ocr_used: bool = False,
    ai_tokens_used: int = 0,
    error_code: str = "",
) -> None:
    """Write an audit record — never stores resume content."""
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_audit_logs)
    try:
        table.put_item(
            Item={
                "job_id": job_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "company_id": company_id,
                "file_type": file_type,
                "file_size_bytes": file_size_bytes,
                "status": status,
                "duration_ms": duration_ms,
                "ocr_used": ocr_used,
                "ai_tokens_used": ai_tokens_used,
                "error_code": error_code,
            }
        )
    except ClientError as exc:
        log.error("audit_log_write_failed", job_id=job_id, error=str(exc))
