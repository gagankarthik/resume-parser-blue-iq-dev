"""
Facility matcher tests — exact name → id at conf 1.0, conservative fuzzy for a
near-miss spelling, and a graceful miss (id null, never guessed) when unmatched or
no catalog is loaded.
"""

import json

import pytest

from app.services.normalization import facility_catalog, facility_matcher


@pytest.fixture(autouse=True)
def _catalog(tmp_path):
    path = tmp_path / "fac.json"
    path.write_text(json.dumps({"facilities": [
        {"id": "3022", "name": "60th Medical Group - Travis AFB",
         "health_system": "Defense Health Agency", "health_system_id": "181"},
        {"id": "2974", "name": "Fort Sanders Regional Medical Center"},
    ]}), encoding="utf-8")
    facility_catalog.reload(str(path))
    yield
    facility_catalog.reload("")


def test_exact_name_matches_at_full_confidence():
    m = facility_matcher.match("Fort Sanders Regional Medical Center")
    assert m.matched
    assert m.facility_id == "2974"
    assert m.confidence == 1.0
    assert m.match_tier == "name"


def test_punctuation_and_case_insensitive():
    m = facility_matcher.match("fort sanders regional medical center")
    assert m.facility_id == "2974"
    assert m.confidence == 1.0


def test_parenthetical_and_dash_normalised_to_exact():
    m = facility_matcher.match("60th Medical Group – Travis AFB (Main Campus)")
    assert m.facility_id == "3022"
    assert m.confidence == 1.0
    assert m.health_system == "Defense Health Agency"


def test_near_miss_typo_fuzzy_matches_below_exact():
    m = facility_matcher.match("Fort Sanders Regionl Medical Center")  # dropped 'a'
    assert m.matched
    assert m.facility_id == "2974"
    assert m.match_tier == "fuzzy"
    assert 0.90 <= m.confidence <= facility_matcher.CONF_FUZZY_MAX


def test_unrelated_name_is_unmatched():
    m = facility_matcher.match("Acme Widgets LLC")
    assert not m.matched
    assert m.facility_id is None
    assert m.confidence == 0.0


@pytest.mark.parametrize("placeholder", ["", "Unknown", "N/A", "  "])
def test_placeholders_skip(placeholder):
    m = facility_matcher.match(placeholder)
    assert not m.matched
    assert m.facility_id is None


def test_no_catalog_is_graceful_miss():
    facility_catalog.reload("")
    m = facility_matcher.match("Fort Sanders Regional Medical Center")
    assert not m.matched
    assert m.facility_id is None
