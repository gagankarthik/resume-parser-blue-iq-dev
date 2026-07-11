"""
GigHealth facilities API transform tests — flattening the flat envelope into
catalog rows, preserving exact names and coercing ids/nullable health systems.
"""

from app.services.normalization import facility_api

_PAYLOAD = {
    "success": True,
    "data": [
        {"id": 3022, "name": "60th Medical Group - Travis AFB",
         "healthSystemId": 181, "healthSystemName": "Defense Health Agency"},
        {"id": 2974, "name": "Fort Sanders Regional Medical Center",
         "healthSystemId": None, "healthSystemName": None},
    ],
    "message": "ok",
    "errors": [],
}


def test_flatten_shapes_rows_and_preserves_names():
    rows = facility_api.flatten_payload(_PAYLOAD)
    travis = next(r for r in rows if r["id"] == "3022")
    assert travis["name"] == "60th Medical Group - Travis AFB"   # exact name
    assert travis["health_system"] == "Defense Health Agency"
    assert travis["health_system_id"] == "181"                    # coerced to str


def test_nullable_health_system_becomes_none():
    rows = facility_api.flatten_payload(_PAYLOAD)
    fort = next(r for r in rows if r["id"] == "2974")
    assert fort["health_system"] is None
    assert fort["health_system_id"] is None


def test_bad_payload_is_empty():
    assert facility_api.flatten_payload({}) == []
    assert facility_api.flatten_payload({"data": None}) == []


def test_rows_missing_id_or_name_dropped():
    payload = {"data": [
        {"id": 1, "name": "Good Hospital"},
        {"name": "No Id"},
        {"id": 2, "name": "  "},
    ]}
    rows = facility_api.flatten_payload(payload)
    assert [r["name"] for r in rows] == ["Good Hospital"]
