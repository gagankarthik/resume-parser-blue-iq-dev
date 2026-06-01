"""
Full parsing pipeline orchestrator.

Ties together: extraction → cleaning → section detection →
rule parsing → AI parsing → normalization → confidence scoring.

Returns a ParseResult; never stores resume content.
"""

import re
import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI, ConfidenceScores
from app.services.extraction import classifier, pdf_extractor, docx_extractor, ocr_extractor
from app.services.extraction.classifier import ExtractionStrategy
from app.services.normalization.normalizer import normalize
from app.services.parsing import rule_parser, section_detector, ai_parser
from app.services.scoring.confidence_scorer import score

log = get_logger(__name__)


@dataclass
class PipelineInput:
    job_id: str
    filename: str
    content: bytes
    company_id: str


@dataclass
class PipelineResult:
    parsed: ParsedResumeAI
    confidence: ConfidenceScores
    file_type: str
    ocr_used: bool
    ai_tokens_used: int
    duration_ms: int


async def run(inp: PipelineInput) -> PipelineResult:
    start = time.monotonic()
    log.info("pipeline_start", job_id=inp.job_id, filename=inp.filename)

    # 1. Classify
    strategy, _ = classifier.classify(inp.filename, inp.content)
    file_type = strategy.value
    ocr_used = False

    # 2. Extract raw text
    if strategy == ExtractionStrategy.PDF:
        raw_text = pdf_extractor.extract(inp.content)
    elif strategy == ExtractionStrategy.DOCX:
        raw_text = docx_extractor.extract(inp.content)
    else:
        raw_text, ocr_used = ocr_extractor.extract(inp.content, inp.filename)

    # 3. Clean text
    cleaned = _clean_text(raw_text)

    # 4. Rule-based anchor extraction (email, phone, URLs)
    anchors = rule_parser.extract(cleaned)

    # 5. Section detection
    sections = section_detector.detect(cleaned)

    # 6. AI parsing (structured output)
    parsed_ai, tokens = await ai_parser.parse(sections, anchors)

    # 7. Normalization
    normalized = normalize(parsed_ai)

    # 8. Confidence scoring
    confidence = score(normalized)

    duration_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "pipeline_complete",
        job_id=inp.job_id,
        duration_ms=duration_ms,
        tokens=tokens,
        ocr_used=ocr_used,
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
    # Remove non-printable chars except newlines/tabs
    text = re.sub(r"[^\x09\x0A\x20-\x7E -￿]", " ", text)
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
