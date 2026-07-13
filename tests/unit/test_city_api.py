"""
Cities API parse tests - turning the fuzzy-search envelope into ordered CityMatch
records with a coerced 0-1 score, preserving best-first order.
"""

from app.services.normalization import city_api

_PAYLOAD = {
    "success": True,
    "data": [
        {"id": 19216, "city": "New York", "stateId": 35, "state": "New York",
         "statecode": "NY", "countryId": 1, "score": 1},
        {"id": 19004, "city": "New City", "stateId": 35, "state": "New York",
         "statecode": "NY", "countryId": 1, "score": 0.2857143},
    ],
    "errors": [],
}


def test_parse_matches_preserves_order_and_fields():
    ms = city_api.parse_matches(_PAYLOAD)
    assert [m.id for m in ms] == ["19216", "19004"]         # best-first preserved
    top = ms[0]
    assert top.city == "New York"
    assert top.state_id == "35" and top.statecode == "NY" and top.country_id == "1"
    assert top.score == 1.0                                  # int 1 coerced to float


def test_empty_and_bad_payloads():
    assert city_api.parse_matches({"data": []}) == []
    assert city_api.parse_matches({}) == []
    assert city_api.parse_matches({"data": None}) == []


def test_rows_missing_id_or_city_dropped():
    payload = {"data": [
        {"id": 1, "city": "Keep", "score": 0.9},
        {"city": "No Id", "score": 0.9},
        {"id": 2, "city": "  ", "score": 0.9},
    ]}
    ms = city_api.parse_matches(payload)
    assert [m.city for m in ms] == ["Keep"]


def test_score_out_of_range_clamped():
    ms = city_api.parse_matches({"data": [{"id": 1, "city": "X", "score": 1.7}]})
    assert ms[0].score == 1.0
    ms = city_api.parse_matches({"data": [{"id": 1, "city": "X", "score": "oops"}]})
    assert ms[0].score == 0.0
