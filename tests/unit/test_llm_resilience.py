"""Unit tests for the LLM resilience primitives - retry classification,
Retry-After-aware backoff, the circuit breaker state machine, and the token bucket.
"""

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from app.services.llm import resilience
from app.services.llm.resilience import (
    CircuitBreaker,
    TokenBucket,
    backoff_delay,
    is_retryable,
    retry_after_seconds,
)


def _status_error(cls, status, headers=None):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return cls("boom", response=resp, body=None)


# -- classification ------------------------------------------------------------

def test_is_retryable_covers_429_5xx_timeout_connection():
    assert is_retryable(_status_error(RateLimitError, 429)) is True
    assert is_retryable(_status_error(InternalServerError, 503)) is True
    assert is_retryable(APITimeoutError(httpx.Request("POST", "https://x"))) is True
    assert is_retryable(APIConnectionError(request=httpx.Request("POST", "https://x"))) is True


def test_non_retryable_client_errors_and_generic():
    from openai import BadRequestError
    assert is_retryable(_status_error(BadRequestError, 400)) is False
    assert is_retryable(ValueError("nope")) is False


# -- retry-after + backoff -----------------------------------------------------

def test_retry_after_parsed_when_numeric():
    exc = _status_error(RateLimitError, 429, headers={"retry-after": "7"})
    assert retry_after_seconds(exc) == 7.0


def test_retry_after_absent_or_http_date_returns_none():
    assert retry_after_seconds(_status_error(RateLimitError, 429)) is None
    exc = _status_error(RateLimitError, 429, headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert retry_after_seconds(exc) is None


def test_backoff_honors_retry_after_capped():
    assert backoff_delay(0, base=5, jitter=0.2, cap=30, retry_after=7) == 7
    assert backoff_delay(0, base=5, jitter=0.2, cap=10, retry_after=99) == 10  # capped


def test_backoff_exponential_within_bounds():
    # attempt 2 -> base*4 = 20, ±20% jitter -> [16, 24], capped at 30
    for _ in range(50):
        d = backoff_delay(2, base=5, jitter=0.2, cap=30)
        assert 16.0 <= d <= 24.0


def test_backoff_capped_at_max():
    assert backoff_delay(10, base=5, jitter=0.0, cap=30) == 30


# -- circuit breaker -----------------------------------------------------------

def test_breaker_opens_after_threshold_then_half_opens(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])

    cb = CircuitBreaker(fail_threshold=3, reset_seconds=30)
    assert cb.allow() is True
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open is True
    assert cb.allow() is False              # short-circuits while open

    clock["t"] += 31                        # cooldown elapsed
    assert cb.allow() is True               # first caller gets the half-open probe
    assert cb.allow() is False              # second caller blocked until it resolves

    cb.record_success()                     # probe succeeded -> closed
    assert cb.is_open is False
    assert cb.allow() is True


def test_breaker_reopens_when_probe_fails(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])
    cb = CircuitBreaker(fail_threshold=1, reset_seconds=10)

    cb.record_failure()
    assert cb.allow() is False
    clock["t"] += 11
    assert cb.allow() is True               # half-open probe
    cb.record_failure()                     # probe failed -> reopen, timer resets
    assert cb.allow() is False


def test_breaker_success_resets_consecutive(monkeypatch):
    monkeypatch.setattr(resilience, "_now", lambda: 0.0)
    cb = CircuitBreaker(fail_threshold=3, reset_seconds=10)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()                     # resets the streak
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open is False              # only 2 since reset, threshold 3


def test_breaker_disabled_always_allows():
    cb = CircuitBreaker(fail_threshold=1, reset_seconds=10, enabled=False)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow() is True


# -- token bucket --------------------------------------------------------------

async def test_token_bucket_disabled_is_noop():
    tb = TokenBucket(rpm=0)
    await tb.acquire()  # returns immediately, no throttling


async def test_token_bucket_allows_burst_then_throttles(monkeypatch):
    # Virtual clock so the test doesn't actually sleep.
    clock = {"t": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])

    async def fake_sleep(s):
        clock["t"] += s
    monkeypatch.setattr(resilience.asyncio, "sleep", fake_sleep)

    tb = TokenBucket(rpm=60, burst=2, max_wait=20)  # 1 token/sec, burst 2
    await tb.acquire()   # burst
    await tb.acquire()   # burst
    await tb.acquire()   # must wait ~1s for a refill
    assert clock["t"] == pytest.approx(1.0, abs=0.01)


async def test_token_bucket_gives_up_past_max_wait(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])

    async def fake_sleep(s):
        clock["t"] += s
    monkeypatch.setattr(resilience.asyncio, "sleep", fake_sleep)

    tb = TokenBucket(rpm=60, burst=1, max_wait=2)  # 1 token/sec
    await tb.acquire()                              # drains the bucket
    # Next needs 1s; but ask for 5 tokens -> 5s wait > max_wait(2) -> proceed anyway.
    await tb.acquire(tokens=5)
    assert clock["t"] <= 2.0
