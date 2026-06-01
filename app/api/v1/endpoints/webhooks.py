"""
Webhook management endpoints.

POST   /webhooks          — register a new webhook
GET    /webhooks          — list registered webhooks
DELETE /webhooks/{id}     — remove a webhook

Webhooks are scoped to the company derived from the API key.
The HMAC secret is only returned on creation; it cannot be retrieved later.
"""

from fastapi import APIRouter, Depends
from python_ulid import ULID

from app.api.dependencies import enforce_rate_limit
from app.core.exceptions import http_404
from app.core.security import generate_webhook_secret
from app.db import dynamodb as db
from app.models.schemas import WebhookCreateRequest, WebhookResponse

router = APIRouter()

_VALID_EVENTS = {"parse.completed", "parse.failed"}


@router.post(
    "/webhooks",
    response_model=WebhookResponse,
    status_code=201,
    summary="Register a webhook",
    tags=["Webhooks"],
)
async def create_webhook(
    payload: WebhookCreateRequest,
    record: dict = Depends(enforce_rate_limit),
) -> WebhookResponse:
    company_id: str = record["company_id"]

    # Validate events
    invalid = set(payload.events) - _VALID_EVENTS
    if invalid:
        from app.core.exceptions import http_422
        raise http_422(f"Invalid events: {invalid}. Valid events: {_VALID_EVENTS}")

    # Require HTTPS in production
    from app.core.config import get_settings
    if get_settings().is_production and not payload.url.startswith("https://"):
        from app.core.exceptions import http_422
        raise http_422("Webhook URL must use HTTPS in production")

    webhook_id = str(ULID())
    secret = generate_webhook_secret()

    db.create_webhook(
        webhook_id=webhook_id,
        company_id=company_id,
        url=payload.url,
        hmac_secret=secret,
        events=payload.events,
    )

    return WebhookResponse(
        webhook_id=webhook_id,
        url=payload.url,
        events=payload.events,
        hmac_secret=secret,  # only time the secret is shown
        status="active",
        created_at=__import__("datetime").datetime.utcnow().isoformat(),
    )


@router.get(
    "/webhooks",
    response_model=list[WebhookResponse],
    summary="List webhooks",
    tags=["Webhooks"],
)
async def list_webhooks(
    record: dict = Depends(enforce_rate_limit),
) -> list[WebhookResponse]:
    company_id: str = record["company_id"]
    hooks = db.list_webhooks(company_id)
    return [
        WebhookResponse(
            webhook_id=h["webhook_id"],
            url=h["url"],
            events=h.get("events", []),
            status=h.get("status", "active"),
            created_at=h.get("created_at", ""),
        )
        for h in hooks
    ]


@router.delete(
    "/webhooks/{webhook_id}",
    status_code=204,
    summary="Delete a webhook",
    tags=["Webhooks"],
)
async def delete_webhook(
    webhook_id: str,
    record: dict = Depends(enforce_rate_limit),
) -> None:
    company_id: str = record["company_id"]
    hook = db.get_webhook(company_id, webhook_id)
    if not hook:
        raise http_404("Webhook not found")
    db.delete_webhook(company_id, webhook_id)
