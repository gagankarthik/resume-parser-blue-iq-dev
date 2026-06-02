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
from dataclasses import dataclass

from app.core.exceptions import AIParsingError, ExtractionError
from app.core.logging import get_logger
from app.models.schemas import ConfidenceScores, ParsedResumeAI
from app.services.extraction import classifier, docx_extractor, ocr_extractor, pdf_extractor
from app.services.extraction.classifier import ExtractionStrategy
from app.services.normalization.normalizer import normalize
from app.services.parsing import ai_parser, rule_parser, section_detector
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


@dataclass
class PipelineResult:
    parsed:         ParsedResumeAI
    confidence:     ConfidenceScores
    file_type:      str
    ocr_used:       bool
    ai_tokens_used: int
    duration_ms:    int


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
        elif strategy == ExtractionStrategy.DOCX:
            raw_text = await asyncio.wait_for(
                loop.run_in_executor(None, docx_extractor.extract, inp.content),
                timeout=_TIMEOUT_EXTRACTION,
            )
        else:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, ocr_extractor.extract, inp.content, inp.filename),
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
    try:
        parsed_ai, tokens = await asyncio.wait_for(
            ai_parser.parse(sections, anchors),
            timeout=_TIMEOUT_AI_PARSE,
        )
    except TimeoutError as exc:
        raise AIParsingError(
            f"AI parsing timed out after {_TIMEOUT_AI_PARSE}s"
        ) from exc

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
    )

    return PipelineResult(
        parsed=normalized,
        confidence=confidence,
        file_type=file_type,
        ocr_used=ocr_used,
        ai_tokens_used=tokens,
        duration_ms=duration_ms,
    )


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
