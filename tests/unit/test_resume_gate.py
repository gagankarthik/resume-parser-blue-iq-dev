"""
Resume gate: an empty document or a document that is not a resume fails with the
single NOT_A_RESUME code, before any parse budget is spent. The classifier fails
open, so a model outage never rejects a real resume.
"""

import pytest

from app.core.errors import ErrorCode
from app.core.exceptions import AIParsingError, NotAResumeError
from app.services import pipeline
from app.services.parsing import resume_gate
from app.services.parsing.resume_gate import DocumentClassification

_RESUME_TEXT = (
    "Jane Smith, RN\njane@example.com\n(555) 234-5678\n"
    "EXPERIENCE\nMercy Hospital - Staff RN, ICU 01/2020 - Present\n"
    "EDUCATION\nState University - BSN 2019"
)


def _verdict(monkeypatch, verdict=None, exc=None):
    async def _run(self, text, meter):
        if exc:
            raise exc
        return verdict

    monkeypatch.setattr(resume_gate.ResumeGateAgent, "run", _run)


def test_error_code_is_registered_with_a_hint():
    from app.core.errors import get_hint

    assert NotAResumeError.error_code == ErrorCode.NOT_A_RESUME
    assert "not a resume" in get_hint(ErrorCode.NOT_A_RESUME)


@pytest.mark.parametrize("text", ["", "   \n\n  ", "| _ ~ 1 ] 23", "Page 1 of 1"])
def test_empty_text_is_empty(text):
    assert resume_gate.is_empty(text)


def test_resume_text_is_not_empty():
    assert not resume_gate.is_empty(_RESUME_TEXT)


@pytest.mark.asyncio
async def test_empty_document_rejected_without_model_call(monkeypatch):
    _verdict(monkeypatch, exc=AssertionError("classifier must not run on an empty document"))
    with pytest.raises(NotAResumeError, match="empty"):
        await resume_gate.check("  \n ")


@pytest.mark.asyncio
async def test_confident_non_resume_rejected(monkeypatch):
    _verdict(monkeypatch, DocumentClassification(
        is_resume=False, document_type="job description", confidence=0.95,
    ))
    with pytest.raises(NotAResumeError, match="job description"):
        await resume_gate.check("We are hiring an ICU Registered Nurse. Requirements: BSN, 2 years.")


@pytest.mark.asyncio
async def test_unsure_non_resume_is_let_through(monkeypatch):
    _verdict(monkeypatch, DocumentClassification(
        is_resume=False, document_type="cover letter", confidence=0.5,
    ))
    assert await resume_gate.check(_RESUME_TEXT) == 0


@pytest.mark.asyncio
async def test_resume_passes(monkeypatch):
    _verdict(monkeypatch, DocumentClassification(is_resume=True, document_type="resume", confidence=0.99))
    assert await resume_gate.check(_RESUME_TEXT) == 0


@pytest.mark.asyncio
async def test_classifier_failure_fails_open(monkeypatch):
    _verdict(monkeypatch, exc=AIParsingError("provider down"))
    assert await resume_gate.check(_RESUME_TEXT) == 0


@pytest.mark.asyncio
async def test_classifier_can_be_switched_off(monkeypatch):
    from app.core.config import get_settings

    _verdict(monkeypatch, exc=AssertionError("classifier is switched off"))
    monkeypatch.setattr(get_settings(), "enable_resume_classifier", False)
    assert await resume_gate.check(_RESUME_TEXT) == 0


@pytest.mark.asyncio
async def test_pipeline_stops_before_ai_parse_on_empty_document(monkeypatch):
    """An empty DOCX must fail with NOT_A_RESUME and never reach the AI parse."""
    async def _no_parse(*_a, **_k):
        raise AssertionError("AI parse must not run for an empty document")

    monkeypatch.setattr(
        pipeline.classifier, "classify",
        lambda filename, content: pipeline.ExtractionStrategy.DOCX,
    )
    monkeypatch.setattr(pipeline.docx_extractor, "extract", lambda content: "\n\n")
    monkeypatch.setattr(pipeline.orchestrator, "parse", _no_parse)
    monkeypatch.setattr(pipeline.ai_parser, "parse", _no_parse)

    with pytest.raises(NotAResumeError):
        await pipeline.run(
            pipeline.PipelineInput(job_id="e1", filename="blank.docx", content=b"x", company_id="c1")
        )


