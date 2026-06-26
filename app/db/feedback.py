"""Parsing-feedback records (table: feedback)."""

import time
from typing import Any

from boto3.dynamodb.conditions import Key

from app.core.config import get_settings
from app.db._client import _get_dynamodb


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
