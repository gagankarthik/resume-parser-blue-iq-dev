"""
Full parsing pipeline orchestrator.

Steps (all with per-step timeouts):
  1. Classify file type → choose extraction strategy
  2. Extract text (sync extractors run in executor — non-blocking)
  3. Clean text (Unicode-safe, preserves international names)
  4. Rule-based anchor extraction (email, phone, URLs)
  5. Section detection
  6. AI parsing (async, GPT-4o structured output)
  7. Pydantic validation
  8. Normalization (healthcare specialties, degrees, dates)
  9. Confidence scoring

Nothing is stored — raw content is never written to disk or database.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.exceptions import AIParsingError, ExtractionError
from app.core.logging import get_logger
from app.models.schemas import ConfidenceScores, ParsedResumeAI, PersonalInfo
from app.services.extraction import classifier, docx_extractor, ocr_extractor, pdf_extractor
from app.services.extraction.classifier import ExtractionStrategy
from app.services.normalization.normalizer import normalize
from app.services.parsing import ai_parser, orchestrator, rule_parser, section_detector
from app.services.scoring.confidence_scorer import score

log = get_logger(__name__)

# Per-step timeouts (seconds)
_TIMEOUT_EXTRACTION = 60
_TIMEOUT_OCR        = 180   # Textract can be slow on multi-page scans
_TIMEOUT_AI_PARSE   = 120


@dataclass
class PipelineInput:
    job_id:     str
    filename:   str
    content:    bytes
    company_id: str
    # Force AWS Textract instead of Tesseract for any OCR this resume needs
    # (scanned files, or a digital PDF that falls back to OCR). OR's with the
    # global settings.force_textract default.
    force_textract: bool = False


@dataclass
class PipelineResult:
    parsed:         ParsedResumeAI
    confidence:     ConfidenceScores
    file_type:      str
    ocr_used:       bool
    ai_tokens_used: int
    duration_ms:    int
    # Graceful-degradation flags. `partial` is True when the AI parse failed and
    # `parsed` holds only what rule-based extraction could recover (contact
    # anchors). `warnings` carries human-readable notes for review.
    partial:        bool = False
    warnings:       list[str] = field(default_factory=list)


async def run(inp: PipelineInput) -> PipelineResult:
    start = time.monotonic()
    loop  = asyncio.get_event_loop()

    log.info("pipeline_start", job_id=inp.job_id, filename=inp.filename)

    # ── 1. Classify ───────────────────────────────────────────────────────────
    strategy, _ = classifier.classify(inp.filename, inp.content)
    file_type   = strategy.value
    ocr_used    = False

    # ── 2. Extract (sync extractors → executor, async-safe) ───────────────────
    try:
        if strategy == ExtractionStrategy.PDF:
            raw_text = await asyncio.wait_for(
                loop.run_in_executor(None, pdf_extractor.extract, inp.content),
                timeout=_TIMEOUT_EXTRACTION,
            )
            # The classifier picked the digital path on text *length* alone, but a
            # PDF can carry a broken/garbled text layer (e.g. CID-encoded fonts with
            # no Unicode map) that passes the length gate yet is unusable. When the
            # extracted text looks low-quality, fall back to the OCR/Textract path so
            # the resume is still read instead of feeding the AI garbage.
            if _is_low_quality_pdf_text(raw_text):
                log.info(
                    "pdf_text_low_quality_ocr_fallback",
                    job_id=inp.job_id, chars=len(raw_text.strip()),
                )
                raw_text, ocr_used = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, ocr_extractor.extract,
                        inp.content, inp.filename, inp.force_textract,
                    ),
                    timeout=_TIMEOUT_OCR,
                )
                file_type = ExtractionStrategy.OCR.value
        elif strategy == ExtractionStrategy.DOCX:
            raw_text = await asyncio.wait_for(
                loop.run_in_executor(None, docx_extractor.extract, inp.content),
                timeout=_TIMEOUT_EXTRACTION,
            )
        else:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, ocr_extractor.extract,
                    inp.content, inp.filename, inp.force_textract,
                ),
                timeout=_TIMEOUT_OCR,
            )
            raw_text, ocr_used = result

    except TimeoutError as exc:
        timeout = _TIMEOUT_OCR if strategy == ExtractionStrategy.OCR else _TIMEOUT_EXTRACTION
        raise ExtractionError(
            f"Text extraction timed out after {timeout}s"
        ) from exc

    # ── 3. Clean text (Unicode-safe) ─────────────────────────────────────────
    cleaned = _clean_text(raw_text)

    # ── 4. Rule-based anchors ─────────────────────────────────────────────────
    anchors  = rule_parser.extract(cleaned)

    # ── 5. Section detection ──────────────────────────────────────────────────
    sections = section_detector.detect(cleaned)

    # ── 6. AI parsing (with per-call timeout) ────────────────────────────────
    # Three tiers, most accurate first, each degrading into the next so a failure
    # never produces a silent total failure:
    #   1. Multi-agent orchestrator (structure → per-role → validate) — most accurate
    #   2. Single-shot structured-output parser — fast, robust fallback
    #   3. Partial record built from rule-based contact anchors — last resort, flagged
    settings  = get_settings()
    warnings: list[str] = []
    partial   = False
    tokens    = 0
    parsed_ai = None

    use_orchestrator = (
        settings.use_multi_agent and len(cleaned) >= settings.multi_agent_min_chars
    )
    if use_orchestrator:
        try:
            parsed_ai, tokens, warnings = await asyncio.wait_for(
                orchestrator.parse(cleaned, anchors),
                timeout=_TIMEOUT_AI_PARSE,
            )
        except (AIParsingError, TimeoutError) as exc:
            log.warning("orchestrator_degraded", job_id=inp.job_id, error=str(exc))
            warnings = []  # discard partial orchestrator warnings; single-shot is authoritative

    if parsed_ai is None:
        try:
            parsed_ai, tokens = await asyncio.wait_for(
                ai_parser.parse(sections, anchors),
                timeout=_TIMEOUT_AI_PARSE,
            )
        except (AIParsingError, TimeoutError) as exc:
            reason = (
                f"AI parsing timed out after {_TIMEOUT_AI_PARSE}s"
                if isinstance(exc, TimeoutError)
                else f"AI parsing failed: {exc}"
            )
            log.warning("ai_parse_degraded", job_id=inp.job_id, reason=reason)
            parsed_ai = _fallback_from_anchors(anchors)
            partial = True
            warnings.append(
                "AI parsing did not complete; returned a partial record built from "
                f"detected contact details only. This record needs human review. ({reason})"
            )

    # ── 7–9. Normalize + score ────────────────────────────────────────────────
    normalized = normalize(parsed_ai)
    confidence = score(normalized)

    duration_ms = int((time.monotonic() - start) * 1000)

    log.info(
        "pipeline_complete",
        job_id=inp.job_id,
        duration_ms=duration_ms,
        tokens=tokens,
        ocr_used=ocr_used,
        file_type=file_type,
        overall_confidence=confidence.overall,
        partial=partial,
    )

    return PipelineResult(
        parsed=normalized,
        confidence=confidence,
        file_type=file_type,
        ocr_used=ocr_used,
        ai_tokens_used=tokens,
        duration_ms=duration_ms,
        partial=partial,
        warnings=warnings,
    )


def _fallback_from_anchors(anchors: rule_parser.RuleExtracted) -> ParsedResumeAI:
    """Build a minimal ParsedResumeAI from rule-extracted contact anchors.

    Used when the AI parser fails so the caller still receives a structured,
    if sparse, record instead of nothing. Only high-confidence regex anchors are
    populated — never invented data.
    """
    personal = PersonalInfo(
        email=anchors.emails[0] if anchors.emails else None,
        phone=anchors.phones[0] if anchors.phones else None,
        linkedin_url=anchors.linkedin_urls[0] if anchors.linkedin_urls else None,
        github_url=anchors.github_urls[0] if anchors.github_urls else None,
        portfolio_url=anchors.portfolio_urls[0] if anchors.portfolio_urls else None,
    )
    return ParsedResumeAI(personal_info=personal)


# A digital PDF must clear this many usable characters before we trust its text
# layer; below it, the OCR fallback is cheaper insurance than parsing nothing.
_MIN_PDF_TEXT_CHARS = 120
# Minimum fraction of "wordy" characters (letters, digits, common punctuation)
# among non-space characters. Garbled CID extractions are dominated by boxes,
# replacement chars, and stray symbols and fall well below this.
_MIN_PDF_WORDY_RATIO = 0.55


def _is_low_quality_pdf_text(text: str) -> bool:
    """Heuristic: does this digital-PDF text layer look broken/garbled?

    Conservative by design — a false positive only costs one OCR pass, but a
    false negative feeds the AI unreadable junk. Flags text that is too short,
    riddled with PyMuPDF CID artefacts, or has too few wordy characters.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_PDF_TEXT_CHARS:
        return True
    # PyMuPDF emits "(cid:NN)" when a font has no Unicode map — a strong signal
    # the text layer cannot be decoded to real characters.
    if stripped.count("(cid:") >= 5:
        return True
    non_space = [c for c in stripped if not c.isspace()]
    if not non_space:
        return True
    wordy = sum(c.isalnum() or c in ".,/-:;()&'@" for c in non_space)
    if wordy / len(non_space) < _MIN_PDF_WORDY_RATIO:
        return True
    # A readable résumé always has a decent run of alphabetic characters; an all-
    # symbol or replacement-char (�) layer does not.
    letters = sum(c.isalpha() for c in non_space)
    if letters / len(non_space) < 0.40:
        return True
    return False


def _clean_text(text: str) -> str:
    """
    Clean extracted text while preserving:
      • All Unicode letters (é, ñ, ü, Arabic, Chinese, etc.) — healthcare
        workers often have non-ASCII names
      • Newlines and spaces needed for structure
    Removes only:
      • C0/C1 control characters (null bytes, bells, form feeds)
      • Surrogates and private-use area garbage
    """
    # Remove C0 control chars except tab (\x09) and LF (\x0a)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    # Remove Unicode surrogates and private-use areas
    text = re.sub(r"[\ud800-\udfff-]", " ", text)
    # Fix common OCR ligature artefacts
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    # Collapse excessive blank lines (3+ → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
