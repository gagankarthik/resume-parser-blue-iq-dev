"""
GigHealth geographies API transform tests - normalising the nested country/state
envelope into clean rows, coercing ids to strings, dropping malformed entries.
"""

from app.services.normalization import geography_api

_PAYLOAD = {
    "success": True,
    "data": [
        {"id": 1, "country": "United States", "code": "US", "states": [
            {"id": 2, "state": "Alabama", "statecode": "AL"},
            {"id": 35, "state": "New York", "statecode": "NY"},
        ]},
        {"id": 2, "country": "Canada", "code": "CA", "states": [
            {"id": 57, "state": "Alberta", "statecode": "AB"},
        ]},
    ],
    "errors": [],
}


def test_flatten_shapes_countries_and_states():
    rows = geography_api.flatten_payload(_PAYLOAD)
    us = next(r for r in rows if r["id"] == "1")
    assert us["country"] == "United States"
    assert us["code"] == "US"
    ny = next(s for s in us["states"] if s["id"] == "35")
    assert ny["state"] == "New York" and ny["statecode"] == "NY"


def test_ids_coerced_to_strings():
    rows = geography_api.flatten_payload(_PAYLOAD)
    assert all(isinstance(r["id"], str) for r in rows)
    assert all(isinstance(s["id"], str) for r in rows for s in r["states"])


def test_bad_payload_is_empty():
    assert geography_api.flatten_payload({}) == []
    assert geography_api.flatten_payload({"data": None}) == []


def test_country_or_state_missing_id_or_name_dropped():
    payload = {"data": [
        {"id": 1, "country": "Good", "states": [
            {"id": 9, "state": "Keep"},
            {"state": "No Id"},
            {"id": 10, "state": "  "},
        ]},
        {"country": "No Id Country", "states": []},
    ]}
    rows = geography_api.flatten_payload(payload)
    assert [r["country"] for r in rows] == ["Good"]
    assert [s["state"] for s in rows[0]["states"]] == ["Keep"]
