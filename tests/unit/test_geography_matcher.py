"""
Geography matcher tests - offline country/state -> platform id at conf 1.0,
country-scoped state resolution, alias handling, and graceful misses.
"""

import json

import pytest

from app.services.normalization import geography_catalog, geography_matcher


@pytest.fixture(autouse=True)
def _catalog(tmp_path):
    p = tmp_path / "geo.json"
    p.write_text(json.dumps({"geographies": [
        {"id": "1", "country": "United States", "code": "US", "states": [
            {"id": "35", "state": "New York", "statecode": "NY"},
            {"id": "44", "state": "Texas", "statecode": "TX"},
        ]},
        {"id": "2", "country": "Canada", "code": "CA", "states": [
            {"id": "57", "state": "Alberta", "statecode": "AB"},
        ]},
    ]}), encoding="utf-8")
    geography_catalog.reload(str(p))
    yield
    geography_catalog.reload("")


@pytest.mark.parametrize("value", ["United States", "united states", "US", "USA", "America"])
def test_country_resolves_by_name_code_and_alias(value):
    m = geography_matcher.resolve_country(value)
    assert m.matched and m.id == "1" and m.code == "US" and m.confidence == 1.0


def test_country_unmatched():
    m = geography_matcher.resolve_country("Narnia")
    assert not m.matched and m.id is None and m.confidence == 0.0


@pytest.mark.parametrize("blank", [None, "", "Unknown", "N/A"])
def test_country_blank(blank):
    assert not geography_matcher.resolve_country(blank).matched


@pytest.mark.parametrize("value", ["NY", "ny", "New York", "new york"])
def test_state_resolves_by_code_and_name(value):
    m = geography_matcher.resolve_state(value, "1")
    assert m.matched and m.id == "35" and m.country_id == "1" and m.confidence == 1.0


def test_state_scoped_to_known_country_only():
    # Alberta is Canadian; under the US country it must NOT resolve (no cross-country
    # flat fallback when the country is known).
    assert not geography_matcher.resolve_state("Alberta", "1").matched
    assert geography_matcher.resolve_state("Alberta", "2").id == "57"


def test_state_flat_fallback_when_country_unknown():
    m = geography_matcher.resolve_state("Alberta", None)
    assert m.matched and m.id == "57" and m.country_id == "2"


def test_state_unmatched():
    assert not geography_matcher.resolve_state("ZZ", "1").matched


def test_no_catalog_is_graceful_miss():
    geography_catalog.reload("")
    assert not geography_matcher.resolve_country("United States").matched
    assert not geography_matcher.resolve_state("NY", "1").matched
