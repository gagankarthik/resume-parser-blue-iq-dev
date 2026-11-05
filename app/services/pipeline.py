"""
Full parsing pipeline orchestrator.

Steps (all with per-step timeouts):
  1. Classify file type -> choose extraction strategy
  2. Extract text (blocking extractors run in a worker thread - non-blocking)
  3. Clean text (Unicode-safe, preserves international names)
  4. Rule-based anchor extraction (email, phone, URLs)
  5. Section detection
  6. AI parsing (async, GPT-4.1-mini structured output)
  7. Pydantic validation
  8. Normalization (healthcare specialties, degrees, dates)
  8b. Specialty -> catalog-id matching (deterministic tiers + batched AI tier)
  9. Confidence scoring

Nothing is stored - raw content is never written to disk or database.
"""

import asyncio
import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.exceptions import AIParsingError, ExtractionError
from app.core.logging import get_logger
from app.models.schemas import ConfidenceScores, ParsedResumeAI
from app.services.budget import TIMEOUT_EXTRACTION, TIMEOUT_OCR, ParseBudget
from app.services.extraction import classifier, docx_extractor, ocr_extractor, pdf_extractor, rtf_extractor
from app.services.extraction.classifier import ExtractionStrategy
from app.services.normalization import city_resolver, credential_recovery, specialty_matcher
from app.services.normalization.normalizer import (
    cert_expiry_warnings,
    normalize,
    scan_compliance,
)
from app.services.parsing import (
    ai_parser,
    heuristic_parser,
    orchestrator,
    resume_gate,
    rule_parser,
    section_detector,
)
from app.services.parsing.rule_parser import RuleExtracted
from app.services.refinement import store as refinement_store
from app.services.scoring.confidence_scorer import score

log = get_logger(__name__)


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
    # Don't log the raw filename - resume filenames routinely embed the candidate's
    # name ("jane_smith_rn.pdf"), i.e. PII in CloudWatch. Extension + length suffice.
    log.info(
        "pipeline_start", job_id=inp.job_id,
        file_ext=inp.filename.rsplit(".", 1)[-1].lower() if "." in inp.filename else "",
        filename_len=len(inp.filename),
    )

    # Every deadline decision in this function belongs to the budget - see
    # app/services/budget.py. Its clock starts HERE, above extraction, so the time
    # extraction takes (a slow OCR pass) is charged to the parse ladder below: that
    # is what `can_afford_orchestrator()` sees. Extraction itself is bounded only by
    # its per-step caps - `for_extraction` hands them back whole.
    budget = ParseBudget.for_async()

    # -- 1-2. Classify + extract -----------------------------------------------
    raw_text, ocr_used, file_type = await _extract_text(inp, budget)

    # -- 3. Clean text (Unicode-safe) -----------------------------------------
    cleaned = _clean_text(raw_text)

    # -- 3b. Resume gate -------------------------------------------------------
    # Fail fast with NOT_A_RESUME when the document is empty or is not a resume,
    # before any AI parse budget is spent on it.
    gate_tokens = await resume_gate.check(cleaned, job_id=inp.job_id, budget=budget)

    # Learned agent rules (feedback refinement): loaded once per parse, off the event
    # loop, so no agent call ever waits on DynamoDB. A no-op when refinement is off.
    await refinement_store.refresh()

    # -- 4. Rule-based anchors -------------------------------------------------
    anchors  = rule_parser.extract(cleaned)

    # -- 5. Section detection --------------------------------------------------
    sections = section_detector.detect(cleaned)

    # -- 6. AI parsing --------------------------------------------------------
    parsed_ai, tokens, warnings, partial = await _parse_ladder(
        cleaned, sections, anchors, budget, job_id=inp.job_id,
    )

    # -- 7-9. Recover + normalize + score --------------------------------------
    # Deterministic backstop BEFORE normalization: rescue any state licence or
    # professional-association line the AI dropped (common when a mixed credentials
    # heading or a two-column layout defeats the model). Additive and conservative;
    # recovered items then flow through the same normalization/hygiene below.
    credential_recovery.recover(cleaned, parsed_ai, sections)
    normalized = normalize(parsed_ai)

    # Deterministic compliance scan (needs the full text) + tracked-cert expiry
    # warnings. Additive; fire only on genuine signals.
    normalized.compliance = scan_compliance(cleaned, normalized)
    warnings.extend(cert_expiry_warnings(normalized))

    # Post-parse enrichment: tier-4 specialty resolution and city-id enrichment.
    #   * Tier-4 specialty: one batched LLM call maps any per-role specialty the
    #     deterministic tiers missed to a catalog id (no-op without a catalog or when
    #     nothing is unmatched).
    #   * City id: opt-in, live fuzzy match against the cities endpoint using the
    #     offline-resolved country_id/state_id (no-op unless enabled + keyed).
    # They run CONCURRENTLY. Both are best-effort, time-bounded, and mutate DISJOINT
    # fields on the same roles - specialty writes experience[*].specialties[], city
    # writes experience[*].city_id/state/country - so there is no shared-state hazard,
    # and overlapping their LLM call with the cities HTTP lookups shaves their latency
    # off the tail instead of stacking it. Budgets are read once (at gather start) so
    # neither waits behind the other, and each is guarded independently: one failing
    # leaves the other and the deterministic result intact and never fails the parse.
    settings = get_settings()

    async def _specialty_ai_tier() -> int:
        spec_budget = budget.for_specialty_ai()
        if spec_budget <= 0:
            return 0
        try:
            return await asyncio.wait_for(
                specialty_matcher.resolve_unmatched_with_ai(normalized, budget=spec_budget),
                timeout=spec_budget,
            )
        except Exception as exc:  # noqa: BLE001 — tier 4 is optional, never fatal
            log.warning("specialty_ai_tier_skipped", job_id=inp.job_id, error=str(exc))
            return 0

    async def _city_api_tier() -> None:
        if not (settings.enable_city_api_match and budget.can_afford_city()):
            return
        try:
            await asyncio.wait_for(
                city_resolver.resolve_cities(normalized), timeout=budget.for_city(),
            )
        except Exception as exc:  # noqa: BLE001 — city enrichment is optional, never fatal
            log.warning("city_api_tier_skipped", job_id=inp.job_id, error=str(exc))

    spec_tokens, _ = await asyncio.gather(_specialty_ai_tier(), _city_api_tier())
    tokens += spec_tokens + gate_tokens

    confidence = score(normalized)

    mismatch = _surname_mismatch_warning(normalized)
    if mismatch:
        warnings.append(mismatch)

    # A resume almost always carries an email; on OCR'd documents a missing one
    # usually means the scan quality defeated OCR (e.g. an underlined hyperlink
    # in a phone screenshot) - surface it for review instead of a silent null.
    if ocr_used and not partial and not normalized.personal_info.email:
        warnings.append(
            "No email address was detected. The document was read via OCR and the "
            "email may be unreadable in the source image - please verify manually."
        )

    duration_ms = budget.elapsed_ms()

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


