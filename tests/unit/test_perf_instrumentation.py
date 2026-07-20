"""Tests for the perf/cost instrumentation and tiering hooks added for the
accuracy/latency improvement work: TokenMeter's prompt/completion/latency split,
per-agent model tiering (FAST_TIER + openai_model_fast), and the benchmark's
percentile helper.
"""

from app.core.config import get_settings
from app.services.parsing.agents.base import BaseAgent, TokenMeter
from app.services.parsing.agents.credentials import CredentialsAgent
from app.services.parsing.agents.education import EducationAgent
from app.services.parsing.agents.structure import StructureAgent
from app.services.parsing.agents.supplemental import SupplementalAgent
from app.services.parsing.agents.work import WorkExperienceAgent
from benchmark.run import _pct


def test_token_meter_tracks_split_and_latency():
    m = TokenMeter()
    m.add("WorkExperienceAgent", 100, prompt=80, completion=20, cached=40, ms=1200.0)
    m.add("WorkExperienceAgent", 50, prompt=40, completion=10, cached=30, ms=800.0)
    m.add("EducationAgent", 30, prompt=25, completion=5, ms=300.0)
    assert m.total == 180
    assert m.prompt_total == 145 and m.completion_total == 35 and m.cached_total == 70
    assert m.by_agent["WorkExperienceAgent"] == 150
    assert m.ms_by_agent["WorkExperienceAgent"] == 2000.0
    assert m.calls_by_agent["WorkExperienceAgent"] == 2
    assert m.calls_by_agent["EducationAgent"] == 1


def test_hard_stages_are_not_fast_tier():
    # The accuracy-critical stages must always use the primary model.
    assert StructureAgent.FAST_TIER is False
    assert WorkExperienceAgent.FAST_TIER is False
    assert BaseAgent.FAST_TIER is False


def test_simple_sections_are_fast_tier():
    assert EducationAgent.FAST_TIER is True
    assert CredentialsAgent.FAST_TIER is True
    assert SupplementalAgent.FAST_TIER is True


def test_model_tiering_defaults_to_primary_model():
    # With no fast model configured (default), nothing changes: a FAST_TIER agent
    # still resolves to the primary model. (Mirrors the resolution in _structured_call.)
    s = get_settings()
    assert s.openai_model_fast == ""
    agent = EducationAgent()
    chosen = agent.FAST_TIER and s.openai_model_fast or s.openai_model
    assert chosen == s.openai_model


def test_model_tiering_uses_fast_model_when_set(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openai_model_fast", "gpt-4.1-nano")
    edu, work = EducationAgent(), WorkExperienceAgent()
    edu_model = (s.openai_model_fast if edu.FAST_TIER and s.openai_model_fast else s.openai_model)
    work_model = (s.openai_model_fast if work.FAST_TIER and s.openai_model_fast else s.openai_model)
    assert edu_model == "gpt-4.1-nano"      # simple section -> fast model
    assert work_model == s.openai_model     # hard stage -> primary model


def test_benchmark_percentile():
    assert _pct([], 50) == 0.0
    assert _pct([5.0], 95) == 5.0
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pct(data, 50) == 3.0
    assert _pct(data, 100) == 5.0
    assert _pct(data, 0) == 1.0
