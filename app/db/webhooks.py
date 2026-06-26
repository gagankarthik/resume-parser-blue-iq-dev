"""Webhook subscriptions (table: webhooks)."""

from datetime import UTC, datetime

from boto3.dynamodb.conditions import Key

from app.core.config import get_settings
from app.db._client import _get_dynamodb


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
