"""Async job tracking (table: jobs) and job-result (de)serialization."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.db._client import _get_dynamodb


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
