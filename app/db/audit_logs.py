"""Audit logs (table: audit_logs) - usage records, never resume content."""

from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db._client import _get_dynamodb

log = get_logger(__name__)

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
    key_hash: str = "",
    key_prefix: str = "",
) -> None:
    """Write an audit record - never stores resume content.

    key_hash / key_prefix attribute the job to the API key that produced it, so
    the admin platform can break usage down per key. Both are optional: legacy
    records and any path without an authenticated key simply omit them.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_audit_logs)
    item = {
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
    if key_hash:
        item["key_hash"] = key_hash
    if key_prefix:
        item["key_prefix"] = key_prefix
    try:
        table.put_item(Item=item)
    except ClientError as exc:
        log.error("audit_log_write_failed", job_id=job_id, error=str(exc))


def get_audit_logs_for_company(company_id: str, since_iso: str) -> list[dict]:
    """
    Audit records for a company since an ISO timestamp, via the
    company-timestamp-index GSI. Each record carries file_type, status,
    duration_ms, ocr_used, ai_tokens_used - enough for usage/token rollups.
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
