"""Unit tests for the feedback field-diff helper."""

from app.services.feedback import diff_fields


def test_identical_objects_have_no_changes():
    assert diff_fields({"a": 1, "b": "x"}, {"a": 1, "b": "x"}) == []


def test_scalar_value_change():
    assert diff_fields({"a": 1}, {"a": 2}) == ["a"]


def test_nested_object_change_uses_dotted_path():
    original = {"personal_info": {"full_name": "Jane Smith"}}
    updated = {"personal_info": {"full_name": "Jane A. Smith, RN"}}
    assert diff_fields(original, updated) == ["personal_info.full_name"]


def test_list_item_added_uses_index_path():
    assert diff_fields({"skills": ["ICU"]}, {"skills": ["ICU", "CCRN"]}) == ["skills[1]"]


def test_list_item_changed():
    assert diff_fields({"skills": ["ICU"]}, {"skills": ["ER"]}) == ["skills[0]"]


def test_added_and_removed_keys_are_changes():
    assert diff_fields({"a": 1}, {"a": 1, "b": 2}) == ["b"]
    assert diff_fields({"a": 1, "b": 2}, {"a": 1}) == ["b"]


def test_multiple_changes_are_sorted_and_complete():
    original = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2]}
    updated = {"a": 9, "nested": {"x": 1, "y": 5}, "list": [1, 2, 3]}
    assert diff_fields(original, updated) == ["a", "list[2]", "nested.y"]


def test_empty_payloads():
    assert diff_fields({}, {}) == []


def test_deeply_nested_within_list():
    original = {"experience": [{"title": "RN"}]}
    updated = {"experience": [{"title": "Charge RN"}]}
    assert diff_fields(original, updated) == ["experience[0].title"]
