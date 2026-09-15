"""
Resume gate - reject a document that is empty or is not a resume.

Runs on the worker right after text extraction and before the AI parse, so a blank
page, an image with no text, or the wrong document (a job description, cover letter,
invoice, contract...) fails fast with ONE error code, NOT_A_RESUME, instead of
spending the whole parse budget and returning a near-empty record that reads as a
successful parse.

Two checks, cheapest first:
  1. Empty - fewer than `_MIN_WORDS` real words survive extraction and cleaning.
     Deterministic, no model call, always on.
  2. Not a resume - one small structured LLM call on the head of the document.
     It rejects only when the model is confident. Any model failure or timeout lets
     the document through (fail open): wrongly rejecting a real resume is worse than
     parsing an odd document. Switched off with ENABLE_RESUME_CLASSIFIER=false.

The rejection message goes back to the uploader in the job error, the poll response
and the parse.failed webhook, so it is always fixed text: the model picks a label from
a closed set and never writes any part of the message.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.exceptions import NotAResumeError
from app.core.logging import get_logger
from app.services.budget import ParseBudget
from app.services.parsing.agents.base import BaseAgent, TokenMeter

log = get_logger(__name__)

# A "word" is a run of 2+ letters in any script, so OCR noise ("| _ ~ 1 ]") and bare
# page furniture don't count. Below this many, there is nothing to parse.
_MIN_WORDS = 5
_WORD_RE = re.compile(r"[^\W\d_]{2,}")

# The head of a document is enough to tell what kind of document it is, and keeps
# the check cheap on a long CV.
_CLASSIFY_CHARS = 6000
# Reject only on a confident "not a resume"; anything less is parsed as usual.
_REJECT_CONFIDENCE = 0.8

DocumentType = Literal[
    "resume", "job_description", "cover_letter", "reference_letter", "offer_or_contract",
    "invoice_or_receipt", "timesheet", "skills_checklist", "license_or_certificate",
    "id_document", "blank_template", "article_or_policy", "form", "other",
]

# The wording a rejection uses for each label. The label is chosen by a model reading
# uploader-controlled content, so only this fixed text - never model output - reaches
# a client. "resume" and "other" (and anything unexpected) use the generic phrase.
_DOCUMENT_KIND: dict[str, str] = {
    "job_description":        "a job description",
    "cover_letter":           "a cover letter",
    "reference_letter":       "a reference letter",
    "offer_or_contract":      "an offer letter or contract",
    "invoice_or_receipt":     "an invoice or receipt",
    "timesheet":              "a timesheet",
    "skills_checklist":       "a skills checklist",
    "license_or_certificate": "a license or certificate",
    "id_document":            "an ID document",
    "blank_template":         "a blank template",
    "article_or_policy":      "an article or policy",
    "form":                   "a form",
}
_OTHER_KIND = "another kind of document"

_SYSTEM = """You decide whether a document is a RESUME (also called a CV or candidate profile). The documents come from a staffing platform that parses healthcare and allied-health resumes.

A document IS a resume when it describes ONE person's own professional background: work history, education, licenses, certifications, or skills. It is still a resume when it is short, badly formatted, partly garbled by OCR, laid out as a table or form, exported from an applicant tracking system, or written in a language other than English.

A document is NOT a resume when it is something else, for example: a job description or job posting, a cover letter or reference letter with no work history, an offer letter or contract, an invoice, receipt, or pay stub, a timesheet, a blank skills checklist, a license or certificate on its own, an ID document, an empty resume template, an article, a policy, a form, or marketing material.

When in doubt, answer is_resume=true."""


class DocumentClassification(BaseModel):
    is_resume:     bool         = Field(..., description="True when the document is a resume/CV of one person")
    document_type: DocumentType = Field("other", description="What the document is; 'other' when no listed type fits")
    confidence:    float        = Field(0.0, ge=0.0, le=1.0, description="How sure you are of is_resume, 0.0-1.0")

    @field_validator("document_type", mode="before")
    @classmethod
    def _known_type(cls, v: object) -> str:
        """Normalise a label ("Job Description" -> "job_description"); anything
        outside the closed set becomes "other"."""
        label = "_".join(v.lower().replace("-", " ").split()) if isinstance(v, str) else ""
        return label if label in get_args(DocumentType) else "other"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> float:
        if isinstance(v, bool):
            return 0.0  # bool is an int: a malformed `true` is not full certainty
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class ResumeGateAgent(BaseAgent):
    name = "ResumeGateAgent"
    FAST_TIER = True  # a coarse yes/no; the fast model is enough when configured

    async def run(self, text: str, meter: TokenMeter) -> DocumentClassification:
        user = (
            f"=== DOCUMENT (first {_CLASSIFY_CHARS} characters) ===\n"
            f"{text[:_CLASSIFY_CHARS]}\n=== END ===\n\nIs this document a resume?"
        )
        return await self._structured_call(
            _SYSTEM, user, DocumentClassification, meter, max_tokens=200,
        )


def is_empty(text: str) -> bool:
    """True when the extracted text has too few real words to be any resume."""
    return len(_WORD_RE.findall(text or "")) < _MIN_WORDS


def _rejection(message: str, tokens: int) -> NotAResumeError:
    """The error to raise, carrying the tokens the gate spent so the worker can
    record them - a rejected document still cost a model call."""
    exc = NotAResumeError(message)
    exc.tokens = tokens
    return exc


async def check(text: str, *, job_id: str = "", budget: ParseBudget | None = None) -> int:
    """Raise NotAResumeError when `text` is empty or is not a resume.

    `budget` is the parse's ParseBudget; the model check gets the window it reports
    (see `ParseBudget.for_resume_gate`) and is skipped when there is none.
    Returns the tokens the classifier spent (0 when it did not run).
    """
    if is_empty(text):
        log.info("resume_gate_rejected", job_id=job_id, reason="empty")
        raise _rejection("The document is empty: no readable text was found in it.", 0)

    if not get_settings().enable_resume_classifier:
        return 0

    timeout = (budget or ParseBudget.for_async()).for_resume_gate()
    if timeout <= 0:
        log.warning("resume_gate_skipped", job_id=job_id, reason="no budget left")
        return 0

    meter = TokenMeter()
    try:
        verdict = await asyncio.wait_for(
            ResumeGateAgent().run(text, meter), timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — the classifier is a guard, never a new way to fail
        log.warning("resume_gate_skipped", job_id=job_id, error=str(exc))
        return meter.total

    # Never log the document type: the verdict is about the candidate's own document.
    log.info("resume_gate", job_id=job_id, is_resume=verdict.is_resume, confidence=verdict.confidence)
    if not verdict.is_resume and verdict.confidence >= _REJECT_CONFIDENCE:
        kind = _DOCUMENT_KIND.get(verdict.document_type, _OTHER_KIND)
        raise _rejection(f"The document is not a resume (it looks like {kind}).", meter.total)
    return meter.total
