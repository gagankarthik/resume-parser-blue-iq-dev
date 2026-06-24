"""DynamoDB serialization round trip for async job results.

boto3 rejects Python floats ("Float types are not supported. Use Decimal
types instead.") — the parsed-resume result carries float confidence scores,
so every async job result must convert floats to Decimals before update_item,
and back to plain int/float on read so schema validators don't null them.
(Regression: the FIRST async job that ever dispatched successfully failed on
exactly this write.)
"""

from decimal import Decimal

from app.db import dynamodb as db
from app.db.dynamodb import _dynamo_safe, _plain
from app.models.schemas import ParsedResumeAI


class _CapturingTable:
    def __init__(self):
        self.item = None

    def put_item(self, Item):  # noqa: N803 — boto3 kwarg name
        self.item = Item


def _patch_table(monkeypatch) -> _CapturingTable:
    table = _CapturingTable()
    monkeypatch.setattr(db, "_get_dynamodb", lambda settings: type("R", (), {"Table": lambda self, name: table})())
    return table


def test_write_audit_log_stores_key_attribution(monkeypatch):
    table = _patch_table(monkeypatch)
    db.write_audit_log(
        job_id="j1", company_id="acme", file_type="pdf", file_size_bytes=10,
        status="completed", duration_ms=5, key_hash="abc", key_prefix="rp_live_ab",
    )
    assert table.item["key_hash"] == "abc"
    assert table.item["key_prefix"] == "rp_live_ab"


def test_write_audit_log_omits_empty_key_fields(monkeypatch):
    """No authenticated key (or legacy callers) → the key fields are left off
    the record entirely rather than written as empty strings."""
    table = _patch_table(monkeypatch)
    db.write_audit_log(
        job_id="j2", company_id="acme", file_type="pdf", file_size_bytes=10,
        status="completed", duration_ms=5,
    )
    assert "key_hash" not in table.item
    assert "key_prefix" not in table.item


def test_dynamo_safe_converts_floats_to_decimals():
    out = _dynamo_safe({"confidence": {"overall": 0.83}, "partial": False})
    assert isinstance(out["confidence"]["overall"], Decimal)
    assert out["partial"] is False


def test_plain_restores_ints_and_floats():
    stored = {"result": {"confidence": {"overall": Decimal("0.83")},
                         "data": {"education": [{"institution": "X",
                                                 "graduation_year": Decimal("2016")}]}}}
    out = _plain(stored)
    assert out["result"]["confidence"]["overall"] == 0.83
    assert isinstance(out["result"]["data"]["education"][0]["graduation_year"], int)


def test_round_trip_preserves_education_year_through_schema():
    parsed = ParsedResumeAI.model_validate(
        {"education": [{"institution": "X", "graduation_year": 2016}]}
    )
    stored = _dynamo_safe(parsed.model_dump())
    back = ParsedResumeAI.model_validate(_plain(stored))
    assert back.education[0].graduation_year == 2016