def test_confidence_clamp_does_not_read_a_bool_as_certainty():
    # bool is an int subclass: float(True) == 1.0 would turn a malformed `true`
    # into a maximally confident rejection. The gate fails open instead.
    assert DocumentClassification(is_resume=False, confidence=True).confidence == 0.0


def test_document_type_is_a_closed_set():
    # The label is derived from uploader-controlled content and echoed back in the
    # job error, the poll response and the parse.failed webhook - it must never be
    # free text. The JSON schema is an enum; anything unrecognised becomes "other".
    schema = DocumentClassification.model_json_schema()["properties"]["document_type"]
    assert "enum" in schema and "other" in schema["enum"]
    assert DocumentClassification(is_resume=False, document_type="job description").document_type == "job_description"
    spoof = DocumentClassification(is_resume=False, document_type="<img src=x onerror=alert(1)> Jane Smith")
    assert spoof.document_type == "other"


@pytest.mark.asyncio
async def test_rejection_message_is_fixed_text(monkeypatch):
    _verdict(monkeypatch, DocumentClassification.model_construct(
        is_resume=False, document_type="<b>Jane Smith</b>", confidence=0.95,
    ))
    with pytest.raises(NotAResumeError) as exc_info:
        await resume_gate.check(_RESUME_TEXT)
    assert str(exc_info.value) == "The document is not a resume (it looks like another kind of document)."


@pytest.mark.asyncio
async def test_rejection_names_the_document_kind(monkeypatch):
    _verdict(monkeypatch, DocumentClassification(
        is_resume=False, document_type="invoice_or_receipt", confidence=0.9,
    ))
    with pytest.raises(NotAResumeError) as exc_info:
        await resume_gate.check(_RESUME_TEXT)
    assert str(exc_info.value) == "The document is not a resume (it looks like an invoice or receipt)."


@pytest.mark.asyncio
async def test_rejection_carries_the_tokens_it_spent(monkeypatch):
    async def _run(self, text, meter):
        meter.add(self.name, 37)
        return DocumentClassification(is_resume=False, document_type="job_description", confidence=0.95)

    monkeypatch.setattr(resume_gate.ResumeGateAgent, "run", _run)
    with pytest.raises(NotAResumeError) as exc_info:
        await resume_gate.check(_RESUME_TEXT)
    assert exc_info.value.tokens == 37

    with pytest.raises(NotAResumeError) as empty_info:
        await resume_gate.check("")
    assert empty_info.value.tokens == 0


@pytest.mark.asyncio
async def test_gate_log_carries_no_model_text(monkeypatch):
    import structlog

    _verdict(monkeypatch, DocumentClassification(is_resume=True, document_type="resume", confidence=0.99))
    with structlog.testing.capture_logs() as logs:
        await resume_gate.check(_RESUME_TEXT, job_id="j1")
    gate = [e for e in logs if e["event"] == "resume_gate"]
    assert gate and "document_type" not in gate[0]
    assert gate[0]["is_resume"] is True and gate[0]["confidence"] == 0.99


@pytest.mark.asyncio
async def test_gate_window_comes_from_the_parse_budget(monkeypatch):
    """The gate runs before the parse ladder, so its deadline is the budget's, not a
    private constant: with no window left it skips the model call (failing open)."""
    import time

    from app.services.budget import ParseBudget

    calls: list[str] = []

    async def _run(self, text, meter):
        calls.append(text)
        return DocumentClassification(is_resume=False, document_type="invoice_or_receipt", confidence=0.99)

    monkeypatch.setattr(resume_gate.ResumeGateAgent, "run", _run)
    spent = ParseBudget(total=200, started=time.monotonic() - 199)
    assert spent.for_resume_gate() <= 0
    assert await resume_gate.check(_RESUME_TEXT, budget=spent) == 0
    assert calls == []
