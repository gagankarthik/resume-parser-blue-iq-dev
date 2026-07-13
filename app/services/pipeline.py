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
  8b. Specialty → catalog-id matching (deterministic tiers + batched AI tier)
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
from app.services.extraction import classifier, docx_extractor, ocr_extractor, pdf_extractor, rtf_extractor
from app.services.extraction.classifier import ExtractionStrategy
from app.services.normalization import city_resolver, specialty_matcher
from app.services.normalization.normalizer import (
    cert_expiry_warnings,
    normalize,
    scan_compliance,
)
from app.services.parsing import ai_parser, heuristic_parser, orchestrator, rule_parser, section_detector
from app.services.scoring.confidence_scorer import score

log = get_logger(__name__)

# Per-step timeouts (seconds)
_TIMEOUT_EXTRACTION = 60
_TIMEOUT_OCR        = 90    # Textract on multi-page scans — bounded so OCR alone
                            # can't eat the whole per-resume budget
# The orchestrator self-bounds each stage (see orchestrator._STAGE2_TIMEOUT et al.)
# and returns a partial result rather than being cancelled, so this is only a hard
# safety net set just above its internal budget. The single-shot fallback is one
# LLM call, so it gets a tighter cap — together they bound the worst case instead
# of stacking two full 2-minute timeouts when the orchestrator degrades.
_TIMEOUT_ORCHESTRATOR = 130
# Single-shot cap. Measured single-shot parse time for a dense multi-role resume
# is 39–55s (e.g. a 12-role radiology resume: 39.1s → 12 roles fully extracted),
# so a 45s cap sat right on the cliff and normal OpenAI latency variance tipped
# it into a contact-only "partial". Give it real headroom — the Lambda function
# timeout is 300s, so the binding constraint is the total budget below, not this.
_TIMEOUT_AI_PARSE     = 90

# Overall soft budget for one resume. Every AI step is capped by the time left
# under this budget. Sized to let a slow orchestrator degrade AND still leave a
# full single-shot fallback, comfortably inside the 300s Lambda timeout.
_TOTAL_BUDGET     = 200
# Time held back from the orchestrator for the single-shot fallback + scoring —
# large enough for a full _TIMEOUT_AI_PARSE fallback attempt after a degrade.
_FALLBACK_RESERVE = 100

