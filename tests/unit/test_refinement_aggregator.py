"""Aggregate feedback records into per-agent correction examples."""

from app.services.refinement.aggregator import aggregate
from app.services.refinement.field_map import CREDENTIALS, PERSONAL, WORK


def _fb(changed_fields, original, updated, changed=True):
    return {
        "changed": changed,
        "changed_fields": changed_fields,
        "original": original,
        "updated": updated,
    }


def test_groups_by_agent_and_counts():
    records = [
        _fb(
            ["personal_info.full_name", "skills[0]"],
            {"personal_info": {"full_name": "Jane Smith RN"}, "skills": ["icu"]},
            {"personal_info": {"full_name": "Jane Smith"}, "skills": ["ICU"]},
        ),
    ]
    out = aggregate(records)
    assert set(out) == {PERSONAL, CREDENTIALS}
    assert out[PERSONAL].total == 1
    assert out[CREDENTIALS].total == 1


def test_list_indices_are_normalised_so_recurring_mistakes_accumulate():
    records = [
        _fb(
            ["experience[0].company", "experience[1].company"],
            {"experience": [{"company": "Agency A"}, {"company": "Agency B"}]},
            {"experience": [{"company": "Hospital A"}, {"company": "Hospital B"}]},
        ),
    ]
    out = aggregate(records)
    work = out[WORK]
    # Both concrete indices collapse to one normalised path.
    assert work.field_counts["experience[].company"] == 2
    assert work.top_fields(1)[0] == ("experience[].company", 2)


def test_examples_capture_before_and_after():
    records = [
        _fb(
            ["personal_info.full_name"],
            {"personal_info": {"full_name": "Jane Smith, RN, BSN"}},
            {"personal_info": {"full_name": "Jane Smith"}},
        ),
    ]
    ex = aggregate(records)[PERSONAL].examples
    assert len(ex) == 1
    assert ex[0].before == "Jane Smith, RN, BSN"
    assert ex[0].after == "Jane Smith"
    assert ex[0].field == "personal_info.full_name"


def test_missing_before_value_resolves_to_none():
    # A field the parser omitted entirely (reviewer added it).
    records = [
        _fb(
            ["personal_info.phone"],
            {"personal_info": {}},
            {"personal_info": {"phone": "555-1212"}},
        ),
    ]
    ex = aggregate(records)[PERSONAL].examples
    assert ex[0].before is None
    assert ex[0].after == "555-1212"


def test_unchanged_records_are_ignored():
    records = [_fb(["skills[0]"], {"skills": ["ICU"]}, {"skills": ["ICU"]}, changed=False)]
    assert aggregate(records) == {}


def test_long_values_are_truncated():
    long = "x" * 500
    records = [
        _fb(
            ["personal_info.summary"],
            {"personal_info": {"summary": long}},
            {"personal_info": {"summary": "short"}},
        ),
    ]
    ex = aggregate(records)[PERSONAL].examples[0]
    assert isinstance(ex.before, str) and len(ex.before) <= 210 and ex.before.endswith("…")
