"""
BaseAgent - shared structured-output LLM calling for every section agent.

Design notes:
  * One AsyncOpenAI client + semaphore are reused *per event loop* (connection-pool
    reuse across the ~5 Stage-2 agents + N per-role WorkAgent calls). The worker
    Lambda creates a fresh event loop on every invocation, so a process-global
    client/semaphore bound to a previous (now-closed) loop would raise
    "bound to a different event loop" (semaphore) or "Event loop is closed"
    (httpx pool) on the 2nd+ warm-container invocation. We therefore rebuild both
    whenever the running loop changes.
  * The semaphore caps in-flight LLM calls so a long travel-nurse resume
    (many roles -> many parallel per-role calls) can't blow the OpenAI TPM ceiling.
  * Structured outputs (`beta.chat.completions.parse`) keep the per-agent schema
    guarantee - agents never hand-parse JSON.
  * TokenMeter accumulates total tokens across the whole pipeline for billing.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import TypeVar

from openai import AsyncOpenAI, LengthFinishReasonError, RateLimitError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.core.logging import get_logger

log = get_logger(__name__)

_MODEL = TypeVar("_MODEL", bound=BaseModel)

_MAX_RETRIES   = 3
_BACKOFF_BASE  = 5     # seconds
_JITTER_FACTOR = 0.2

# Escalating frequency_penalty used ONLY to break a degenerate repetition loop
# (a strict-schema call that runs to the token ceiling - see the LengthFinishReasonError
# handler in `_structured_call`). Default calls stay at 0.0 so verbatim extraction is
# never biased away from words a résumé legitimately repeats.
_LOOP_BREAK_PENALTIES = (0.3, 0.6)

# Shared across all agents running on the SAME event loop. Rebuilt when the
# running loop changes (see module docstring - warm Lambda worker reuse).
_client: AsyncOpenAI | None = None
_semaphore: asyncio.Semaphore | None = None
_bound_loop: asyncio.AbstractEventLoop | None = None


def _ensure_for_loop() -> None:
    """(Re)build the client + semaphore if the running loop changed.

    Called from within an async context, so ``get_running_loop()`` is valid and
    the (synchronous, await-free) rebuild is atomic w.r.t. concurrent agents on
    the same loop - no locking needed.
    """
    global _client, _semaphore, _bound_loop
    loop = asyncio.get_running_loop()
    if _bound_loop is loop and _client is not None and _semaphore is not None:
        return
    _client = AsyncOpenAI(api_key=get_settings().openai_api_key)
    _semaphore = asyncio.Semaphore(get_settings().multi_agent_max_concurrency)
    _bound_loop = loop


def _get_client() -> AsyncOpenAI:
    _ensure_for_loop()
    assert _client is not None  # set by _ensure_for_loop
    return _client


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
        """Run one structured-output call with retry + concurrency control.

        Raises AIParsingError after retries are exhausted so the orchestrator can
        decide how to degrade (per-section default, or whole-pipeline fallback).

        `model` overrides the default `settings.openai_model` for this call, letting
        cheaper/faster models be assigned to the simpler section agents (model
        tiering) without changing the hard extraction stages. Defaults to the
        configured model so behaviour is unchanged unless a caller opts in.
        """
        settings = get_settings()
        client   = _get_client()
        sem      = _get_semaphore()
        last_exc: Exception | None = None
        penalty  = 0.0  # bumped only after a repetition-loop (length) failure

        # Resolve the model once: an explicit arg wins; otherwise a FAST_TIER agent
        # uses the configured fast model when one is set; otherwise the primary model.
        chosen_model = model or (
            settings.openai_model_fast if self.FAST_TIER and settings.openai_model_fast
            else settings.openai_model
        )

        for attempt in range(_MAX_RETRIES):
            try:
                t0 = time.monotonic()
                async with sem:
                    resp = await client.beta.chat.completions.parse(
                        model=chosen_model,
                        max_tokens=max_tokens or settings.openai_max_tokens,
                        temperature=0.0,
                        seed=settings.openai_seed,
                        frequency_penalty=penalty,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        response_format=response_format,
                    )
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                parsed = resp.choices[0].message.parsed
                usage = resp.usage
                tokens = usage.total_tokens if usage else 0
                prompt_tok = usage.prompt_tokens if usage else 0
                completion_tok = usage.completion_tokens if usage else 0
                # Prompt-cache hits are reported under prompt_tokens_details when the
                # provider supplies them; absent on older SDKs/models, hence getattr.
                details = getattr(usage, "prompt_tokens_details", None) if usage else None
                cached_tok = getattr(details, "cached_tokens", 0) or 0 if details else 0
                meter.add(
                    self.name, tokens, prompt=prompt_tok, completion=completion_tok,
                    cached=cached_tok, ms=elapsed_ms,
                )
                if parsed is None:
                    raise AIParsingError(f"[{self.name}] empty structured output")
                return parsed

            except LengthFinishReasonError as exc:
                # The model ran to the token ceiling. With a large strict schema, small
                # models sometimes fall into a degenerate repetition loop (e.g. emitting
                # "Radiologic Tech / Radiologic Technologist / ..." until max_tokens) and
                # never close the JSON. Retrying with identical params just reproduces it
                # deterministically (temperature 0 + fixed seed) and burns a full
                # max_tokens of latency each time - which is what silently cancelled the
                # whole WorkExperienceAgent stage and dropped every role. Break the loop
                # with an escalating frequency_penalty; fail fast once it's spent so the
                # caller (WorkExperienceAgent.run) can stub the role inside its deadline.
                last_exc = exc
                if penalty < _LOOP_BREAK_PENALTIES[-1]:
                    penalty = next(p for p in _LOOP_BREAK_PENALTIES if p > penalty)
                    log.warning("agent_length_loop", agent=self.name, next_penalty=penalty)
                    continue
                raise AIParsingError(
                    f"[{self.name}] hit the token ceiling (repetition loop) even with penalty {penalty}"
                ) from exc

            except RateLimitError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    base   = _BACKOFF_BASE * (2 ** attempt)
                    jitter = base * _JITTER_FACTOR * (2 * random.random() - 1)
                    await asyncio.sleep(max(base + jitter, 1.0))
                else:
                    raise AIParsingError(f"[{self.name}] rate limited after {_MAX_RETRIES} attempts") from exc

            except AIParsingError:
                raise

            except Exception as exc:
                last_exc = exc
                log.warning("agent_attempt_failed", agent=self.name, attempt=attempt + 1, error=str(exc))
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                else:
                    raise AIParsingError(f"[{self.name}] failed after {_MAX_RETRIES} attempts: {exc}") from exc

        raise AIParsingError(f"[{self.name}] failed: {last_exc}")
