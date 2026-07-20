"""
The one place an LLM structured-output call is made.

`structured_parse` wraps a single provider call with the whole resilience policy:

  token bucket (rate)  ->  circuit breaker (per provider)  ->  attempt loop
  [ retry transient 429/5xx/timeout with backoff | escalate frequency_penalty to
    break a token-ceiling repetition loop ]  ->  Azure same-model fallback  ->  raise

Callers (BaseAgent._structured_call, ai_parser.parse, the specialty-AI tier) keep
their own prompt-building and token metering; they hand the assembled call here and
get back the parsed model + usage, or an AIParsingError so the pipeline degrades to
the deterministic floor. Centralizing this is the fix for the correlated-failure
risk: retry/breaker/fallback are defined once, not reimplemented per call site.

Per-loop vs per-process state:
  * Clients (httpx pools) and the TokenBucket (asyncio.Lock) are bound to the
    running loop and rebuilt when it changes - the worker Lambda makes a fresh loop
    per invocation, so a cached one would fail on warm reuse.
  * Circuit breakers are process-global on purpose: a warm container should REMEMBER
    that the provider is down across invocations, not relearn it every time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI, LengthFinishReasonError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.core.logging import get_logger
from app.services.llm.resilience import (
    CircuitBreaker,
    TokenBucket,
    backoff_delay,
    is_retryable,
    retry_after_seconds,
)

log = get_logger(__name__)

# Escalating frequency_penalty used ONLY to break a degenerate repetition loop (a
# strict-schema call that runs to the token ceiling). Default calls stay at 0.0 so
# verbatim extraction is never biased away from words a résumé legitimately repeats.
LOOP_BREAK_PENALTIES = (0.3, 0.6)


@dataclass
class LLMResult:
    """What a successful structured call returns to the caller."""

    parsed: BaseModel
    usage: Any            # resp.usage (may be None) - caller extracts token counts
    provider: str         # "openai" | "azure" (which one served it)


@dataclass
class _Provider:
    name: str
    client: AsyncOpenAI
    model: str
    breaker: CircuitBreaker


# -- Internal control-flow signals (never escape this module) ------------------

class _ContentError(Exception):
    """A non-retryable, provider-healthy failure (bad request, empty output, or an
    exhausted repetition loop). Same model on a fallback would fail identically, so
    we stop rather than fall through to Azure."""


class _TransientExhausted(Exception):
    """Retries on a transient (429/5xx/timeout) fault are spent for this provider;
    the caller may try the next provider."""


# -- Per-loop / per-process state ----------------------------------------------

_bound_loop: asyncio.AbstractEventLoop | None = None
_primary_client: AsyncOpenAI | None = None
_azure_client: AsyncOpenAI | None = None
_bucket: TokenBucket | None = None

# Process-global so an outage is remembered across warm-container invocations.
_breakers: dict[str, CircuitBreaker] = {}


def _get_breaker(name: str) -> CircuitBreaker:
    breaker = _breakers.get(name)
    if breaker is None:
        s = get_settings()
        breaker = CircuitBreaker(
            fail_threshold=s.llm_circuit_fail_threshold,
            reset_seconds=s.llm_circuit_reset_seconds,
            enabled=s.llm_circuit_breaker_enabled,
            name=name,
        )
        _breakers[name] = breaker
    return breaker


def _ensure_loop() -> None:
    """(Re)build loop-bound state (clients, bucket) when the running loop changes."""
    global _bound_loop, _primary_client, _azure_client, _bucket
    loop = asyncio.get_running_loop()
    if _bound_loop is loop and _primary_client is not None:
        return
    s = get_settings()
    _primary_client = AsyncOpenAI(api_key=s.openai_api_key)
    _azure_client = (
        AsyncAzureOpenAI(
            api_key=s.azure_openai_api_key,
            azure_endpoint=s.azure_openai_endpoint,
            api_version=s.azure_openai_api_version,
        )
        if s.use_azure_fallback
        else None
    )
    _bucket = TokenBucket(
        rpm=s.llm_rate_limit_rpm,
        burst=s.llm_rate_limit_burst,
        max_wait=s.llm_rate_limit_max_wait_seconds,
    )
    _bound_loop = loop


def _providers(model: str) -> list[_Provider]:
    """Ordered providers to try: primary OpenAI, then Azure (same model) if set."""
    _ensure_loop()
    assert _primary_client is not None
    out = [_Provider("openai", _primary_client, model, _get_breaker("openai"))]
    if _azure_client is not None:
        out.append(
            _Provider("azure", _azure_client, get_settings().azure_deployment_name,
                      _get_breaker("azure"))
        )
    return out


def reset_state() -> None:
    """Test hook: drop all cached clients, bucket, and breakers."""
    global _bound_loop, _primary_client, _azure_client, _bucket
    _bound_loop = _primary_client = _azure_client = _bucket = None
    _breakers.clear()


# -- The executor --------------------------------------------------------------

async def structured_parse[M: BaseModel](
    *,
    system: str,
    user: str,
    response_format: type[M],
    model: str,
    max_tokens: int,
    label: str,
    semaphore: asyncio.Semaphore | None = None,
) -> LLMResult:
    """Run one structured-output call with full resilience.

    Tries each provider (primary, then Azure fallback) whose breaker is closed;
    within a provider, retries transient faults with backoff and breaks a token-
    ceiling repetition loop with an escalating frequency_penalty. Raises
    AIParsingError when every provider is exhausted or a non-retryable/content
    error occurs - the signal for the caller to degrade.
    """
    providers = _providers(model)
    assert _bucket is not None
    last_exc: BaseException | None = None
    skipped_open = 0

    for prov in providers:
        if not prov.breaker.allow():
            skipped_open += 1
            log.warning("llm_circuit_open", provider=prov.name, label=label)
            continue
        try:
            result = await _call_provider(prov, system, user, response_format,
                                          max_tokens, label, semaphore)
        except _ContentError as exc:
            # Provider is healthy - do not count against the breaker, and do not try
            # a same-model fallback (it would fail identically).
            raise AIParsingError(str(exc)) from (exc.__cause__ or exc)
        except _TransientExhausted as exc:
            prov.breaker.record_failure()
            last_exc = exc.__cause__ or exc
            log.warning("llm_provider_exhausted", provider=prov.name, label=label,
                        error=str(last_exc))
            continue  # fall through to the next provider
        else:
            prov.breaker.record_success()
            return result

    if skipped_open and last_exc is None:
        raise AIParsingError(f"[{label}] LLM circuit open; degrading without a call")
    raise AIParsingError(f"[{label}] all LLM providers exhausted: {last_exc}")


async def _call_provider[M: BaseModel](
    prov: _Provider,
    system: str,
    user: str,
    response_format: type[M],
    max_tokens: int,
    label: str,
    semaphore: asyncio.Semaphore | None,
) -> LLMResult:
    """One provider's attempt loop. Raises `_ContentError` or `_TransientExhausted`."""
    s = get_settings()
    penalty = 0.0

    for attempt in range(s.llm_max_retries):
        await _bucket.acquire()  # type: ignore[union-attr]  # set by _providers/_ensure_loop
        try:
            resp = await _raw_call(prov, system, user, response_format,
                                   max_tokens, penalty, semaphore, s)
        except LengthFinishReasonError as exc:
            # Retrying with identical params reproduces the loop deterministically
            # (temperature 0 + fixed seed); escalate the penalty instead. Fail fast
            # once it's spent so the caller can stub/degrade inside its deadline.
            if penalty < LOOP_BREAK_PENALTIES[-1]:
                penalty = next(p for p in LOOP_BREAK_PENALTIES if p > penalty)
                log.warning("llm_length_loop", provider=prov.name, label=label,
                            next_penalty=penalty)
                continue
            raise _ContentError(
                f"[{label}] hit the token ceiling (repetition loop) even with penalty {penalty}"
            ) from exc
        except Exception as exc:
            if is_retryable(exc):
                if attempt < s.llm_max_retries - 1:
                    delay = backoff_delay(
                        attempt,
                        base=s.llm_backoff_base_seconds,
                        jitter=s.llm_backoff_jitter,
                        cap=s.llm_backoff_max_seconds,
                        retry_after=retry_after_seconds(exc),
                    )
                    log.warning("llm_retry", provider=prov.name, label=label,
                                attempt=attempt + 1, retry_in=round(delay, 1),
                                error=type(exc).__name__)
                    await asyncio.sleep(delay)
                    continue
                raise _TransientExhausted() from exc  # let the caller try fallback
            # A non-retryable client/content error (e.g. 400): a fallback would fail
            # the same way - stop here.
            raise _ContentError(f"[{label}] non-retryable provider error: {exc}") from exc

        parsed = resp.choices[0].message.parsed
        if parsed is None:
            raise _ContentError(f"[{label}] empty structured output")
        return LLMResult(parsed=parsed, usage=resp.usage, provider=prov.name)

    # Only reachable if the final attempt was a (rare) length-loop `continue`.
    raise _TransientExhausted()


async def _raw_call[M: BaseModel](
    prov: _Provider,
    system: str,
    user: str,
    response_format: type[M],
    max_tokens: int,
    penalty: float,
    semaphore: asyncio.Semaphore | None,
    settings: Any,
):
    """The actual provider call, optionally under the shared concurrency semaphore."""
    async def _do():
        return await prov.client.beta.chat.completions.parse(
            model=prov.model,
            max_tokens=max_tokens,
            temperature=0.0,
            seed=settings.openai_seed,
            frequency_penalty=penalty,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=response_format,
        )

    if semaphore is not None:
        async with semaphore:
            return await _do()
    return await _do()