async def _extract_text(inp: PipelineInput, budget: ParseBudget) -> tuple[str, bool, str]:
    """Classify the file and extract its text: `(raw_text, ocr_used, file_type)`.

    Classification and extraction block (PyMuPDF, python-docx, OCR), so each runs in
    a worker thread - `asyncio.to_thread` also carries structlog's contextvars into
    it. Every extractor runs under its own per-step cap, and a timeout names the cap
    that actually fired.
    """
    strategy = await asyncio.to_thread(classifier.classify, inp.filename, inp.content)
    cap = budget.for_extraction(TIMEOUT_EXTRACTION)
    try:
        if strategy == ExtractionStrategy.OCR:
            cap = budget.for_extraction(TIMEOUT_OCR)
            raw_text, ocr_used = await asyncio.wait_for(
                asyncio.to_thread(ocr_extractor.extract, inp.content, inp.filename, inp.force_textract),
                timeout=cap,
            )
            return raw_text, ocr_used, strategy.value

        extract = {
            ExtractionStrategy.PDF:  pdf_extractor.extract,
            ExtractionStrategy.DOCX: docx_extractor.extract,
            ExtractionStrategy.RTF:  rtf_extractor.extract,
        }[strategy]
        raw_text = await asyncio.wait_for(asyncio.to_thread(extract, inp.content), timeout=cap)

        # The classifier picked the digital path on text *length* alone, but a
        # PDF can carry a broken/garbled text layer (e.g. CID-encoded fonts with
        # no Unicode map) that passes the length gate yet is unusable. When the
        # extracted text looks low-quality, fall back to the OCR/Textract path so
        # the resume is still read instead of feeding the AI garbage. The worker has
        # the full budget for the extra OCR pass.
        if strategy == ExtractionStrategy.PDF and _is_low_quality_pdf_text(raw_text):
            log.info(
                "pdf_text_low_quality_ocr_fallback",
                job_id=inp.job_id, chars=len(raw_text.strip()),
            )
            cap = budget.for_extraction(TIMEOUT_OCR)
            raw_text, ocr_used = await asyncio.wait_for(
                asyncio.to_thread(ocr_extractor.extract, inp.content, inp.filename, inp.force_textract),
                timeout=cap,
            )
            return raw_text, ocr_used, ExtractionStrategy.OCR.value
        return raw_text, False, strategy.value

    except TimeoutError as exc:
        raise ExtractionError(f"Text extraction timed out after {cap}s") from exc


