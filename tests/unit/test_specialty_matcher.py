"""
Tiered specialty matcher tests.

Covers the deterministic tiers (name / full_name / keywords), the no-catalog
fallback (canonical names with id=None), the unmatched-kept-for-review path,
dedup/order, and the batched AI tier (tier 4) with its no-op guards and a mocked
LLM call.
"""

import json

import pytest

from app.models.schemas import ExperienceItem, ParsedResumeAI
from app.services.normalization import specialty_catalog, specialty_matcher
from app.services.parsing.agents.specialty import SpecialtyAIResult, SpecialtyMatchAgent


@pytest.fixture(autouse=True)
def _reset_catalog():
    yield
    specialty_catalog.reload(None)


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "cat.json"
    path.write_text(json.dumps([
        {"id": "1042", "specialty": "Medical Surgical",
         "full_name": "Medical Surgical / Telemetry", "keywords": ["floor nursing"],
         "group": "Med Surg / Tele"},
        {"id": 2001, "specialty": "Intensive Care Unit",
         "full_name": "Critical Care ICU", "keywords": ["critical care unit"]},
    ]), encoding="utf-8")
    return specialty_catalog.reload(str(path))


# ── Deterministic tiers (with catalog) ────────────────────────────────────────

def test_tier1_name_via_abbreviation(catalog):
    m = specialty_matcher.match("Med Surg")          # abbrev → "Medical Surgical"
    assert (m.specialty_id, m.match_tier, m.confidence, m.matched) == ("1042", "name", 1.0, True)
    assert m.name == "Medical Surgical"
    assert m.raw == "Med Surg"


def test_tier2_full_name(catalog):
    m = specialty_matcher.match("Critical Care ICU")
    assert (m.specialty_id, m.match_tier, m.confidence) == ("2001", "full_name", 0.95)
    assert m.name == "Intensive Care Unit"


def test_tier3_keyword(catalog):
    m = specialty_matcher.match("floor nursing")
    assert (m.specialty_id, m.match_tier, m.confidence) == ("1042", "keywords", 0.80)


def test_unmatched_kept_without_id(catalog):
    m = specialty_matcher.match("Underwater Basket Weaving")
    assert m.specialty_id is None
    assert m.matched is False
    assert m.match_tier is None
    assert m.confidence == 0.0
    assert m.name == "Underwater Basket Weaving"     # not dropped


# ── No-catalog fallback ───────────────────────────────────────────────────────

def test_no_catalog_resolves_name_without_id():
    specialty_catalog.reload(None)
    m = specialty_matcher.match("ICU")
    assert m.name == "Intensive Care Unit"           # canonicalised by the taxonomy
    assert m.specialty_id is None                     # no catalog → no id
    assert m.matched is False
    assert m.match_tier == "name"
    assert m.confidence == 1.0


def test_batch_dedups_by_canonical_name(catalog):
    out = specialty_matcher.match_batch(["ICU", "Intensive Care Unit", "Med Surg"])
    # "ICU" and "Intensive Care Unit" collapse to one entry.
    assert [m.name for m in out] == ["Intensive Care Unit", "Medical Surgical"]


# ── Tier 4: batched AI shortlist ──────────────────────────────────────────────

def _parsed_with_unmatched():
    parsed = ParsedResumeAI(experience=[
        ExperienceItem(company="X", role="RN", specialties=["Cardiac Drip Unit"]),
    ])
    # Run deterministic matching so the entry is genuinely unmatched.
    from app.services.normalization.normalizer import normalize
    return normalize(parsed)


async def test_tier4_noop_without_catalog():
    specialty_catalog.reload(None)
    parsed = _parsed_with_unmatched()
    tokens = await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)
    assert tokens == 0
    assert parsed.experience[0].specialties[0].specialty_id is None


async def test_tier4_noop_when_nothing_unmatched(catalog):
    parsed = ParsedResumeAI(experience=[
        ExperienceItem(company="X", role="RN", specialties=["ICU"]),
    ])
    from app.services.normalization.normalizer import normalize
    normalize(parsed)
    assert parsed.experience[0].specialties[0].matched is True
    tokens = await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)
    assert tokens == 0


async def test_tier4_applies_validated_ai_match(catalog, monkeypatch):
    parsed = _parsed_with_unmatched()
    sm = parsed.experience[0].specialties[0]
    assert sm.matched is False and sm.raw == "Cardiac Drip Unit"

    async def fake_call(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 7)
        return SpecialtyAIResult(matches=[
            {"raw": "Cardiac Drip Unit", "specialty_id": "2001", "confidence": 0.9},
        ])

    monkeypatch.setattr(SpecialtyMatchAgent, "_structured_call", fake_call)
    tokens = await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)

    assert tokens == 7
    out = parsed.experience[0].specialties[0]
    assert out.specialty_id == "2001"
    assert out.matched is True
    assert out.match_tier == "ai"
    assert out.confidence == specialty_matcher.CONF_AI_MAX  # 0.9 capped to 0.70


async def test_tier4_drops_hallucinated_id(catalog, monkeypatch):
    parsed = _parsed_with_unmatched()

    async def fake_call(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 3)
        return SpecialtyAIResult(matches=[
            {"raw": "Cardiac Drip Unit", "specialty_id": "9999", "confidence": 0.99},
        ])

    monkeypatch.setattr(SpecialtyMatchAgent, "_structured_call", fake_call)
    await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)

    out = parsed.experience[0].specialties[0]
    assert out.specialty_id is None       # off-shortlist id rejected
    assert out.matched is False
