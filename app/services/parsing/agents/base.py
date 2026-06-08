"""
BaseAgent — shared structured-output LLM calling for every section agent.

Design notes:
  • One AsyncOpenAI client is reused process-wide (connection-pool reuse across
    the ~5 Stage-2 agents + N per-role WorkAgent calls).
  • A global semaphore caps in-flight LLM calls so a long travel-nurse résumé
    (many roles → many parallel per-role calls) can't blow the OpenAI TPM ceiling.
  • Structured outputs (`beta.chat.completions.parse`) keep the per-agent schema
    guarantee — agents never hand-parse JSON.
  • TokenMeter accumulates total tokens across the whole pipeline for billing.
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

# Shared across all agents in the process.
_client: AsyncOpenAI | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=get_settings().openai_api_key)
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().multi_agent_max_concurrency)
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
