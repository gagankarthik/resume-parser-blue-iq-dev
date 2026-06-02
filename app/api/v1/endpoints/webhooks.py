"""
Webhook management endpoints.

POST   /webhooks      — register a new webhook (returns HMAC secret once)
GET    /webhooks      — list registered webhooks (secret not returned)
DELETE /webhooks/{id} — remove a webhook

Webhooks are scoped to the company derived from the API key.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from python_ulid import ULID

from app.api.dependencies import enforce_rate_limit
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.security import generate_webhook_secret
from app.db import dynamodb as db
from app.models.schemas import WebhookCreateRequest, WebhookResponse

router = APIRouter()

_VALID_EVENTS = {"parse.completed", "parse.failed", "batch.completed"}


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
    settings = get_settings()

    if settings.is_production and not payload.url.startswith("https://"):
        raise api_error(422, ErrorCode.INVALID_REQUEST, "Webhook URL must use HTTPS in production")

    webhook_id = str(ULID())
    secret     = generate_webhook_secret()

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
        hmac_secret=secret,
        status="active",
        created_at=datetime.now(UTC).isoformat(),
    )


@router.get(
    "/webhooks",
    response_model=list[WebhookResponse],
    summary="List webhooks",
    tags=["Webhooks"],
)
async def list_webhooks(record: dict = Depends(enforce_rate_limit)) -> list[WebhookResponse]:
    hooks = db.list_webhooks(record["company_id"])
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
        raise api_error(404, ErrorCode.WEBHOOK_NOT_FOUND, "Webhook not found")
    db.delete_webhook(company_id, webhook_id)
