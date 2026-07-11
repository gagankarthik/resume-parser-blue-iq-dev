"""
City resolver tests — the opt-in live enrichment stamps city_id + score-confidence,
de-dupes identical lookups, honours the accept floor, and stays a no-op when
disabled or when a role lacks the geography ids.
"""

from dataclasses import dataclass

from app.models.schemas import ExperienceItem, ParsedResumeAI
from app.services.normalization import city_api, city_resolver


@dataclass
class _FakeSettings:
    enable_city_api_match: bool = True
    gig_specialties_api_key: str = "k"
    gig_cities_api_url: str = "http://x/cities"
    city_api_max_lookups: int = 25


def _patch(monkeypatch, settings, search):
    monkeypatch.setattr(city_resolver, "get_settings", lambda: settings)

    async def _search(client, url, key, *, country_id, state_id, city_name, timeout=10.0):
        return search(country_id, state_id, city_name)

    monkeypatch.setattr(city_resolver.city_api, "search", _search)


def _exp(**kw):
    return ExperienceItem(company="X", role="RN", **kw)


def _city(cid, name, score):
    return city_api.CityMatch(id=cid, city=name, state_id="35", state="New York",
                              statecode="NY", country_id="1", score=score)


async def test_stamps_city_id_and_confidence(monkeypatch):
    settings = _FakeSettings()
    _patch(monkeypatch, settings,
           lambda c, s, name: [_city("19216", "New York", 1.0)])
    parsed = ParsedResumeAI(experience=[
        _exp(city="New York", country_id="1", state_id="35"),
    ])
    n = await city_resolver.resolve_cities(parsed)
    assert n == 1
    exp = parsed.experience[0]
    assert exp.city_id == "19216" and exp.city_confidence == 1.0


async def test_low_score_left_unmatched(monkeypatch):
    settings = _FakeSettings()
    _patch(monkeypatch, settings,
           lambda c, s, name: [_city("9", "New City", 0.3)])   # below CITY_ACCEPT_MIN
    parsed = ParsedResumeAI(experience=[
        _exp(city="New Yrok", country_id="1", state_id="35"),
    ])
    await city_resolver.resolve_cities(parsed)
    assert parsed.experience[0].city_id is None
    assert parsed.experience[0].city_confidence == 0.0


async def test_identical_lookups_deduped(monkeypatch):
    settings = _FakeSettings()
    calls = []

    def _search(c, s, name):
        calls.append(name)
        return [_city("19216", "New York", 1.0)]

    _patch(monkeypatch, settings, _search)
    parsed = ParsedResumeAI(experience=[
        _exp(city="New York", country_id="1", state_id="35"),
        _exp(city="new york", country_id="1", state_id="35"),   # same lookup key
    ])
    n = await city_resolver.resolve_cities(parsed)
    assert n == 1 and len(calls) == 1                            # one API call
    assert all(e.city_id == "19216" for e in parsed.experience)  # both stamped


async def test_disabled_is_noop(monkeypatch):
    settings = _FakeSettings(enable_city_api_match=False)
    _patch(monkeypatch, settings, lambda c, s, name: [_city("1", "X", 1.0)])
    parsed = ParsedResumeAI(experience=[_exp(city="New York", country_id="1", state_id="35")])
    assert await city_resolver.resolve_cities(parsed) == 0
    assert parsed.experience[0].city_id is None


async def test_missing_geography_ids_skipped(monkeypatch):
    settings = _FakeSettings()
    _patch(monkeypatch, settings, lambda c, s, name: [_city("1", "X", 1.0)])
    parsed = ParsedResumeAI(experience=[
        _exp(city="New York"),                       # no country_id/state_id
        _exp(city="Austin", country_id="1"),         # no state_id
    ])
    assert await city_resolver.resolve_cities(parsed) == 0
    assert all(e.city_id is None for e in parsed.experience)
