"""
Async webhook delivery with HMAC-SHA256 signing, retry, and circuit breaker.

Signature format (Stripe-compatible):
  X-Signature: sha256=<hex>
  X-Timestamp:  <unix_timestamp>

Circuit breaker:
  In-memory per-process. After CIRCUIT_OPEN_AFTER consecutive delivery failures
  for a URL, that URL is skipped until CIRCUIT_RESET_AFTER seconds have elapsed.
  Lambda cold starts reset the circuit naturally.
"""

import asyncio
import json
import time

import httpx

from app.core.logging import get_logger
from app.core.security import sign_webhook_payload
from app.core.url_validator import UnsafeWebhookURLError, validate_webhook_url
from app.db import dynamodb as db

log = get_logger(__name__)

_RETRY_DELAYS       = [2, 5, 10]          # seconds between attempts
CIRCUIT_OPEN_AFTER  = 5                    # consecutive failures to open circuit
CIRCUIT_RESET_AFTER = 300                  # seconds before attempting a dead URL again

# url → (failure_count, last_failure_epoch)
_circuit: dict[str, tuple[int, float]] = {}


def _circuit_open(url: str) -> bool:
    entry = _circuit.get(url)
    if not entry:
        return False
    failures, last_fail = entry
    if failures >= CIRCUIT_OPEN_AFTER:
        if time.time() - last_fail < CIRCUIT_RESET_AFTER:
            return True
        # Reset after cooldown
        del _circuit[url]
    return False


def _record_failure(url: str) -> None:
    entry = _circuit.get(url, (0, 0.0))
    _circuit[url] = (entry[0] + 1, time.time())


def _record_success(url: str) -> None:
    _circuit.pop(url, None)


async def deliver_event(company_id: str, event: str, payload: dict) -> None:
    """Deliver event to all registered webhooks subscribed to this event."""
    hooks = db.get_active_webhooks_for_event(company_id, event)
    if not hooks:
        return
    tasks = [_deliver_to_hook(hook, event, payload) for hook in hooks]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _deliver_to_hook(hook: dict, event: str, payload: dict) -> None:
    url    = hook["url"]
    secret = hook["hmac_secret"]

    # SSRF re-check at delivery (defends against DNS rebinding on stored URLs).
    # getaddrinfo is blocking, so run it off the event loop.
    try:
        await asyncio.get_event_loop().run_in_executor(None, validate_webhook_url, url)
    except UnsafeWebhookURLError as exc:
        log.warning("webhook_unsafe_url_skip", url=url, error=str(exc))
        return

    if _circuit_open(url):
        log.warning("webhook_circuit_open_skip", url=url, event=event)
        return

    body      = json.dumps({"event": event, **payload}).encode()
    timestamp = str(int(time.time()))
    signature = sign_webhook_payload(secret, timestamp, body)
    headers   = {
        "Content-Type": "application/json",
        "X-Signature":  signature,
        "X-Timestamp":  timestamp,
        "X-Event":      event,
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
                        url=url, event_name=event,
                        status=resp.status_code, attempt=attempt + 1,
                    )
                    _record_success(url)
                    return
                log.warning("webhook_server_error", url=url, status=resp.status_code)
            except httpx.RequestError as exc:
                log.warning("webhook_request_error", url=url, error=str(exc))

    _record_failure(url)
    log.error("webhook_all_retries_failed", url=url, event_name=event)
