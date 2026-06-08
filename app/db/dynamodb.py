"""
All DynamoDB operations in one place.

Tables:
  api_keys      — pk: key_hash
  jobs          — pk: job_id       (async job tracking, TTL 1h)
  batches       — pk: batch_id     (batch tracking, TTL 24h)
  webhooks      — pk: company_id, sk: webhook_id
  audit_logs    — pk: job_id, sk: timestamp
  companies     — pk: company_id  (GSI email-index)
  feedback      — pk: feedback_id (GSI company-created-index, TTL)
"""

import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=4)
def _get_dynamodb_resource(region: str, endpoint_url: str) -> Any:
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.resource("dynamodb", **kwargs)


def _get_dynamodb(settings=None) -> Any:
    if settings is None:
        settings = get_settings()
    return _get_dynamodb_resource(settings.aws_region, settings.dynamodb_endpoint_url)


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
) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    table.put_item(
        Item={
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "company_id": company_id,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
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


def list_api_keys_for_company(company_id: str) -> list[dict]:
    """All keys belonging to a company (via the company-index GSI)."""
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    resp = table.query(
        IndexName=settings.api_keys_company_index,
        KeyConditionExpression=Key("company_id").eq(company_id),
    )
    return resp.get("Items", [])


# ── Companies / accounts ──────────────────────────────────────────────────────

def create_company(
    company_id: str,
    name: str,
    email: str,
    plan: str = "free",
    password_hash: str | None = None,
) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    item: dict[str, Any] = {
        "company_id": company_id,
        "name": name,
        "email": email,
        "plan": plan,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if password_hash:
        item["password_hash"] = password_hash
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(company_id)")


def get_company(company_id: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    return table.get_item(Key={"company_id": company_id}).get("Item")


def get_company_by_email(email: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    resp = table.query(
        IndexName=settings.companies_email_index,
        KeyConditionExpression=Key("email").eq(email),
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def list_companies() -> list[dict]:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    return table.scan().get("Items", [])


# Mutable fields an admin may update; everything else (id, email, created_at,
# password_hash) is immutable through this path.
_COMPANY_MUTABLE = ("plan", "status")


def update_company(company_id: str, updates: dict) -> dict | None:
    """Patch a company's mutable fields (plan, status). Returns the updated item,
    or None if the company does not exist."""
    fields = {k: v for k, v in updates.items() if k in _COMPANY_MUTABLE and v is not None}
    if not fields:
        return get_company(company_id)

    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    try:
        resp = table.update_item(
            Key={"company_id": company_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(company_id)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


# ── Usage / stats (from audit logs) ───────────────────────────────────────────

def get_audit_logs_for_company(company_id: str, since_iso: str) -> list[dict]:
    """
    Audit records for a company since an ISO timestamp, via the
    company-timestamp-index GSI. Each record carries file_type, status,
    duration_ms, ocr_used, ai_tokens_used — enough for usage/token rollups.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_audit_logs)
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": settings.audit_logs_company_index,
        "KeyConditionExpression": Key("company_id").eq(company_id)
        & Key("timestamp").gte(since_iso),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


# ── Jobs ──────────────────────────────────────────────────────────────────────

def create_job(
    job_id: str,
    company_id: str,
    batch_id: str | None = None,
    retried_from: str | None = None,
    retry_count: int = 0,
) -> None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    item: dict[str, Any] = {
        "job_id": job_id,
        "company_id": company_id,
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
        "ttl": int(time.time()) + settings.job_result_ttl_seconds,
        "retry_count": retry_count,
    }
    if batch_id:
        item["batch_id"] = batch_id
    if retried_from:
        item["retried_from"] = retried_from
    table.put_item(Item=item)


def create_upload_job(
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
) -> None:
    """Create a job awaiting a direct (presigned) S3 upload.

    Records the company_id and s3_key so /resume/parse-uploaded can verify
    ownership and locate the file. Status is 'pending_upload' until the client
    completes the upload and calls parse-uploaded.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    table.put_item(
        Item={
            "job_id": job_id,
            "company_id": company_id,
            "status": "pending_upload",
            "s3_key": s3_key,
            "filename": filename,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl": int(time.time()) + settings.job_result_ttl_seconds,
            "retry_count": 0,
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
            ":t": datetime.now(UTC).isoformat(),
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
            ":t": datetime.now(UTC).isoformat(),
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
            ":t": datetime.now(UTC).isoformat(),
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
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


def list_webhooks(company_id: str) -> list[dict]:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_webhooks)
    resp = table.query(
        KeyConditionExpression=Key("company_id").eq(company_id)
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
                "timestamp": datetime.now(UTC).isoformat(),
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


# ── Feedback ──────────────────────────────────────────────────────────────────

def create_feedback(
    feedback_id: str,
    job_id: str,
    company_id: str,
    original: dict,
    updated: dict,
    changed: bool,
    changed_fields: list[str],
    created_at: str,
    profile_id: str | None = None,
    notes: str | None = None,
) -> None:
    """Persist a parsing-feedback record (original + corrected JSON).

    Stored under the authenticated company_id and TTL-expired after
    feedback_retention_days.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_feedback)
    item: dict[str, Any] = {
        "feedback_id": feedback_id,
        "job_id": job_id,
        "company_id": company_id,
        "created_at": created_at,
        "original": original,
        "updated": updated,
        "changed": changed,
        "changed_fields": changed_fields,
        "ttl": int(time.time()) + settings.feedback_retention_days * 86400,
    }
    if profile_id:
        item["profile_id"] = profile_id
    if notes:
        item["notes"] = notes
    table.put_item(Item=item)


def list_feedback_for_company(company_id: str, since_iso: str) -> list[dict]:
    """All feedback records for a company since an ISO timestamp, via the
    company-created-index GSI. Used to batch-export corrections for model
    improvement.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_feedback)
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "IndexName": settings.feedback_company_index,
        "KeyConditionExpression": Key("company_id").eq(company_id)
        & Key("created_at").gte(since_iso),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


# ── Batches ───────────────────────────────────────────────────────────────────

def _batch_table(settings=None):
    if settings is None:
        settings = get_settings()
    return _get_dynamodb(settings).Table(settings.dynamodb_table_batches)


def create_batch(
    batch_id: str,
    company_id: str,
    job_ids: list[str],
    total: int,
) -> None:
    table = _batch_table()
    table.put_item(
        Item={
            "batch_id": batch_id,
            "company_id": company_id,
            "job_ids": job_ids,
            "total": total,
            "completed": 0,
            "failed": 0,
            "status": "processing",
            "created_at": datetime.now(UTC).isoformat(),
            # 24-hour TTL — batches don't need to live as long as job results
            "ttl": int(time.time()) + 86400,
        }
    )


def get_batch(batch_id: str) -> dict | None:
    table = _batch_table()
    resp = table.get_item(Key={"batch_id": batch_id})
    return resp.get("Item")


def increment_batch_counter(batch_id: str, field: str) -> bool:
    """
    Atomically increment 'completed' or 'failed' counter.
    Returns True when all files in the batch are done (completed + failed == total),
    which signals the caller to fire the batch.completed webhook.
    """
    table = _batch_table()
    try:
        resp = table.update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="ADD #f :one",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="ALL_NEW",
        )
        item = resp.get("Attributes", {})
        total = int(item.get("total", 0))
        completed = int(item.get("completed", 0))
        failed = int(item.get("failed", 0))
        done = completed + failed

        if done >= total and total > 0:
            # Finalize status
            if failed == 0:
                final_status = "completed"
            elif completed == 0:
                final_status = "failed"
            else:
                final_status = "partial"

            table.update_item(
                Key={"batch_id": batch_id},
                UpdateExpression="SET #s = :s, completed_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": final_status,
                    ":t": datetime.now(UTC).isoformat(),
                },
            )
            return True  # batch is finished
        return False

    except ClientError as exc:
        log.error("batch_counter_update_failed", batch_id=batch_id, error=str(exc))
        return False
