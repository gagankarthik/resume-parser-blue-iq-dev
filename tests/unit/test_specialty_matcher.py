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
    specialty_catalog.reload("")   # empty; keep the bundled default out of tests


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


# -- Deterministic tiers (with catalog) ----------------------------------------

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


# -- Candidate extraction: a specialty embedded in a phrase still resolves ------

@pytest.fixture
def icu_catalog(tmp_path):
    path = tmp_path / "icu.json"
    path.write_text(json.dumps([
        {"id": "82",  "specialty": "NICU", "full_name": "Neonatal Intensive Care Unit"},
        {"id": "155", "specialty": "SICU", "full_name": "Surgical Intensive Care Unit"},
        {"id": "18",  "specialty": "CCU",  "full_name": "Critical Care Unit"},
    ]), encoding="utf-8")
    return specialty_catalog.reload(str(path))


def test_parenthetical_acronym_resolves_to_name_tier(icu_catalog):
    # A written-out unit with its acronym in parens resolves via the acronym.
    m = specialty_matcher.match("Surgical Intensive Care Unit (SICU)")
    assert (m.specialty_id, m.match_tier, m.confidence) == ("155", "name", 1.0)
    assert m.name == "SICU"


def test_slash_joined_phrase_resolves_via_full_name(icu_catalog):
    m = specialty_matcher.match("Critical Care Unit/Cardiac Care Unit")
    assert (m.specialty_id, m.match_tier, m.confidence) == ("18", "full_name", 0.95)


def test_fuzzy_tier_resolves_typo(icu_catalog):
    # A near-identical typo resolves via the conservative fuzzy tier.
    m = specialty_matcher.match("Neonatal Intensive Care Unt")   # missing 'i'
    assert m.matched is True and m.specialty_id == "82"
    assert m.match_tier == "fuzzy"
    assert 0.90 <= m.confidence <= specialty_matcher.CONF_FUZZY_MAX


def test_leading_token_resolves_descriptor(icu_catalog):
    # A specialty that opens a longer descriptor resolves via its leading token,
    # so it does not survive as a separate unmatched (duplicate) entry.
    m = specialty_matcher.match("NICU Level III and IV")
    assert (m.specialty_id, m.match_tier, m.confidence) == ("82", "name", 1.0)


def test_fuzzy_exact_match_scores_one(tmp_path):
    # A candidate that is IDENTICAL (after normalization) to a catalog full name but
    # is not in the exact index path still scores 1.0 via the fuzzy tier - never
    # capped to the fuzzy max, never sent to AI.
    path = tmp_path / "c.json"
    path.write_text(json.dumps([
        {"id": "82", "specialty": "NICU", "full_name": "Neonatal Intensive Care Unit"},
    ]), encoding="utf-8")
    specialty_catalog.reload(str(path))
    hit = specialty_matcher._fuzzy_lookup(
        specialty_catalog.get_catalog(), ["neonatal intensive care unit"], [],
    )
    assert hit is not None
    _rec, conf, tier = hit
    assert conf == 1.0 and tier == "full_name"


def test_fuzzy_tier_ignores_unrelated_phrase(icu_catalog):
    # A phrase that is not close to any specialty stays unmatched (no forced match).
    m = specialty_matcher.match("Provided direct patient care and charting")
    assert m.matched is False and m.specialty_id is None


def test_duplicate_specialties_collapse_to_one(icu_catalog):
    # "CCU" and "Critical Care Unit" both resolve to id 18 -> a single entry.
    out = specialty_matcher.match_batch(["CCU", "Critical Care Unit"], "RN")
    assert len(out) == 1
    assert out[0].specialty_id == "18"


def test_base_specialty_preferred_over_variant_on_shared_full_name(tmp_path):
    # "Emergency Room" is the full name of both "ER" and "Pediatric ER"; a bare phrase
    # must resolve to the base specialty regardless of catalog order.
    path = tmp_path / "er.json"
    path.write_text(json.dumps([
        {"id": "120", "specialty": "Pediatric ER", "full_name": "Emergency Room",
         "profession": "RN"},
        {"id": "45", "specialty": "ER", "full_name": "Emergency Room",
         "profession": "RN"},
    ]), encoding="utf-8")
    specialty_catalog.reload(str(path))
    m = specialty_matcher.match("Emergency Room", "RN")
    assert m.specialty_id == "45" and m.name == "ER"