# Wall-clock budget for a SYNCHRONOUS request. Sized for the ceiling of the gateway
# a DIRECT API caller sits behind: our CloudFront origin read timeout, 60s (see
# infrastructure/terraform/cloudfront_api.tf). The parse must degrade and answer
# before that, or the caller gets a bare 504 with no data at all.
#
# This budget CANNOT protect a caller behind a tighter gateway. The UAT console, for
# one, reaches this API through a Next.js route handler on AWS Amplify Hosting,
# whose SSR compute has a HARD 30s request timeout — not configurable, no quota to
# raise, and Next's `maxDuration` is not honored there. A measured single-shot parse
# of even a *typical* two-role résumé takes ~20s, so a complete synchronous parse
# does not fit 30s once extraction, normalization and transfer are counted: there is
# no budget value that makes it work. Such callers must not block on a parse at all
# — they pass `async_only` and poll instead (see app/api/v1/endpoints/resume.py).
_SYNC_WALL_BUDGET = 50
# Smallest AI-parse window worth opening on the sync path. If less than this is
# left after extraction, don't start a call we know cannot land — degrade straight
# to the floor so the caller promotes to async while it still has budget to do so.
_MIN_SYNC_AI_TIMEOUT = 8
# Time held back from the SYNC single-shot parse for the section-only "enrich"
# pass that runs when it times out. The single-shot is capped this many seconds
# BELOW the wall budget so a résumé that would run long is cut early, leaving room
# to recover the semantic sections (headline, secondary phone, education
# locations, skills, certs) with fast section agents instead of dropping to the
# contact-only floor. Only used by a sync caller that RETURNS the partial; probe
# callers promote instead and hand this time to the single-shot (see sync_probe).
_SYNC_ENRICH_RESERVE = 14
# Time the sync path holds back from EXTRACTION for the AI parse + scoring that
# must follow it. Extraction used to run entirely outside the sync budget, on its
# own 60s/90s caps, so one slow step could blow the gateway ceiling before the AI
# parse even began — an independent source of 504s that this budget never saw.
_SYNC_EXTRACT_RESERVE = 20
# Never hand an extraction step less than this; below it the step is pointless.
_MIN_EXTRACT_TIMEOUT = 5


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
    # True when the caller is waiting synchronously on the HTTP response, so the
    # pipeline must finish within the gateway ceiling (_SYNC_WALL_BUDGET) and
    # degrade gracefully rather than run to the full budget and 504. The async
    # worker leaves this False to use the full _TOTAL_BUDGET.
    sync: bool = False
    # True when this sync run is only a fast PROBE: if the single-shot can't finish
    # in the budget, the caller will re-dispatch the file to the async worker (full
    # budget, complete parse) instead of returning the partial. So skip the costly
    # section-only enrich pass (it would be thrown away) and give the single-shot
    # the reserve time instead. Only meaningful together with sync=True.
    sync_probe: bool = False


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

    # Don't log the raw filename — résumé filenames routinely embed the candidate's
    # name ("jane_smith_rn.pdf"), i.e. PII in CloudWatch. Extension + length suffice.
    log.info(
        "pipeline_start", job_id=inp.job_id,
        file_ext=inp.filename.rsplit(".", 1)[-1].lower() if "." in inp.filename else "",
        filename_len=len(inp.filename),
    )

    # Wall-clock budget for the whole run. Declared HERE, above extraction, so that
    # every stage below is bounded by it — extraction included. (It used to be
    # declared just before the AI parse, which left extraction's 60s/90s per-step
    # caps free to overrun the sync gateway ceiling on their own.)
    budget = _SYNC_WALL_BUDGET if inp.sync else _TOTAL_BUDGET

    def _remaining() -> float:
        return budget - (time.monotonic() - start)

    def _extract_timeout(cap: float) -> float:
        """Cap one extraction step by the time actually left in the budget.

        The async worker keeps the full per-step cap. A sync request cannot: it must
        leave room for the AI parse that follows and still answer inside the gateway
        ceiling, so the step is clamped to what the budget can really afford.
        """
        if not inp.sync:
            return cap
        return max(_MIN_EXTRACT_TIMEOUT, min(cap, _remaining() - _SYNC_EXTRACT_RESERVE))

    # ── 1. Classify ───────────────────────────────────────────────────────────
    strategy, _ = classifier.classify(inp.filename, inp.content)
    file_type   = strategy.value
    ocr_used    = False
    # Set when a sync run discovers the file can only be read by OCR, which cannot
    # fit the sync budget. The AI parse is then skipped and the run degrades to a
    # flagged partial, which the endpoints promote to the async worker.
    sync_needs_ocr = False

    # ── 2. Extract (sync extractors → executor, async-safe) ───────────────────
    try:
        if strategy == ExtractionStrategy.PDF:
            raw_text = await asyncio.wait_for(
                loop.run_in_executor(None, pdf_extractor.extract, inp.content),
                timeout=_extract_timeout(_TIMEOUT_EXTRACTION),
            )
            # The classifier picked the digital path on text *length* alone, but a
            # PDF can carry a broken/garbled text layer (e.g. CID-encoded fonts with
            # no Unicode map) that passes the length gate yet is unusable. When the
            # extracted text looks low-quality, fall back to the OCR/Textract path so
            # the resume is still read instead of feeding the AI garbage.
            if _is_low_quality_pdf_text(raw_text):
                if inp.sync:
                    # An OCR pass alone is budgeted at _TIMEOUT_OCR — several times the
                    # whole sync wall budget. Starting it behind a gateway that severs
                    # the connection at 30s just guarantees a bodyless 504. Bail out to
                    # a partial instead: the caller promotes the file to the async
                    # worker, which OCRs and parses it on the full budget.
                    log.info(
                        "pdf_text_low_quality_needs_async",
                        job_id=inp.job_id, chars=len(raw_text.strip()),
                    )
                    sync_needs_ocr = True
                else:
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
                timeout=_extract_timeout(_TIMEOUT_EXTRACTION),
            )
        elif strategy == ExtractionStrategy.RTF:
            raw_text = await asyncio.wait_for(
                loop.run_in_executor(None, rtf_extractor.extract, inp.content),
                timeout=_extract_timeout(_TIMEOUT_EXTRACTION),
            )
        else:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, ocr_extractor.extract,
                    inp.content, inp.filename, inp.force_textract,
                ),
                timeout=_extract_timeout(_TIMEOUT_OCR),
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

    # ── 6. AI parsing ────────────────────────────────────────────────────────
    # The two request shapes get different ladders because they have very
    # different budgets:
    #   ASYNC (worker, full budget): multi-agent orchestrator (structure → per-role
    #     → validate) → single-shot fallback → deterministic floor.
    #   SYNC (gateway ceiling — see _SYNC_WALL_BUDGET): single-shot PRIMARY — fast
    #     AND complete for the résumés it parses inside its cap. The cap sits below
    #     the wall budget so a résumé that would run long is cut early, leaving room
    #     for a section-only "enrich" pass (semantic sections, no slow per-role work
    #     stage) plus deterministic work history — far richer than the bare floor.
    #     Probe callers skip the enrich and promote to async instead.
    #     (The full orchestrator was tried on sync and dropped the per-role work
    #     stage — cancelled on the tight budget — silently losing all experience.)
    settings  = get_settings()
    warnings: list[str] = []
    partial   = False
    tokens    = 0
    parsed_ai = None

    if sync_needs_ocr:
        # Unreadable text layer on the sync path (see extraction). There is nothing
        # worth feeding the AI, and OCR does not fit the budget — degrade to the
        # deterministic floor and flag it. `partial` is the signal the endpoints use
        # to promote the file to the async worker, which reads it properly via OCR.
        parsed_ai = heuristic_parser.parse(cleaned, anchors)
        partial   = True
        warnings.append(
            "This PDF's text layer could not be decoded, so the résumé needs OCR — "
            "which does not fit the synchronous budget. Re-run it on the asynchronous "
            "path (the API does this for you) to get a complete record."
        )
    elif not inp.sync:
        # ── ASYNC: orchestrator primary → single-shot → floor ─────────────────
        use_orchestrator = (
            settings.use_multi_agent
            and len(cleaned) >= settings.multi_agent_min_chars
            # If extraction (e.g. a slow OCR pass) already ate most of the budget,
            # go straight to the cheaper single-shot parser.
            and _remaining() > _FALLBACK_RESERVE + 15
        )
        if use_orchestrator:
            orch_budget = min(_TIMEOUT_ORCHESTRATOR, _remaining() - _FALLBACK_RESERVE)
            try:
                parsed_ai, tokens, warnings = await asyncio.wait_for(
                    orchestrator.parse(cleaned, anchors, budget=orch_budget),
                    timeout=orch_budget + 10,  # hard net above the self-bounded stages
                )
            except (AIParsingError, TimeoutError) as exc:
                log.warning("orchestrator_degraded", job_id=inp.job_id, error=str(exc))
                warnings = []  # discard partial orchestrator warnings; the fallback is authoritative

        if parsed_ai is None:
            ai_timeout = min(_TIMEOUT_AI_PARSE, max(15.0, _remaining()))
            try:
                parsed_ai, tokens = await asyncio.wait_for(
                    ai_parser.parse(sections, anchors), timeout=ai_timeout,
                )
            except (AIParsingError, TimeoutError) as exc:
                reason = (
                    f"AI parsing timed out after {ai_timeout:.0f}s"
                    if isinstance(exc, TimeoutError) else f"AI parsing failed: {exc}"
                )
                log.warning("ai_parse_degraded", job_id=inp.job_id, reason=reason)
                parsed_ai = heuristic_parser.parse(cleaned, anchors)
                partial = True
                recovered = (
                    f"{len(parsed_ai.experience)} experience, "
                    f"{len(parsed_ai.education)} education, {len(parsed_ai.skills)} skill entries"
                )
                warnings.append(
                    "AI parsing did not complete; returned a rule-based partial record "
                    f"(recovered {recovered}) with no semantic cleanup. This record needs "
                    f"human review. ({reason})"
                )
    else:
        # ── SYNC: single-shot primary ─────────────────────────────────────────
        # In PROBE mode the caller promotes any partial to the async worker (full
        # budget, complete parse), so the section-only enrich would be thrown away
        # — skip it and hand the single-shot the reserve time instead (bigger cap →
        # more résumés finish synchronously). Otherwise (a caller that returns the
        # partial directly) keep the enrich reserve.
        enrich_reserve = 3.0 if inp.sync_probe else _SYNC_ENRICH_RESERVE
        # Clamp to what is genuinely left. The old `max(15.0, …)` floor here meant a
        # slow extraction could still hand the AI a 15s window the budget could not
        # afford, overshooting the gateway ceiling — the very 504 this budget exists
        # to prevent. If the window is too small to be worth opening, degrade now and
        # let the caller promote to async while there is still time to dispatch.
        ai_timeout = min(_TIMEOUT_AI_PARSE, _remaining() - enrich_reserve)
        try:
            if ai_timeout < _MIN_SYNC_AI_TIMEOUT:
                raise TimeoutError(
                    f"only {max(0.0, ai_timeout):.0f}s of the {_SYNC_WALL_BUDGET}s "
                    "synchronous budget was left for the AI parse"
                )
            parsed_ai, tokens = await asyncio.wait_for(
                ai_parser.parse(sections, anchors), timeout=ai_timeout,
            )
        except (AIParsingError, TimeoutError) as exc:
            reason = (
                f"AI parsing did not fit the {_SYNC_WALL_BUDGET}s synchronous budget"
                if isinstance(exc, TimeoutError) else f"AI parsing failed: {exc}"
            )
            log.warning("ai_parse_degraded", job_id=inp.job_id, reason=reason)
            # Deterministic floor is the base — never empty, never times out.
            floor = heuristic_parser.parse(cleaned, anchors)
            parsed_ai = floor
            partial = True
            # Enrich: recover the high-value semantic sections (headline, secondary
            # phone, education locations, skills, certs, licenses) with the fast
            # section-only agents — omitting the slow per-role work stage — and keep
            # the deterministic work history. Skipped in probe mode (the partial is
            # not returned to the caller — it triggers an async re-parse instead).
            if not inp.sync_probe and settings.use_multi_agent and _remaining() > 9:
                try:
                    light, ltok, lwarn = await asyncio.wait_for(
                        orchestrator.parse_light(cleaned, anchors, budget=_remaining() - 3),
                        timeout=_remaining() - 1,
                    )
                    tokens += ltok
                    parsed_ai = _backfill_from_floor(light, floor)
                    warnings.extend(lwarn)
                except (AIParsingError, TimeoutError) as exc2:  # noqa: BLE001 — enrich is optional
                    log.warning("sync_enrich_failed", job_id=inp.job_id, error=str(exc2))
            recovered = (
                f"{len(parsed_ai.experience)} experience, "
                f"{len(parsed_ai.education)} education, {len(parsed_ai.skills)} skill entries"
            )
            warnings.append(
                "The high-accuracy parse did not finish in time; returned a recovered "
                f"record (recovered {recovered}; work history not semantically verified). "
                f"This record needs human review. ({reason})"
            )

    # ── 7–9. Normalize + score ────────────────────────────────────────────────
    normalized = normalize(parsed_ai)

    # Deterministic compliance scan (needs the full text) + tracked-cert expiry
    # warnings. Additive; fire only on genuine signals.
    normalized.compliance = scan_compliance(cleaned, normalized)
    warnings.extend(cert_expiry_warnings(normalized))

    # Tier-4 specialty resolution: one batched LLM call maps any per-role specialty
    # the deterministic tiers missed to a catalog id (no-op without a catalog or
    # when nothing is unmatched). Best-effort and time-bounded — a failure leaves
    # the deterministic matches intact and never fails the parse.
    spec_budget = _remaining() - 5  # keep a little headroom for scoring
    if spec_budget > 0:
        try:
            tokens += await asyncio.wait_for(
                specialty_matcher.resolve_unmatched_with_ai(normalized, budget=spec_budget),
                timeout=spec_budget,
            )
        except Exception as exc:  # noqa: BLE001 — tier 4 is optional, never fatal
            log.warning("specialty_ai_tier_skipped", job_id=inp.job_id, error=str(exc))

    # City id enrichment: opt-in, live fuzzy match against the cities endpoint using
    # the offline-resolved country_id/state_id. No-op unless enabled + keyed. Best-
    # effort and time-bounded — a failure leaves the deterministic result intact.
    if get_settings().enable_city_api_match and _remaining() > 3:
        try:
            await asyncio.wait_for(
                city_resolver.resolve_cities(normalized), timeout=_remaining() - 1,
            )
        except Exception as exc:  # noqa: BLE001 — city enrichment is optional, never fatal
            log.warning("city_api_tier_skipped", job_id=inp.job_id, error=str(exc))

    confidence = score(normalized)

    mismatch = _surname_mismatch_warning(normalized)
    if mismatch:
        warnings.append(mismatch)

    # A résumé almost always carries an email; on OCR'd documents a missing one
    # usually means the scan quality defeated OCR (e.g. an underlined hyperlink
    # in a phone screenshot) — surface it for review instead of a silent null.
    if ocr_used and not partial and not normalized.personal_info.email:
        warnings.append(
            "No email address was detected. The document was read via OCR and the "
            "email may be unreadable in the source image — please verify manually."
        )

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


