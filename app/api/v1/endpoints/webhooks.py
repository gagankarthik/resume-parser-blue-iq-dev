"""
Webhook management endpoints.

POST   /webhooks      - register a new webhook (returns HMAC secret once)
GET    /webhooks      - list registered webhooks (secret not returned)
DELETE /webhooks/{id} - remove a webhook

Webhooks are scoped to the company derived from the API key.
"""

import asyncio

from fastapi import APIRouter, Depends

from app.api.dependencies import get_api_key_record
from app.core.errors import ErrorCode, api_error
from app.core.url_validator import UnsafeWebhookURLError
from app.db import dynamodb as db
from app.models.schemas import WebhookCreateRequest, WebhookResponse
from app.services.application.webhooks import register_webhook

router = APIRouter()


@router.post(
    "/webhooks",
    response_model=WebhookResponse,
    status_code=201,
    summary="Register a webhook",
    tags=["Webhooks"],
)
async def create_webhook(
    payload: WebhookCreateRequest,
    record: dict = Depends(get_api_key_record),
) -> WebhookResponse:
    company_id: str = record["company_id"]

    # SSRF guard (scheme + public DNS, https-only in prod) runs inside
    # register_webhook; DNS resolution blocks, so off the event loop.
    try:
        hook = await asyncio.to_thread(register_webhook, company_id, payload.url, payload.events)
    except UnsafeWebhookURLError as exc:
        raise api_error(422, ErrorCode.INVALID_REQUEST, str(exc)) from exc

    return WebhookResponse(
        webhook_id=hook["webhook_id"],
        url=hook["url"],
        events=hook["events"],
        hmac_secret=hook["hmac_secret"],
        status=hook["status"],
        created_at=hook["created_at"],
    )


@router.get(
    "/webhooks",
    response_model=list[WebhookResponse],
    summary="List webhooks",
    tags=["Webhooks"],
)
async def list_webhooks(record: dict = Depends(get_api_key_record)) -> list[WebhookResponse]:
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
    record: dict = Depends(get_api_key_record),
) -> None:
    company_id: str = record["company_id"]
    hook = db.get_webhook(company_id, webhook_id)
    if not hook:
        raise api_error(404, ErrorCode.WEBHOOK_NOT_FOUND, "Webhook not found")
    db.delete_webhook(company_id, webhook_id)