def test_duplicated_exact_phrase_matches_deterministically_at_one(icu_catalog):
    # A doubled specialty ("NICU NICU") must resolve deterministically at 1.0 via the
    # NAME tier - it must NOT miss the deterministic tiers and fall to the AI cap
    # (0.7). Regression guard for the "exact match returning 0.7" bug.
    for raw in ("NICU NICU", "CCU CCU CCU"):
        m = specialty_matcher.match(raw)
        assert m.matched is True
        assert m.match_tier == "name"
        assert m.confidence == 1.0
    m = specialty_matcher.match("NICU NICU")
    assert m.specialty_id == "82" and m.raw == "NICU"      # raw also de-duplicated


def test_tidy_raw_collapses_adjacent_and_phrase_repeats():
    assert specialty_matcher._tidy_raw("ICU ICU ICU") == "ICU"
    assert specialty_matcher._tidy_raw("Critical Care Unit Critical Care Unit") == "Critical Care Unit"
    # A long run-on is bounded and left legible (no visible repeat).
    tidy = specialty_matcher._tidy_raw(
        "Neonatal Intensive Care Unit (NICU) Level III and Level IV "
        "Neonatal Care Unit (NICU) Level III and Level IV including surgical patients."
    )
    assert tidy is not None and len(tidy.split()) <= 9
    # A clean single specialty is untouched.
    assert specialty_matcher._tidy_raw("Med Surg / Tele") == "Med Surg / Tele"


def test_garbled_runon_specialty_resolves_and_raw_is_tidied(icu_catalog):
    garbled = ("Neonatal Intensive Care Unit (NICU) Level III and Level IV "
               "Neonatal Care Unit (NICU) Level III and Level IV including "
               "high frequency ventilation and surgical patients.")
    m = specialty_matcher.match(garbled, "RN")
    # The parenthetical NICU still lands an exact, high-confidence id - not 0.7.
    assert (m.specialty_id, m.match_tier, m.confidence) == ("82", "name", 1.0)
    # raw is preserved for audit but collapsed to a legible, bounded string.
    assert m.raw is not None and len(m.raw.split()) <= 9
    assert "NICU" in m.raw


# -- Profession-scoped id selection --------------------------------------------

@pytest.fixture
def prof_catalog(tmp_path):
    # Same name "ICU" under two professions with different ids; "LPN/ LVN" pair.
    path = tmp_path / "prof.json"
    path.write_text(json.dumps([
        {"id": "56",  "specialty": "ICU", "profession": "RN"},
        {"id": "757", "specialty": "ICU", "profession": "CNA"},
        {"id": "411", "specialty": "ICU", "profession": "LPN/ LVN"},
        {"id": "999", "specialty": "BICU", "full_name": "Burn Intensive Care Unit",
         "keywords": ["burn icu", "burn unit"], "profession": "RN"},
    ]), encoding="utf-8")
    return specialty_catalog.reload(str(path))


def test_profession_scopes_shared_name(prof_catalog):
    assert specialty_matcher.match("ICU", "RN").specialty_id == "56"
    assert specialty_matcher.match("ICU", "CNA").specialty_id == "757"


def test_profession_pair_splits_lpn_lvn(prof_catalog):
    # "LPN/ LVN" is indexed under both "lpn" and "lvn".
    assert specialty_matcher.match("ICU", "LPN").specialty_id == "411"
    assert specialty_matcher.match("ICU", "LVN").specialty_id == "411"


def test_full_title_aliases_to_catalog_code(prof_catalog):
    # Resumes spell professions out; they must scope to the catalog's short code.
    assert specialty_matcher.match("ICU", "Registered Nurse").specialty_id == "56"
    assert specialty_matcher.match("ICU", "Certified Nursing Assistant").specialty_id == "757"
    assert specialty_matcher.match("ICU", "Licensed Practical Nurse").specialty_id == "411"