def _surname_mismatch_warning(parsed: ParsedResumeAI) -> str | None:
    """Flag a likely-incomplete surname by comparing the parsed name against the
    email local part: "Rubie Ricafort" with "rubie.ricafortmoulds@…" suggests a
    hyphenated/double surname ("Ricafort-Moulds") the résumé body truncated.
    Conservative: only fires when the local part continues the surname with a
    ≥4-letter alphabetic run that is not another part of the name.
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
            f"(\"{pi.full_name}\" vs \"…{surname}{suffix}@\"). The résumé may "
            "truncate a hyphenated or double surname — review full_name."
        )
    return None


def _backfill_from_floor(primary: ParsedResumeAI, floor: ParsedResumeAI) -> ParsedResumeAI:
    """Fill sections the semantic parse left empty with the deterministic parser's
    recovery. Used on the sync enrich path: `primary` carries the high-quality
    section-agent output (personal / education / credentials); `floor` supplies the
    work history — and any other section the agents could not finish in time — so
    a section is never silently dropped when its agent times out under the tight
    enrich budget (e.g. a slow CredentialsAgent losing every certification)."""
    if not primary.experience:
        primary.experience = floor.experience
    if not primary.education:
        primary.education = floor.education
    if not primary.skills:
        primary.skills = floor.skills
    if not primary.certifications:
        primary.certifications = floor.certifications
    if not primary.licenses:
        primary.licenses = floor.licenses
    return primary


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