async def _parse_ladder(
    cleaned: str,
    sections: dict[str, str],
    anchors: RuleExtracted,
    budget: ParseBudget,
    *,
    job_id: str,
) -> tuple[ParsedResumeAI, int, list[str], bool]:
    """Run the parse ladder: `(parsed, tokens, warnings, partial)`.

    One ladder, run on the worker's full budget: the multi-agent orchestrator
    (structure -> per-role -> validate) is primary, with the single-shot parser as
    fallback and the deterministic heuristic floor as the last resort so a parse
    never comes back empty. The arithmetic behind each step lives in ParseBudget.
    """
    settings = get_settings()
    use_orchestrator = (
        settings.use_multi_agent
        and len(cleaned) >= settings.multi_agent_min_chars
        and budget.can_afford_orchestrator()
    )
    if use_orchestrator:
        orch = budget.for_orchestrator()
        try:
            parsed, tokens, warnings = await asyncio.wait_for(
                orchestrator.parse(cleaned, anchors, budget=orch.budget),
                timeout=orch.timeout,  # hard net above the self-bounded stages
            )
            return parsed, tokens, warnings, False
        except (AIParsingError, TimeoutError) as exc:
            # Any partial orchestrator warnings are discarded; the fallback is authoritative.
            log.warning("orchestrator_degraded", job_id=job_id, error=str(exc))

    ai_timeout = budget.for_async_ai_parse()
    try:
        parsed, tokens = await asyncio.wait_for(
            ai_parser.parse(sections, anchors), timeout=ai_timeout,
        )
        return parsed, tokens, [], False
    except (AIParsingError, TimeoutError) as exc:
        reason = (
            f"AI parsing timed out after {ai_timeout:.0f}s"
            if isinstance(exc, TimeoutError) else f"AI parsing failed: {exc}"
        )
        log.warning("ai_parse_degraded", job_id=job_id, reason=reason)
        floor = heuristic_parser.parse(cleaned, anchors, sections)
        recovered = (
            f"{len(floor.experience)} experience, "
            f"{len(floor.education)} education, {len(floor.skills)} skill entries"
        )
        return floor, 0, [
            "AI parsing did not complete; returned a rule-based partial record "
            f"(recovered {recovered}) with no semantic cleanup. This record needs "
            f"human review. ({reason})"
        ], True


def _surname_mismatch_warning(parsed: ParsedResumeAI) -> str | None:
    """Flag a likely-incomplete surname by comparing the parsed name against the
    email local part: "Rubie Ricafort" with "rubie.ricafortmoulds@..." suggests a
    hyphenated/double surname ("Ricafort-Moulds") the resume body truncated.
    Conservative: only fires when the local part continues the surname with a
    >=4-letter alphabetic run that is not another part of the name.
    """
    pi = parsed.personal_info
    if not (pi.full_name and pi.email):
        return None
    local = re.sub(r"[^a-z]", "", pi.email.split("@", 1)[0].lower())
    tokens = [
        t for t in (re.sub(r"[^a-z]", "", w.lower()) for w in pi.full_name.split())
        if len(t) >= 3
    ]
    if not tokens or not local:
        return None
    surname = tokens[-1]
    idx = local.find(surname)
    if idx < 0:
        return None
    suffix = local[idx + len(surname):]
    if len(suffix) >= 4 and suffix.isalpha() and suffix not in tokens:
        return (
            f"The email address suggests a longer surname than the parsed name "
            f"(\"{pi.full_name}\" vs \"...{surname}{suffix}@\"). The resume may "
            "truncate a hyphenated or double surname - review full_name."
        )
    return None


# A digital PDF must clear this many usable characters before we trust its text
# layer; below it, the OCR fallback is cheaper insurance than parsing nothing.
_MIN_PDF_TEXT_CHARS = 120
# Minimum fraction of "wordy" characters (letters, digits, common punctuation)
# among non-space characters. Garbled CID extractions are dominated by boxes,
# replacement chars, and stray symbols and fall well below this.
_MIN_PDF_WORDY_RATIO = 0.55


def _is_low_quality_pdf_text(text: str) -> bool:
    """Heuristic: does this digital-PDF text layer look broken/garbled?

    Conservative by design - a false positive only costs one OCR pass, but a
    false negative feeds the AI unreadable junk. Flags text that is too short,
    riddled with PyMuPDF CID artefacts, or has too few wordy characters.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_PDF_TEXT_CHARS:
        return True
    # PyMuPDF emits "(cid:NN)" when a font has no Unicode map - a strong signal
    # the text layer cannot be decoded to real characters.
    if stripped.count("(cid:") >= 5:
        return True
    non_space = [c for c in stripped if not c.isspace()]
    if not non_space:
        return True
    wordy = sum(c.isalnum() or c in ".,/-:;()&'@" for c in non_space)
    if wordy / len(non_space) < _MIN_PDF_WORDY_RATIO:
        return True
    # A readable resume always has a decent run of alphabetic characters; an all-
    # symbol or replacement-char (�) layer does not.
    letters = sum(c.isalpha() for c in non_space)
    if letters / len(non_space) < 0.40:
        return True
    return False


def _clean_text(text: str) -> str:
    """
    Clean extracted text while preserving:
      * All Unicode letters (é, ñ, ü, Arabic, Chinese, etc.) - healthcare
        workers often have non-ASCII names
      * Newlines and spaces needed for structure
    Removes only:
      * C0/C1 control characters (null bytes, bells, form feeds)
      * Surrogates and private-use area garbage
    """
    # Remove C0 control chars except tab (\x09) and LF (\x0a)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    # Remove Unicode surrogates and private-use areas
    text = re.sub(r"[\ud800-\udfff-]", " ", text)
    # Fix common OCR ligature artefacts
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    # Collapse excessive blank lines (3+ -> 2)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
