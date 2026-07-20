"""
Resilience primitives for the LLM layer - pure and independently unit-testable.

  * `is_retryable` / `retry_after_seconds` / `backoff_delay` - classify a provider
    exception (429 / 5xx / timeout / connection) and compute how long to wait,
    honoring a `Retry-After` header when the provider sends one.
  * `CircuitBreaker` - process-local breaker. After N consecutive infra failures it
    OPENS and short-circuits calls so a provider outage degrades fast (to the
    deterministic floor / fallback) instead of every job hanging through full
    backoff. Half-opens after a cooldown to probe recovery. Synchronous and
    lock-free: asyncio is single-threaded, and its critical sections never await,
    so it is safe to share process-wide across warm-container invocations (which is
    what lets it REMEMBER an ongoing outage between invocations).
  * `TokenBucket` - async requests-per-minute limiter to smooth a burst against the
    account ceiling. Best-effort: it gives up waiting past `max_wait` and proceeds
    rather than failing a job, since its job is to smooth, not to hard-block.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

# Clock indirection so tests can inject a deterministic time source.
_now = time.monotonic


def is_retryable(exc: BaseException) -> bool:
    """True for a transient provider fault worth retrying: 429, any 5xx, request
    timeout, or a connection error. A 4xx other than 429 is a client/content error
    and is NOT retried (retrying reproduces it and burns budget)."""
    if isinstance(exc, RateLimitError | APITimeoutError | APIConnectionError | InternalServerError):
        return True
    if isinstance(exc, APIStatusError):
        code = getattr(exc, "status_code", 0) or 0
        return code == 429 or code >= 500
    return False


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's `Retry-After` (seconds) if present and numeric, else None.

    A 429/503 often carries this header telling us exactly when the limit clears;
    obeying it is friendlier and more effective than blind exponential backoff.
    An HTTP-date form is ignored (returns None -> fall back to computed backoff).
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def backoff_delay(
    attempt: int,
    *,
    base: float,
    jitter: float,
    cap: float,
    retry_after: float | None = None,
) -> float:
    """Delay before the next attempt (0-indexed `attempt`).

    Honors `retry_after` when given; otherwise exponential (base * 2**attempt) with
    ±`jitter` fraction to avoid a thundering herd. Always clamped to `[1, cap]` so a
    single wait can't blow the caller's time budget.
    """
    if retry_after is not None:
        return min(retry_after, cap)
    raw = base * (2 ** attempt)
    delta = raw * jitter * (2 * random.random() - 1)
    return min(max(raw + delta, 1.0), cap)


class CircuitBreaker:
    """Per-process circuit breaker with a half-open probe.

    States: CLOSED (calls flow) -> OPEN (short-circuit for `reset_seconds`) ->
    HALF-OPEN (a single probe allowed) -> CLOSED on success / OPEN on failure.
    """

    def __init__(
        self,
        *,
        fail_threshold: int,
        reset_seconds: float,
        enabled: bool = True,
        name: str = "llm",
    ) -> None:
        self.fail_threshold = max(1, fail_threshold)
        self.reset_seconds = reset_seconds
        self.enabled = enabled
        self.name = name
        self._consecutive = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        """True if a call may proceed. When open past the reset window, admits a
        SINGLE half-open probe and blocks the rest until it resolves."""
        if not self.enabled or self._opened_at is None:
            return True
        if _now() - self._opened_at < self.reset_seconds:
            return False  # still open
        if self._probing:
            return False  # a half-open probe is already in flight
        self._probing = True
        return True

    def record_success(self) -> None:
        self._consecutive = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._consecutive += 1
        self._probing = False
        if self._consecutive >= self.fail_threshold:
            self._opened_at = _now()  # (re)open and restart the cooldown


class TokenBucket:
    """Async token bucket limiting requests to ~`rpm` per minute.

    Bound to one event loop (its lock is), so build one per running loop. Disabled
    (a no-op) when `rpm <= 0`.
    """

    def __init__(self, *, rpm: int, burst: int = 0, max_wait: float = 20.0) -> None:
        self.enabled = rpm > 0
        self.rate = rpm / 60.0 if self.enabled else 0.0
        # Default burst allows a small clump of near-simultaneous calls (the per-role
        # fan-out) before throttling kicks in.
        self.capacity = float(burst) if burst > 0 else max(1.0, math.ceil(self.rate * 2))
        self.max_wait = max_wait
        self._tokens = self.capacity
        self._updated = _now()
        self._lock: asyncio.Lock | None = None

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until `tokens` are available (or `max_wait` elapses, then proceed
        best-effort). Serialized so admissions are FIFO-fair."""
        if not self.enabled:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            waited = 0.0
            while True:
                now = _now()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                sleep_for = (tokens - self._tokens) / self.rate
                if waited + sleep_for > self.max_wait:
                    # Smoothing, not hard-blocking: proceed rather than fail a job.
                    self._tokens = 0.0
                    return
                await asyncio.sleep(sleep_for)
                waited += sleep_for
