"""
Async webhook delivery with HMAC-SHA256 signing, retry, and circuit breaker.

Signature format (Stripe-compatible):
  X-Signature: sha256=<hex>
  X-Timestamp:  <unix_timestamp>

Circuit breaker:
  In-memory per-process. After CIRCUIT_OPEN_AFTER consecutive delivery failures
  for a URL, that URL is skipped until CIRCUIT_RESET_AFTER seconds have elapsed.
  Lambda cold starts reset the circuit naturally.

Webhook URLs are never logged - receivers put tokens in paths and query strings.
Logs name the registration (webhook_id) and the host.
"""

import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger
from app.core.security import sign_webhook_payload
from app.core.url_validator import UnsafeWebhookURLError, resolve_webhook_url
from app.db import dynamodb as db

log = get_logger(__name__)

_RETRY_DELAYS       = [2, 5, 10]          # seconds between attempts
_HTTP_TIMEOUT       = 10.0                 # overall deadline per POST attempt
CIRCUIT_OPEN_AFTER  = 5                    # consecutive failures to open circuit
CIRCUIT_RESET_AFTER = 300                  # seconds before attempting a dead URL again

# Worst case for one hook: every retry sleeps, and every attempt burns the full HTTP
# timeout. A caller that bounds delivery (the worker's `deliver_safely`) MUST allow at
# least this long, or it cancels the tail of the ladder - see job_outcome.WEBHOOK_TIMEOUT.
DELIVERY_BUDGET_SECONDS = sum(_RETRY_DELAYS) + (len(_RETRY_DELAYS) + 1) * _HTTP_TIMEOUT

# url -> (failure_count, last_failure_epoch)
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


async def _post_once(client: httpx.AsyncClient, request: httpx.Request) -> int:
    """One delivery attempt under an overall deadline; returns the status code.

    httpx's timeout applies per connect/read/write, so a receiver trickling bytes
    could hold an attempt open indefinitely - the deadline bounds the whole
    attempt. The body is never read: only the status matters, and a receiver
    could stream an unbounded one.
    """
    async with asyncio.timeout(_HTTP_TIMEOUT):
        response = await client.send(request, stream=True)
        await response.aclose()
        return response.status_code


async def _deliver_to_hook(hook: dict, event: str, payload: dict) -> None:
    url    = hook["url"]
    secret = hook["hmac_secret"]
    where  = {"webhook_id": hook.get("webhook_id", ""), "host": urlsplit(url).hostname or ""}

    # SSRF guard at delivery: resolve the host ONCE, require every address to be
    # public, and connect to exactly that address. Letting httpx resolve the name
    # again would reopen the DNS-rebinding window between check and connect.
    # getaddrinfo is blocking, so run it off the event loop.
    try:
        target = await asyncio.to_thread(resolve_webhook_url, url)
    except UnsafeWebhookURLError as exc:
        log.warning("webhook_unsafe_url_skip", **where, error=str(exc))
        return

    if _circuit_open(url):
        log.warning("webhook_circuit_open_skip", **where, event_name=event)
        return

    body      = json.dumps({"event": event, **payload}).encode()
    timestamp = str(int(time.time()))
    signature = sign_webhook_payload(secret, timestamp, body)
    registered = httpx.URL(url)
    headers   = {
        "Content-Type": "application/json",
        "X-Signature":  signature,
        "X-Timestamp":  timestamp,
        "X-Event":      event,
        # The request goes to the validated IP; the receiver still sees its own name.
        "Host":         registered.netloc.decode("ascii"),
    }
    # TLS SNI and certificate verification use the registered host name, not the IP.
    extensions = {"sni_hostname": target.host}

    # A non-5xx reply means "delivered" - including a 4xx. A receiver that rejects
    # the signature answers 401, so the event is dropped for good; that is why the
    # status is logged at WARNING, not folded into the success line. It is the only
    # signal a misconfigured secret leaves on our side.
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            request = client.build_request(
                "POST", registered.copy_with(host=target.ip),
                content=body, headers=headers, extensions=extensions,
            )
            for attempt, delay in enumerate([0] + _RETRY_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    status = await _post_once(client, request)
                except (httpx.RequestError, TimeoutError) as exc:
                    log.warning("webhook_request_error", **where, error_type=type(exc).__name__)
                    continue
                if status < 500:
                    log.info(
                        "webhook_delivered",
                        **where, event_name=event,
                        status=status, attempt=attempt + 1,
                    )
                    if status >= 400:
                        log.warning(
                            "webhook_rejected_not_retried",
                            **where, event_name=event, status=status,
                            hint="4xx is terminal - this event was dropped. A 401 "
                                 "is almost always a signing-secret mismatch or a "
                                 "receiver verifying a re-serialized body.",
                        )
                    _record_success(url)
                    return
                log.warning("webhook_server_error", **where, status=status)
    except asyncio.CancelledError:
        # The caller's budget ran out mid-ladder. Record the failure anyway, or a
        # permanently dead URL never accumulates enough failures to open the circuit
        # and every later parse pays the full ladder again.
        _record_failure(url)
        log.warning("webhook_delivery_cancelled", **where, event_name=event)
        raise

    _record_failure(url)
    log.error("webhook_all_retries_failed", **where, event_name=event)
