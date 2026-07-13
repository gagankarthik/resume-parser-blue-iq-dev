"""
Facility catalog loader tests - JSON/CSV parsing, name index, and the
missing/empty-file fallback (a bad or absent catalog must never break parsing).
"""

import json

import pytest

from app.services.normalization import facility_catalog
from app.services.normalization.healthcare_taxonomy import _match_key


@pytest.fixture(autouse=True)
def _reset_catalog():
    """Each test reloads explicitly; reset to an empty catalog afterwards so the
    bundled default snapshot never leaks between tests."""
    yield
    facility_catalog.reload("")


def _write(tmp_path, name, payload):
    p = tmp_path / name
    if name.endswith(".json"):
        p.write_text(json.dumps(payload), encoding="utf-8")
    else:
        p.write_text(payload, encoding="utf-8")
    return str(p)


def test_unset_path_is_empty():
    cat = facility_catalog.reload("")
    assert cat.is_empty
    assert cat.records == []


def test_missing_file_is_empty(tmp_path):
    cat = facility_catalog.reload(str(tmp_path / "nope.json"))
    assert cat.is_empty


def test_garbled_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json ", encoding="utf-8")
    assert facility_catalog.reload(str(p)).is_empty


def test_default_path_loads_bundled_snapshot():
    cat = facility_catalog.reload(None)
    assert not cat.is_empty
    rec = cat.by_name_key[_match_key("60th Medical Group - Travis AFB")]
    assert rec.id == "3022"
    assert rec.health_system == "Defense Health Agency"


def test_loads_json_envelope_and_builds_index(tmp_path):
    path = _write(tmp_path, "cat.json", {"facilities": [
        {"id": "3022", "name": "60th Medical Group - Travis AFB",
         "health_system": "Defense Health Agency", "health_system_id": "181"},
        {"id": "2974", "name": "Fort Sanders Regional Medical Center"},
    ]})
    cat = facility_catalog.reload(path)
    assert len(cat.records) == 2
    rec = cat.by_name_key[_match_key("fort sanders regional medical center")]
    assert rec.id == "2974"
    assert rec.health_system is None


def test_loads_csv(tmp_path):
    path = _write(
        tmp_path, "cat.csv",
        "id,name,health_system_id\n7,Mercy Hospital,55\n",
    )
    cat = facility_catalog.reload(path)
    rec = cat.by_name_key[_match_key("Mercy Hospital")]
    assert rec.id == "7"
    assert rec.health_system_id == "55"


def test_duplicate_name_is_first_wins(tmp_path):
    path = _write(tmp_path, "cat.json", [
        {"id": "1", "name": "Regional Medical Center"},
        {"id": "2", "name": "Regional Medical Center"},
    ])
    cat = facility_catalog.reload(path)
    assert cat.by_name_key[_match_key("Regional Medical Center")].id == "1"
