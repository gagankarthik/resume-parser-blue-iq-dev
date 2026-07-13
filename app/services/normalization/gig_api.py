"""Shared GigHealth Partner API concerns - envelope, errors, and 429 backoff.

The four partner endpoints (specialities, geographies, cities, facilities) all speak
the same contract, so it lives here once instead of four times:

  * Auth is the ``x-api-key`` header. A missing/invalid/revoked key is **401**; a valid
    key without the endpoint's permission granted is **403**.
  * Every response - success or failure - uses one envelope::

        {"success": bool, "message": str, "data": [...], "errors": [...]}

  * Two independent limits both surface as **429**: a per-second burst limit and a
    monthly quota (UTC calendar month). The partner guide is explicit: "On a 429, back
    off and retry; do not loop tightly."

Why this module exists at all: the cities client used to swallow every HTTP error into
an empty list with no log line, so a 401 (bad key), a 403 (permission not granted), a
429 (quota exhausted) and a genuine "no city matched" were **indistinguishable** from
the outside. That turned a deployment/config problem into what looked like a parsing
problem. Failures are now typed and logged; callers still degrade gracefully, but they
degrade *loudly*.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

# The partner guide documents a per-second burst limit and a monthly quota, both 429.
# Retry the burst limit a couple of times; a quota exhaustion will simply keep 429ing
# and fall through, which is correct - there is nothing to wait for until the 1st.
_MAX_RETRIES = 2
_BACKOFF_BASE = 0.5


@dataclass(frozen=True)
class GigApiError(Exception):
    """A partner API call that did not return usable data.

    ``kind`` is the actionable classification - this is what tells an operator whether
    the fix is a new key, a new permission, a higher quota, or nothing at all.
    """

    kind:    str          # auth | forbidden | rate_limited | server | transport | malformed
    status:  int | None
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind} ({self.status}): {self.message}"


def classify(status: int) -> str:
    """Map an HTTP status onto the partner guide's documented failure modes."""
    if status == 401:
        return "auth"          # missing / invalid / revoked key
    if status == 403:
        return "forbidden"     # key valid, endpoint permission not granted
    if status == 429:
        return "rate_limited"  # per-second burst OR monthly quota
    if status >= 500:
        return "server"
    return "malformed"


def unwrap(payload: object) -> list[dict]:
    """Return the envelope's ``data`` rows, or raise ``GigApiError`` if it is not usable.

    A 200 can still carry ``success: false``; the guide says error bodies use the same
    envelope. Treat that as a failure rather than silently reading an empty ``data``.
    """
    if not isinstance(payload, dict):
        raise GigApiError("malformed", None, "response body was not a JSON object")
    if payload.get("success") is False:
        raise GigApiError(
            "malformed", None, str(payload.get("message") or "partner API reported success=false"),
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise GigApiError("malformed", None, "envelope had no 'data' array")
    return [row for row in data if isinstance(row, dict)]


def _message(resp: httpx.Response) -> str:
    """Best-effort read of the envelope's human-readable ``message`` on an error."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return resp.reason_phrase or "no message"


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429, honouring Retry-After when present."""
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return min(float(raw), 10.0)
        except ValueError:
            pass
    return _BACKOFF_BASE * (2**attempt)


async def get_async(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    params: dict | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    """GET a partner endpoint, retrying 429s with backoff. Raises ``GigApiError``.

    Used on the request hot path (cities). Never loops tightly: at most ``_MAX_RETRIES``
    retries, each after an exponential (or ``Retry-After``-honouring) pause.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.get(
                url, params=params, headers={"x-api-key": api_key}, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise GigApiError("transport", None, f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            await asyncio.sleep(_retry_after(resp, attempt))
            continue
        if resp.status_code >= 400:
            raise GigApiError(classify(resp.status_code), resp.status_code, _message(resp))
        try:
            return unwrap(resp.json())
        except ValueError as exc:
            raise GigApiError("malformed", resp.status_code, "response was not JSON") from exc

    raise GigApiError("rate_limited", 429, "still rate-limited after retries")


def get_sync_envelope(
    url: str,
    api_key: str,
    *,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """Blocking GET for the offline catalog refresh scripts, retrying 429s with backoff.

    Returns the full envelope dict (the refresh clients each have their own
    ``flatten_payload`` that consumes it). Raises ``GigApiError`` on failure - the
    refresh scripts WANT to fail loudly, because a stale snapshot silently kept is
    worse than a script that exits non-zero.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, params=params, headers={"x-api-key": api_key}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise GigApiError("transport", None, f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            log.warning("gig_api_rate_limited", url=url, attempt=attempt + 1)
            time.sleep(_retry_after(resp, attempt))
            continue
        if resp.status_code >= 400:
            raise GigApiError(classify(resp.status_code), resp.status_code, _message(resp))
        try:
            body = resp.json()
        except ValueError as exc:
            raise GigApiError("malformed", resp.status_code, "response was not JSON") from exc
        if isinstance(body, dict) and body.get("success") is False:
            raise GigApiError(
                "malformed", resp.status_code,
                str(body.get("message") or "partner API reported success=false"),
            )
        return body if isinstance(body, dict) else {}

    raise GigApiError("rate_limited", 429, "still rate-limited after retries")
