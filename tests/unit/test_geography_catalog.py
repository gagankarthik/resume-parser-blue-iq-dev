"""
Geography catalog loader tests — snapshot parsing, country/state indexes (flat +
country-scoped), and the missing/garbled-file fallback (never break parsing).
"""

import json

import pytest

from app.services.normalization import geography_catalog
from app.services.normalization.healthcare_taxonomy import _match_key


@pytest.fixture(autouse=True)
def _reset_catalog():
    yield
    geography_catalog.reload("")


def _write(tmp_path, payload):
    p = tmp_path / "geo.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


_SNAPSHOT = {"geographies": [
    {"id": "1", "country": "United States", "code": "US", "states": [
        {"id": "2", "state": "Alabama", "statecode": "AL"},
        {"id": "35", "state": "New York", "statecode": "NY"},
    ]},
    {"id": "2", "country": "Canada", "code": "CA", "states": [
        {"id": "57", "state": "Alberta", "statecode": "AB"},
    ]},
]}


def test_unset_path_is_empty():
    cat = geography_catalog.reload("")
    assert cat.is_empty


def test_missing_file_is_empty(tmp_path):
    assert geography_catalog.reload(str(tmp_path / "nope.json")).is_empty


def test_garbled_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid ", encoding="utf-8")
    assert geography_catalog.reload(str(p)).is_empty


def test_default_path_loads_bundled_snapshot():
    cat = geography_catalog.reload(None)
    assert not cat.is_empty
    assert cat.country_by_code["US"].id == "1"


def test_builds_country_and_state_indexes(tmp_path):
    cat = geography_catalog.reload(_write(tmp_path, _SNAPSHOT))
    assert len(cat.countries) == 2
    assert len(cat.states) == 3
    assert cat.country_by_name[_match_key("United States")].id == "1"
    assert cat.country_by_code["CA"].id == "2"
    # State scoped by country carries the parent country id.
    ny = cat.state_by_country_code[("1", "NY")]
    assert ny.id == "35" and ny.country_id == "1"
    ab = cat.state_by_country_name[("2", _match_key("Alberta"))]
    assert ab.id == "57" and ab.statecode == "AB"
