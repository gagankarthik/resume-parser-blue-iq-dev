"""Webhook registration - one path for the API-key route and the admin route."""

from ulid import ULID

from app.core.security import generate_webhook_secret
from app.core.url_validator import validate_webhook_url
from app.db import dynamodb as db


def register_webhook(company_id: str, url: str, events: list[str]) -> dict:
    """Validate `url`, store the subscription and return the record as stored.

    The record includes the one-time `hmac_secret` and the stored `created_at`.
    Raises UnsafeWebhookURLError when the URL fails the SSRF guard (non-public
    DNS answer, or http in production). Blocking (DNS + DynamoDB) - call it off
    the event loop.
    """
    validate_webhook_url(url)
    return db.create_webhook(
        webhook_id=str(ULID()),
        company_id=company_id,
        url=url,
        hmac_secret=generate_webhook_secret(),
        events=events,
    )
