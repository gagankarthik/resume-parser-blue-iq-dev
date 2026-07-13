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
from dataclasses import dataclass, field
from typing import TypeVar

from openai import AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.core.logging import get_logger

log = get_logger(__name__)

_MODEL = TypeVar("_MODEL", bound=BaseModel)

_MAX_RETRIES   = 3
_BACKOFF_BASE  = 5     # seconds
_JITTER_FACTOR = 0.2

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
    """Process-local token accumulator shared across one pipeline run."""

    total: int = 0
    # Per-agent breakdown, useful for debugging which stage is expensive.
    by_agent: dict[str, int] = field(default_factory=dict)

    def add(self, agent: str, tokens: int) -> None:
        self.total += tokens
        self.by_agent[agent] = self.by_agent.get(agent, 0) + tokens


class BaseAgent:
    """All section agents inherit from this."""

    name: str = "BaseAgent"

    async def _structured_call(
        self,
        system: str,
        user: str,
        response_format: type[_MODEL],
        meter: TokenMeter,
        *,
        max_tokens: int | None = None,
    ) -> _MODEL:
        """Run one structured-output call with retry + concurrency control.

        Raises AIParsingError after retries are exhausted so the orchestrator can
        decide how to degrade (per-section default, or whole-pipeline fallback).
        """
        settings = get_settings()
        client   = _get_client()
        sem      = _get_semaphore()
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                async with sem:
                    resp = await client.beta.chat.completions.parse(
                        model=settings.openai_model,
                        max_tokens=max_tokens or settings.openai_max_tokens,
                        temperature=0.0,
                        seed=settings.openai_seed,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        response_format=response_format,
                    )
                parsed = resp.choices[0].message.parsed
                tokens = resp.usage.total_tokens if resp.usage else 0
                meter.add(self.name, tokens)
                if parsed is None:
                    raise AIParsingError(f"[{self.name}] empty structured output")
                return parsed

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
