"""Company / account records (table: companies)."""

from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.db._client import _get_dynamodb


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
    """Return every company, following pagination.

    A bare scan() returns only the first <=1 MB page, so once companies exceed one
    page the admin listing and platform-wide stats rollups silently under-report.
    Page through LastEvaluatedKey to get the full set.
    """
    settings = get_settings()
    table = _get_dynamodb(settings).Table(settings.dynamodb_table_companies)
    items: list[dict] = []
    kwargs: dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


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
