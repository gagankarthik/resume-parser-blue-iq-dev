"""API-key records (table: api_keys)."""

from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key

from app.core.config import get_settings
from app.db._client import _get_dynamodb


def get_api_key(key_hash: str) -> dict | None:
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    resp = table.get_item(Key={"key_hash": key_hash})
    return resp.get("Item")


def create_api_key(
    key_hash: str,
    key_prefix: str,
    company_id: str,
    name: str = "",
) -> dict:
    """Store an API key record (hash only - never the raw key) and return it as written.

    `name` is an optional human label; records without one simply omit it.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_api_keys)
    item: dict[str, Any] = {
        "key_hash": key_hash,
        "key_prefix": key_prefix,
        "company_id": company_id,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if name:
        item["name"] = name
    table.put_item(Item=item)
    return item


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
