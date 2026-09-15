"""Batch tracking (table: batches)."""

import time
from datetime import UTC, datetime

from app.core.config import get_settings
from app.db._client import _get_dynamodb


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
            # 24-hour TTL - batches don't need to live as long as job results
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

    A failed write raises. It used to be logged and reported as "not done", which
    hid a lost count behind a batch that simply never completed.
    """
    table = _batch_table()
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

    if total <= 0 or completed + failed < total:
        return False

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
