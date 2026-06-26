"""API-key records (table: api_keys)."""

from datetime import UTC, datetime

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
