"""Async job tracking (table: jobs) and job-result (de)serialization."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db._client import _get_dynamodb

log = get_logger(__name__)


def _is_item_too_large(exc: ClientError) -> bool:
    """True when a write failed because the item exceeds DynamoDB's 400 KB cap."""
    err = exc.response.get("Error", {})
    return (
        err.get("Code") == "ValidationException"
        and "size" in err.get("Message", "").lower()
    )


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


def claim_upload_job(job_id: str) -> bool:
    """Atomically transition a job from 'pending_upload' to 'processing'.

    Returns True when THIS caller won the claim, False when the job was already
    claimed. Closes the check-then-act race in /resume/parse-uploaded where two
    concurrent calls with the same job_id both pass a `status == pending_upload`
    read and both run the (billed) parse.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :proc, started_at = :t",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":proc": "processing",
                ":pending": "pending_upload",
                ":t": datetime.now(UTC).isoformat(),
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def mark_batch_counted(job_id: str) -> bool:
    """Atomically claim the batch-counter increment for this job.

    Returns True the first time, False if already counted. An async-Lambda
    ("Event") invocation that is retried after a hard timeout/OOM would otherwise
    re-run the pipeline and increment the batch's completed/failed counters a
    second time (inflating them past `total` and re-firing batch.completed).
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET batch_counted = :t",
            ConditionExpression="attribute_not_exists(batch_counted)",
            ExpressionAttributeValues={":t": True},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _dynamo_safe(value: dict) -> dict:
    """Make a JSON-able dict storable in DynamoDB: floats become Decimals
    (boto3 rejects Python floats with 'Float types are not supported').
    The parsed-resume result carries float confidence scores, so every async
    job result must pass through this before update_item."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def update_job_completed(job_id: str, result: dict) -> None:
    # A degraded parse (AI timed out / failed → only contact anchors recovered)
    # gets its own terminal status "partial", NOT "completed". A consumer that
    # gates ingestion on status == "completed" must not silently accept a record
    # that carries a "needs human review" warning. `result["partial"]` is set by
    # the pipeline and always present via result_record()/the worker payload.
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    status = "partial" if result.get("partial") else "completed"
    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, completed_at = :t, #r = :r",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={
                ":s": status,
                ":t": datetime.now(UTC).isoformat(),
                ":r": _dynamo_safe(result),
            },
        )
        return
    except ClientError as exc:
        if not _is_item_too_large(exc):
            raise

    # The parsed result exceeds DynamoDB's 400 KB item limit (a very dense résumé).
    # Persist a TERMINAL record without the oversized payload so the job never
    # wedges in 'processing' (pollers would otherwise see 'processing' until the
    # TTL, then JOB_NOT_FOUND). Real-time consumers already got the full data via
    # the parse.completed webhook; poll clients get a clear marker + warning.
    log.warning("job_result_too_large", job_id=job_id)
    warnings = list(result.get("warnings") or [])
    warnings.append(
        "result_omitted: parsed output exceeded the storage size limit; "
        "retrieve it from the parse.completed webhook or re-parse"
    )
    stub = {
        "data": None,
        "confidence": result.get("confidence"),
        "partial": bool(result.get("partial")),
        "warnings": warnings,
        "result_too_large": True,
    }
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, completed_at = :t, #r = :r",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={
            ":s": status,
            ":t": datetime.now(UTC).isoformat(),
            ":r": _dynamo_safe(stub),
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


def _plain(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals back to int/float so read-back job
    results look like fresh JSON. Without this, schema validators that check
    isinstance(v, int | float) (e.g. education years) silently null Decimals."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def get_job(job_id: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_jobs)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    return _plain(item) if item else None
