"""
City resolver tests - the opt-in live enrichment stamps city_id + score-confidence,
de-dupes identical lookups, honours the accept floor, and stays a no-op when
disabled or when a role lacks the geography ids.
"""

from dataclasses import dataclass

import structlog.testing

from app.models.schemas import ExperienceItem, ParsedResumeAI
from app.services.normalization import city_api, city_resolver, gig_api


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


# -- Regressions from the live-API probe ---------------------------------------
#
# Real scores measured against api.gighealth.com for the resume that surfaced this:
#
#   "Buffalo"       -> Buffalo        1.0    exact, right city
#   "Williamsville" -> Williamsville  1.0    exact, right city
#   "Williamsville" -> Willisville    0.6    WRONG city
#   "Williamsville" -> Williamson     0.5    WRONG city
#   "New Yrok"      -> New York       0.385  typo of the RIGHT city
#
# The wrong answers score HIGHER than a typo of the right one, so the fuzzy band is
# not safely usable. The old floor (0.5) accepted `Williamson` outright.


async def test_wrong_city_at_point_six_is_rejected(monkeypatch):
    """THE REGRESSION. `Willisville` scores 0.6 as a match for `Williamsville` - a
    real, wrong city. The old 0.5 floor stamped it. A wrong city_id silently corrupts
    placement data; a null one gets reviewed."""
    settings = _FakeSettings()
    _patch(monkeypatch, settings,
           lambda c, s, name: [_city("77065", "Willisville", 0.6)])
    parsed = ParsedResumeAI(experience=[
        _exp(city="Williamsville", country_id="1", state_id="35"),
    ])
    await city_resolver.resolve_cities(parsed)
    exp = parsed.experience[0]
    assert exp.city_id is None, "a 0.6 fuzzy match is a different city, not a typo"
    assert exp.city_confidence == 0


async def test_exact_match_still_accepted(monkeypatch):
    """The floor must not be so high that real cities stop resolving - every city on
    the failing resume scored exactly 1.0 against the live API."""
    settings = _FakeSettings()
    for city, cid in [("Buffalo", "3512"), ("Williamsville", "77062"), ("Newport News", "19331")]:
        city_resolver._reset_cache()
        _patch(monkeypatch, settings, lambda c, s, name, _cid=cid, _c=city: [_city(_cid, _c, 1.0)])
        parsed = ParsedResumeAI(experience=[_exp(city=city, country_id="1", state_id="35")])
        await city_resolver.resolve_cities(parsed)
        assert parsed.experience[0].city_id == cid, f"{city} must still resolve"


async def test_missing_api_key_is_a_loud_noop(monkeypatch):
    """ROOT CAUSE of the null city_ids in production. An unkeyed Lambda used to return
    silently, so `city_id: null` on every role looked like a parser bug rather than a
    missing env var. It must still be a no-op - but it must SAY so."""
    settings = _FakeSettings(gig_specialties_api_key="")
    monkeypatch.setattr(city_resolver, "get_settings", lambda: settings)
    parsed = ParsedResumeAI(experience=[
        _exp(city="Buffalo", country_id="1", state_id="35"),
    ])
    with structlog.testing.capture_logs() as logs:
        n = await city_resolver.resolve_cities(parsed)
    assert n == 0
    assert parsed.experience[0].city_id is None
    events = [e["event"] for e in logs]
    assert "city_api_no_key" in events, f"a missing key must be visible in the logs; got {events}"


async def test_api_failure_is_logged_and_never_fails_the_parse(monkeypatch):
    """A 403 (key lacks the `cities` permission) must not be indistinguishable from
    'no city matched' - it used to be swallowed into an empty list with no log."""
    settings = _FakeSettings()
    monkeypatch.setattr(city_resolver, "get_settings", lambda: settings)

    async def _boom(client, url, key, *, country_id, state_id, city_name, timeout=10.0):
        raise gig_api.GigApiError("forbidden", 403, "not authorized to access this resource")

    monkeypatch.setattr(city_resolver.city_api, "search", _boom)
    parsed = ParsedResumeAI(experience=[
        _exp(city="Buffalo", country_id="1", state_id="35"),
    ])
    with structlog.testing.capture_logs() as logs:
        await city_resolver.resolve_cities(parsed)   # must not raise
    assert parsed.experience[0].city_id is None
    failed = [e for e in logs if e["event"] == "city_api_lookup_failed"]
    assert failed, "a 403 must not look like 'no city matched'"
    assert failed[0]["kind"] == "forbidden" and failed[0]["status"] == 403


async def test_cache_spares_quota_across_resumes(monkeypatch):
    """The partner guide asks integrators to cache rather than call per transaction -
    every call counts against the monthly quota. A warm worker must not re-query the
    same city for each resume."""
    settings = _FakeSettings()
    calls: list[str] = []

    def _search(c, s, name):
        calls.append(name)
        return [_city("3512", "Buffalo", 1.0)]

    _patch(monkeypatch, settings, _search)

    for _ in range(3):  # three separate résumés, same city
        parsed = ParsedResumeAI(experience=[
            _exp(city="Buffalo", country_id="1", state_id="35"),
        ])
        await city_resolver.resolve_cities(parsed)
        assert parsed.experience[0].city_id == "3512"   # resolved every time

    assert len(calls) == 1, f"city was looked up {len(calls)}x; the cache should spend quota once"
