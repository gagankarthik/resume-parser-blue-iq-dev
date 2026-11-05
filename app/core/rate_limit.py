"""
In-process, per-identifier request rate limiter.

A fixed-window counter keyed by API key, evaluated inside the auth dependency so
every authenticated request is throttled with no per-endpoint wiring.

Scope & trade-offs: this is BEST-EFFORT per Lambda instance - each concurrent
execution environment keeps its own counter, so the effective global limit scales
with the number of warm instances. It is the cheap first line against a single
client hammering one warm instance; for a strict global limit, front the API with
a distributed limiter (API Gateway usage plans, or a Redis/DynamoDB token bucket).
The design mirrors the existing in-memory API-key cache in api/dependencies.py.

The API-key dependency is a sync function, so FastAPI runs it on threadpool
threads concurrently with the event loop; a lock guards the counter maps. Nothing
sensitive is stored - only opaque identifiers and counts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger

log = get_logger(__name__)

_WINDOW_SECONDS = 60
# identifier -> (window_index, count_in_window), ordered by when that window began.
# Two separate maps so the public auth throttle and the per-API-key throttle can't
# evict each other's counters.
_WINDOWS: OrderedDict[str, tuple[int, int]] = OrderedDict()
_AUTH_WINDOWS: OrderedDict[str, tuple[int, int]] = OrderedDict()
# Cap the counter map so a churn of distinct identifiers cannot grow it without
# bound - including within a single window, where there is nothing stale to drop.
_MAX_TRACKED = 50_000
_LOCK = threading.Lock()


def _hit(
    windows: OrderedDict[str, tuple[int, int]],
    identifier: str,
    limit: int,
    *,
    company_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Record one request in `windows`; raise HTTP 429 when over `limit`."""
    now = time.time()
    window = int(now // _WINDOW_SECONDS)

    with _LOCK:
        current = windows.get(identifier)
        if current is None or current[0] != window:
            _start_window(windows, identifier, window)
            return
        count = current[1] + 1
        if count <= limit:
            windows[identifier] = (window, count)
            return

    retry_after = max(1, _WINDOW_SECONDS - int(now % _WINDOW_SECONDS))
    log.warning("rate_limited", company_id=company_id, limit=limit,
                window_seconds=_WINDOW_SECONDS)
    raise api_error(
        429, ErrorCode.RATE_LIMITED,
        detail or (
            f"Rate limit of {limit} requests per minute exceeded. "
            f"Retry in {retry_after}s."
        ),
        headers={"Retry-After": str(retry_after)},
    )


def _start_window(windows: OrderedDict[str, tuple[int, int]], identifier: str, window: int) -> None:
    """Open `identifier`'s counter for `window`, keeping the map within _MAX_TRACKED.

    Re-inserting at the end keeps entries ordered by window start, so expired
    counters always sit at the front: evicting from the front drops them first
    and, only once every tracked identifier is in the current window, the
    longest-running one (whose count restarts). O(1) per insert - never a scan.
    """
    windows.pop(identifier, None)
    while windows:
        oldest_window = next(iter(windows.values()))[0]
        if oldest_window == window and len(windows) < _MAX_TRACKED:
            break
        windows.popitem(last=False)
    windows[identifier] = (window, 1)


def check(identifier: str, *, company_id: str | None = None) -> None:
    """Record one request for `identifier`; raise HTTP 429 if over the limit.

    No-op when rate limiting is disabled or the configured limit is non-positive.
    On breach, raises an `api_error` carrying a `Retry-After` header (seconds until
    the current window rolls over).
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    _hit(_WINDOWS, identifier, limit, company_id=company_id)


def check_auth(identifier: str) -> None:
    """Throttle the public auth routes (login/signup) per client IP.

    Always enforced (independent of `rate_limit_enabled`) so brute-force and
    account-enumeration protection stays on even while per-API-key limiting is off
    during client testing. No-op only when the configured limit is non-positive.
    """
    limit = get_settings().auth_rate_limit_per_minute
    if limit <= 0:
        return
    _hit(
        _AUTH_WINDOWS, f"auth:{identifier}", limit,
        detail=f"Too many authentication attempts. Retry in up to {_WINDOW_SECONDS}s.",
    )


def reset() -> None:
    """Clear all counters - for tests."""
    with _LOCK:
        _WINDOWS.clear()
        _AUTH_WINDOWS.clear()
