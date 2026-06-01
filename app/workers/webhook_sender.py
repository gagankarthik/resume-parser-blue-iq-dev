"""
Async webhook delivery with HMAC-SHA256 signing and retry logic.

Signature format (Stripe-compatible):
  X-Signature: sha256=<hex>
  X-Timestamp: <unix_timestamp>

Payload: {"event": "parse.completed", "job_id": "...", "data": {...}}
"""

import asyncio
import json
import time

import httpx

from app.core.logging import get_logger
from app.core.security import sign_webhook_payload
from app.db import dynamodb as db

log = get_logger(__name__)

_RETRY_DELAYS = [2, 5, 10]  # seconds between retries


async def deliver_event(
    company_id: str,
    event: str,
    payload: dict,
) -> None:
    """Fire-and-forget delivery to all registered webhooks for this event."""
    hooks = db.get_active_webhooks_for_event(company_id, event)
    if not hooks:
        return

    tasks = [_deliver_to_hook(hook, event, payload) for hook in hooks]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _deliver_to_hook(hook: dict, event: str, payload: dict) -> None:
    url = hook["url"]
    secret = hook["hmac_secret"]
    body = json.dumps({"event": event, **payload}).encode()
    timestamp = str(int(time.time()))
    signature = sign_webhook_payload(secret, timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
        "X-Event": event,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.post(url, content=body, headers=headers)
                if resp.status_code < 500:
                    log.info(
                        "webhook_delivered",
                        url=url,
                        event=event,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return
                log.warning(
                    "webhook_server_error",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
            except httpx.RequestError as exc:
                log.warning("webhook_request_error", url=url, error=str(exc), attempt=attempt + 1)

    log.error("webhook_delivery_failed_all_retries", url=url, event=event)
