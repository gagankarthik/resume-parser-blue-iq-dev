"""DynamoDB serialization round trip for async job results.

boto3 rejects Python floats ("Float types are not supported. Use Decimal
types instead.") — the parsed-resume result carries float confidence scores,
so every async job result must convert floats to Decimals before update_item,
and back to plain int/float on read so schema validators don't null them.
(Regression: the FIRST async job that ever dispatched successfully failed on
exactly this write.)
"""

from decimal import Decimal

from app.db.dynamodb import _dynamo_safe, _plain
from app.models.schemas import ParsedResumeAI


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
