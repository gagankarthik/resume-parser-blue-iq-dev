"""
BaseAgent - shared structured-output LLM calling for every section agent.

Design notes:
  * The actual provider call + resilience (retry/backoff, circuit breaker, Azure
    same-model fallback) lives in `app.services.llm.client.structured_parse`, shared
    with the single-shot parser and the specialty-AI tier so the policy is defined
    once. This module owns only the per-loop concurrency semaphore and token metering.
  * The semaphore caps in-flight LLM calls so a long travel-nurse resume (many roles
    -> many parallel per-role calls) can't blow the OpenAI TPM ceiling. It is rebuilt
    per event loop: the worker Lambda creates a fresh loop each invocation, and a
    semaphore bound to a previous (now-closed) loop would raise "bound to a different
    event loop" on warm-container reuse.
  * Structured outputs keep the per-agent schema guarantee - agents never hand-parse.
  * TokenMeter accumulates total tokens across the whole pipeline for billing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TypeVar, cast

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.llm.client import structured_parse

log = get_logger(__name__)

_MODEL = TypeVar("_MODEL", bound=BaseModel)

# Per-event-loop concurrency gate, rebuilt when the running loop changes (see the
# module docstring - warm Lambda worker reuse).
_semaphore: asyncio.Semaphore | None = None
_bound_loop: asyncio.AbstractEventLoop | None = None


def _ensure_for_loop() -> None:
    """(Re)build the semaphore if the running loop changed.

    Called from within an async context, so ``get_running_loop()`` is valid and the
    (synchronous, await-free) rebuild is atomic w.r.t. concurrent agents on the same
    loop - no locking needed.
    """
    global _semaphore, _bound_loop
    loop = asyncio.get_running_loop()
    if _bound_loop is loop and _semaphore is not None:
        return
    _semaphore = asyncio.Semaphore(get_settings().multi_agent_max_concurrency)
    _bound_loop = loop


def _get_semaphore() -> asyncio.Semaphore:
    _ensure_for_loop()
    assert _semaphore is not None  # set by _ensure_for_loop
    return _semaphore


@dataclass
class TokenMeter:
    """Process-local token accumulator shared across one pipeline run.

    Tracks total tokens plus a prompt/completion split and per-agent latency so a
    slow or expensive stage can be pinpointed without a profiler. The split matters
    for cost tuning: the per-role WorkAgent fan-out is prompt-heavy (it re-sends the
    résumé as context on every call), which is exactly what prompt caching targets.
    """

    total: int = 0
    prompt_total: int = 0
    completion_total: int = 0
    cached_total: int = 0
    # Per-agent breakdowns, useful for debugging which stage is expensive/slow.
    by_agent: dict[str, int] = field(default_factory=dict)
    ms_by_agent: dict[str, float] = field(default_factory=dict)
    calls_by_agent: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        agent: str,
        tokens: int,
        *,
        prompt: int = 0,
        completion: int = 0,
        cached: int = 0,
        ms: float = 0.0,
    ) -> None:
        self.total += tokens
        self.prompt_total += prompt
        self.completion_total += completion
        self.cached_total += cached
        self.by_agent[agent] = self.by_agent.get(agent, 0) + tokens
        self.ms_by_agent[agent] = self.ms_by_agent.get(agent, 0.0) + ms
        self.calls_by_agent[agent] = self.calls_by_agent.get(agent, 0) + 1


class BaseAgent:
    """All section agents inherit from this."""

    name: str = "BaseAgent"
    # When True, this agent uses settings.openai_model_fast (if configured) instead
    # of the primary model. Set on the simpler section agents only; the hard stages
    # (structure, work, validator) leave it False so they always use the primary model.
    FAST_TIER: bool = False

    async def _structured_call(
        self,
        system: str,
        user: str,
        response_format: type[_MODEL],
        meter: TokenMeter,
        *,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> _MODEL:
        """Run one structured-output call, recording usage into `meter`.

        Delegates the provider call + resilience (retry/backoff, circuit breaker,
        Azure fallback) to `app.services.llm.client.structured_parse`, under this
        module's per-loop concurrency semaphore. Raises AIParsingError after the
        resilience layer is exhausted so the orchestrator can decide how to degrade
        (per-section default, or whole-pipeline fallback).

        `model` overrides the default `settings.openai_model` for this call, letting
        cheaper/faster models be assigned to the simpler section agents (model
        tiering) without changing the hard extraction stages. Defaults to the
        configured model so behaviour is unchanged unless a caller opts in.
        """
        settings = get_settings()

        # Resolve the model once: an explicit arg wins; otherwise a FAST_TIER agent
        # uses the configured fast model when one is set; otherwise the primary model.
        chosen_model = model or (
            settings.openai_model_fast if self.FAST_TIER and settings.openai_model_fast
            else settings.openai_model
        )

        t0 = time.monotonic()
        result = await structured_parse(
            system=system,
            user=user,
            response_format=response_format,
            model=chosen_model,
            max_tokens=max_tokens or settings.openai_max_tokens,
            label=self.name,
            semaphore=_get_semaphore(),
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        usage = result.usage
        tokens = usage.total_tokens if usage else 0
        prompt_tok = usage.prompt_tokens if usage else 0
        completion_tok = usage.completion_tokens if usage else 0
        # Prompt-cache hits are reported under prompt_tokens_details when the provider
        # supplies them; absent on older SDKs/models, hence getattr.
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cached_tok = getattr(details, "cached_tokens", 0) or 0 if details else 0
        meter.add(
            self.name, tokens, prompt=prompt_tok, completion=completion_tok,
            cached=cached_tok, ms=elapsed_ms,
        )
        return cast(_MODEL, result.parsed)
