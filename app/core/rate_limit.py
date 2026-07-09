"""
In-process, per-identifier request rate limiter.

A fixed-window counter keyed by API key, evaluated inside the auth dependency so
every authenticated request is throttled with no per-endpoint wiring.

Scope & trade-offs: this is BEST-EFFORT per Lambda instance — each concurrent
execution environment keeps its own counter, so the effective global limit scales
with the number of warm instances. It is the cheap first line against a single
client hammering one warm instance; for a strict global limit, front the API with
a distributed limiter (API Gateway usage plans, or a Redis/DynamoDB token bucket).
The design mirrors the existing in-memory API-key cache in api/dependencies.py.

Runs on asyncio's single-threaded event loop, so the counter mutations need no
lock. Nothing sensitive is stored — only opaque identifiers and counts.
"""

from __future__ import annotations

import time

from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger

log = get_logger(__name__)

_WINDOW_SECONDS = 60
# identifier → (window_index, count_in_window)
_WINDOWS: dict[str, tuple[int, int]] = {}
# Cap the counter map so a churn of distinct identifiers cannot grow it without
# bound; when exceeded we drop everything outside the current window.
_MAX_TRACKED = 50_000


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

    now = time.time()
    window = int(now // _WINDOW_SECONDS)
    current = _WINDOWS.get(identifier)

    if current is None or current[0] != window:
        if len(_WINDOWS) >= _MAX_TRACKED:
            _prune(window)
        _WINDOWS[identifier] = (window, 1)
        return

    count = current[1] + 1
    if count > limit:
        retry_after = max(1, _WINDOW_SECONDS - int(now % _WINDOW_SECONDS))
        log.warning("rate_limited", company_id=company_id, limit=limit,
                    window_seconds=_WINDOW_SECONDS)
        raise api_error(
            429, ErrorCode.RATE_LIMITED,
            f"Rate limit of {limit} requests per minute exceeded. "
            f"Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    _WINDOWS[identifier] = (window, count)


def _prune(current_window: int) -> None:
    """Drop counters from windows other than the current one."""
    for key in [k for k, (w, _) in _WINDOWS.items() if w != current_window]:
        del _WINDOWS[key]


def reset() -> None:
    """Clear all counters — for tests."""
    _WINDOWS.clear()