def test_unknown_profession_falls_back_to_flat(prof_catalog):
    # No PT "ICU" -> falls back to the flat first-wins record (RN, listed first).
    assert specialty_matcher.match("ICU", "PT").specialty_id == "56"


def test_keyword_tier_differentiates_subtype(prof_catalog):
    # "burn unit" isn't a taxonomy name/full-name, so the curated keyword resolves it.
    m = specialty_matcher.match("burn unit", "RN")
    assert (m.specialty_id, m.match_tier) == ("999", "keywords")


def test_unmatched_kept_without_id(catalog):
    m = specialty_matcher.match("Underwater Basket Weaving")
    assert m.specialty_id is None
    assert m.matched is False
    assert m.match_tier is None
    assert m.confidence == 0.0
    assert m.name == "Underwater Basket Weaving"     # not dropped


# -- No-catalog fallback -------------------------------------------------------

def test_no_catalog_resolves_name_without_id():
    specialty_catalog.reload("")
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


# -- Tier 4: batched AI shortlist ----------------------------------------------

def _parsed_with_unmatched():
    parsed = ParsedResumeAI(experience=[
        ExperienceItem(company="X", role="RN", specialties=["Cardiac Drip Unit"]),
    ])
    # Run deterministic matching so the entry is genuinely unmatched.
    from app.services.normalization.normalizer import normalize
    return normalize(parsed)


async def test_tier4_noop_without_catalog():
    specialty_catalog.reload("")
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


def _parsed_with_phrase(phrase: str) -> ParsedResumeAI:
    parsed = ParsedResumeAI(experience=[
        ExperienceItem(company="X", role="RN", specialties=[phrase]),
    ])
    from app.services.normalization.normalizer import normalize
    return normalize(parsed)


async def test_tier4_applies_validated_ai_match(catalog, monkeypatch):
    # A lexically plausible phrase (shares "critical" with the record's keyword) that
    # the deterministic + fuzzy tiers still miss -> the AI tier applies the pick.
    parsed = _parsed_with_phrase("Critical Care Overflow")
    sm = parsed.experience[0].specialties[0]
    assert sm.matched is False

    async def fake_call(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 7)
        return SpecialtyAIResult(matches=[
            {"raw": "Critical Care Overflow", "specialty_id": "2001", "confidence": 0.9},
        ])

    monkeypatch.setattr(SpecialtyMatchAgent, "_structured_call", fake_call)
    tokens = await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)

    assert tokens == 7
    out = parsed.experience[0].specialties[0]
    assert out.specialty_id == "2001"
    assert out.matched is True
    assert out.match_tier == "ai"
    assert out.confidence == specialty_matcher.CONF_AI_MAX  # 0.9 capped to 0.70


async def test_tier4_drops_implausible_ai_pick(catalog, monkeypatch):
    # A confident pick with NO lexical footing ("Cardiac Drip Unit" -> "Intensive Care
    # Unit") is a hallucination risk - dropped and left unmatched for human review.
    parsed = _parsed_with_phrase("Cardiac Drip Unit")

    async def fake_call(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 4)
        return SpecialtyAIResult(matches=[
            {"raw": "Cardiac Drip Unit", "specialty_id": "2001", "confidence": 0.9},
        ])

    monkeypatch.setattr(SpecialtyMatchAgent, "_structured_call", fake_call)
    await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)

    out = parsed.experience[0].specialties[0]
    assert out.specialty_id is None
    assert out.matched is False


async def test_tier4_drops_low_confidence_ai_pick(catalog, monkeypatch):
    # Even a plausible pick is dropped when the model's own certainty is below the
    # acceptance floor - prefer "unmatched, review" over a weak guess.
    parsed = _parsed_with_phrase("Critical Care Overflow")

    async def fake_call(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 4)
        return SpecialtyAIResult(matches=[
            {"raw": "Critical Care Overflow", "specialty_id": "2001", "confidence": 0.4},
        ])

    monkeypatch.setattr(SpecialtyMatchAgent, "_structured_call", fake_call)
    await specialty_matcher.resolve_unmatched_with_ai(parsed, budget=10)

    out = parsed.experience[0].specialties[0]
    assert out.specialty_id is None
    assert out.matched is False


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
