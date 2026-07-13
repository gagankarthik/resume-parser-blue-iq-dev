"""
GigHealth specialties API transform tests - flattening the nested envelope into
flat catalog rows with profession + curated keywords, preserving exact names.
"""

from app.services.normalization import specialty_api

_PAYLOAD = {
    "success": True,
    "data": [{
        "id": 1, "name": "Nursing",
        "professions": [{
            "id": 1, "name": "RN",
            "specialityGroups": [{
                "id": 12, "name": "ICU",
                "specialities": [
                    {"id": 56, "name": "ICU", "fullName": "Intensive Care Unit"},
                    {"id": 9, "name": "BICU", "fullName": "Burn Intensive Care Unit"},
                ],
            }],
            "ungroupedSpecialities": [
                {"id": 4, "name": "Anesthetist", "fullName": None},
            ],
        }, {
            "id": 18, "name": "CNA",
            "specialityGroups": [{
                "id": 12, "name": "ICU",
                "specialities": [
                    {"id": 757, "name": "ICU", "fullName": "Intensive Care Unit"},
                ],
            }],
            "ungroupedSpecialities": [],
        }],
    }],
}


def test_flatten_shapes_rows_and_preserves_names():
    rows = specialty_api.flatten_payload(_PAYLOAD)
    icu = [r for r in rows if r["specialty"] == "ICU"]
    # Same name under two professions with distinct ids.
    assert {(r["profession"], r["id"]) for r in icu} == {("RN", "56"), ("CNA", "757")}
    rn_icu = next(r for r in icu if r["profession"] == "RN")
    assert rn_icu["full_name"] == "Intensive Care Unit"
    assert rn_icu["group"] == "ICU"


def test_ungrouped_specialty_has_no_group():
    rows = specialty_api.flatten_payload(_PAYLOAD)
    anes = next(r for r in rows if r["specialty"] == "Anesthetist")
    assert anes["group"] is None
    assert anes["profession"] == "RN"
    assert anes["full_name"] is None


def test_curated_keywords_attached_by_name():
    rows = specialty_api.flatten_payload(_PAYLOAD)
    bicu = next(r for r in rows if r["specialty"] == "BICU")
    assert "burn icu" in bicu["keywords"]          # sub-type differentiation
    icu = next(r for r in rows if r["specialty"] == "ICU")
    assert icu["keywords"] == []                    # no curated overrides for plain ICU


def test_bad_payload_is_empty():
    assert specialty_api.flatten_payload({}) == []
    assert specialty_api.flatten_payload({"data": None}) == []


def test_rows_missing_id_or_name_dropped():
    payload = {"data": [{"name": "Nursing", "professions": [{
        "name": "RN",
        "specialityGroups": [{"name": "G", "specialities": [
            {"id": 1, "name": "Good"},
            {"name": "No Id"},
            {"id": 2, "name": "  "},
        ]}],
        "ungroupedSpecialities": [],
    }]}]}
    rows = specialty_api.flatten_payload(payload)
    assert [r["specialty"] for r in rows] == ["Good"]
