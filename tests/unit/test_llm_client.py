"""Executor tests for app.services.llm.client.structured_parse:
transient retry -> Azure fallback, circuit-breaker short-circuit to failure (the
signal that degrades to the deterministic floor), and no fallback on content errors.
"""

from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, LengthFinishReasonError, RateLimitError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AIParsingError
from app.services.llm import client


class _Out(BaseModel):
    ok: bool = True


def _resp(parsed=None, tokens=10):
    usage = SimpleNamespace(total_tokens=tokens, prompt_tokens=tokens, completion_tokens=0)
    message = SimpleNamespace(parsed=parsed if parsed is not None else _Out())
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _rate_limit():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return RateLimitError("rate", response=resp, body=None)


def _length_error():
    # LengthFinishReasonError requires a completion arg in the SDK; a bare instance
    # is enough for the isinstance check in the executor.
    return LengthFinishReasonError.__new__(LengthFinishReasonError)


@pytest.fixture
def base_settings(monkeypatch):
    """Fast, deterministic settings: no real backoff sleeps, breaker off unless a
    test enables it, no Azure unless a test configures it."""
    s = get_settings()
    monkeypatch.setattr(s, "llm_max_retries", 1)          # exhaust transient immediately
    monkeypatch.setattr(s, "llm_circuit_breaker_enabled", False)
    monkeypatch.setattr(s, "llm_rate_limit_rpm", 0)       # bucket disabled
    monkeypatch.setattr(s, "azure_openai_api_key", "")
    monkeypatch.setattr(s, "azure_openai_endpoint", "")
    client.reset_state()
    yield s
    client.reset_state()


def _enable_azure(monkeypatch, s):
    monkeypatch.setattr(s, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(s, "azure_openai_endpoint", "https://unit.openai.azure.com/")
    monkeypatch.setattr(s, "azure_openai_api_version", "2024-10-21")
    client.reset_state()


def _script_raw(monkeypatch, outcomes: dict[str, list]):
    """Fake _raw_call driven by a per-provider list of outcomes (exception or resp)."""
    calls: dict[str, int] = {}

    async def fake_raw(prov, system, user, response_format, max_tokens, penalty, semaphore, settings):
        i = calls.get(prov.name, 0)
        calls[prov.name] = i + 1
        seq = outcomes[prov.name]
        outcome = seq[min(i, len(seq) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client, "_raw_call", fake_raw)
    return calls


async def _call():
    return await client.structured_parse(
        system="s", user="u", response_format=_Out,
        model="gpt-4.1-mini", max_tokens=1024, label="test",
    )


async def test_success_on_primary_no_fallback(base_settings, monkeypatch):
    calls = _script_raw(monkeypatch, {"openai": [_resp()]})
    result = await _call()
    assert result.provider == "openai"
    assert calls == {"openai": 1}


async def test_transient_exhaustion_falls_back_to_azure(base_settings, monkeypatch):
    _enable_azure(monkeypatch, base_settings)
    calls = _script_raw(monkeypatch, {
        "openai": [_rate_limit()],   # primary 429, retries exhausted (max_retries=1)
        "azure":  [_resp()],         # fallback succeeds
    })
    result = await _call()
    assert result.provider == "azure"
    assert calls["openai"] == 1 and calls["azure"] == 1


async def test_all_providers_transient_raises_ai_error(base_settings, monkeypatch):
    _enable_azure(monkeypatch, base_settings)
    _script_raw(monkeypatch, {"openai": [_rate_limit()], "azure": [_rate_limit()]})
    with pytest.raises(AIParsingError):
        await _call()


async def test_open_breaker_short_circuits_to_floor(base_settings, monkeypatch):
    # Breaker on, threshold 1, no Azure: one failure opens the primary breaker; the
    # next call is short-circuited (no provider available) -> AIParsingError -> floor.
    monkeypatch.setattr(base_settings, "llm_circuit_breaker_enabled", True)
    monkeypatch.setattr(base_settings, "llm_circuit_fail_threshold", 1)
    monkeypatch.setattr(base_settings, "llm_circuit_reset_seconds", 999)
    client.reset_state()

    calls = _script_raw(monkeypatch, {"openai": [_rate_limit(), _resp()]})
    with pytest.raises(AIParsingError):
        await _call()                 # fails -> opens breaker
    with pytest.raises(AIParsingError):
        await _call()                 # short-circuited, no call made
    assert calls["openai"] == 1       # second call never hit the provider


async def test_length_loop_is_content_error_no_fallback(base_settings, monkeypatch):
    # max_retries high enough to exhaust the 2 penalties; length error every time.
    monkeypatch.setattr(base_settings, "llm_max_retries", 3)
    _enable_azure(monkeypatch, base_settings)
    calls = _script_raw(monkeypatch, {
        "openai": [_length_error(), _length_error(), _length_error()],
        "azure":  [_resp()],
    })
    with pytest.raises(AIParsingError):
        await _call()
    assert "azure" not in calls        # same model would loop identically - no fallback


async def test_non_retryable_400_no_fallback(base_settings, monkeypatch):
    _enable_azure(monkeypatch, base_settings)
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    bad = BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    calls = _script_raw(monkeypatch, {"openai": [bad], "azure": [_resp()]})
    with pytest.raises(AIParsingError):
        await _call()
    assert "azure" not in calls
