"""
Specialty catalog loader tests — JSON/CSV parsing, index building, and the
missing/empty-file fallback (a bad or absent catalog must never break parsing).
"""

import json

import pytest

from app.services.normalization import specialty_catalog
from app.services.normalization.healthcare_taxonomy import _match_key


@pytest.fixture(autouse=True)
def _reset_catalog():
    """Each test reloads explicitly; reset to an empty catalog afterwards so the
    bundled default snapshot never leaks between tests."""
    yield
    specialty_catalog.reload("")


def test_unset_path_is_empty():
    cat = specialty_catalog.reload("")
    assert cat.is_empty
    assert cat.records == []


def _write(tmp_path, name, payload):
    p = tmp_path / name
    if name.endswith(".json"):
        p.write_text(json.dumps(payload), encoding="utf-8")
    else:
        p.write_text(payload, encoding="utf-8")
    return str(p)


def test_default_path_loads_bundled_snapshot():
    # reload(None) uses the configured default — the bundled Gig snapshot.
    cat = specialty_catalog.reload(None)
    assert not cat.is_empty
    # Same specialty name carries a different id per profession (RN vs CNA ICU).
    rn_icu = cat.by_prof_name_key[("rn", _match_key("ICU"))]
    cna_icu = cat.by_prof_name_key[("cna", _match_key("ICU"))]
    assert rn_icu.id != cna_icu.id


def test_missing_file_is_empty(tmp_path):
    cat = specialty_catalog.reload(str(tmp_path / "does_not_exist.json"))
    assert cat.is_empty


def test_garbled_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json ", encoding="utf-8")
    cat = specialty_catalog.reload(str(p))
    assert cat.is_empty


def test_loads_json_list_and_builds_indexes(tmp_path):
    path = _write(tmp_path, "cat.json", [
        {"id": "1042", "specialty": "Medical Surgical",
         "full_name": "Medical Surgical / Telemetry",
         "keywords": ["floor nursing", "ms/tele"], "group": "Med Surg / Tele"},
    ])
    cat = specialty_catalog.reload(path)
    assert len(cat.records) == 1
    rec = cat.records[0]
    assert rec.id == "1042"
    assert rec.keywords == ("floor nursing", "ms/tele")
    assert cat.by_name_key[_match_key("Medical Surgical")] is rec
    assert cat.by_full_key[_match_key("Medical Surgical / Telemetry")] is rec
    assert cat.by_keyword_key[_match_key("ms/tele")] is rec


def test_int_id_and_envelope_and_missing_fields(tmp_path):
    # {"specialties": [...]} envelope, integer id, rows missing id/name dropped.
    path = _write(tmp_path, "cat.json", {"specialties": [
        {"id": 2001, "specialty": "Intensive Care Unit"},
        {"specialty": "No Id Here"},          # dropped: no id
        {"id": 9, "name": "   "},             # dropped: blank name
    ]})
    cat = specialty_catalog.reload(path)
    assert [r.id for r in cat.records] == ["2001"]
    assert cat.records[0].full_name is None


def test_loads_csv(tmp_path):
    path = _write(
        tmp_path, "cat.csv",
        "id,specialty,full_name,keywords,group\n"
        "1042,Medical Surgical,Medical Surgical / Telemetry,floor nursing|ms tele,Med Surg / Tele\n",
    )
    cat = specialty_catalog.reload(path)
    assert len(cat.records) == 1
    assert cat.records[0].keywords == ("floor nursing", "ms tele")
    assert cat.by_keyword_key[_match_key("ms tele")].id == "1042"
