"""
Multi-agent orchestrator — the high-accuracy parsing path.

Pipeline:
  Stage 1  StructureAgent          → map roles + bullet counts (sequential)
  Stage 2  Personal / Work / Education / Credentials / Supplemental (parallel)
           — Work extracts each mapped role independently
  Stage 4  ValidatorAgent          → reconcile bullet counts, re-extract mismatches

Resilience: every Stage-2 agent runs under return_exceptions, so one section
failing degrades only THAT section (empty default) instead of killing the whole
parse. The orchestrator raises only if it produced nothing usable, letting the
pipeline fall back to the single-shot parser.

Returns (ParsedResumeAI, total_tokens, warnings).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable

from app.core.exceptions import AIParsingError
from app.core.logging import get_logger
from app.models.schemas import (
    EducationItem,
    ExperienceItem,
    ParsedResumeAI,
    PersonalInfo,
)
from app.services.parsing.agents.base import TokenMeter
from app.services.parsing.agents.credentials import CredentialsAgent
from app.services.parsing.agents.education import EducationAgent
from app.services.parsing.agents.personal import PersonalInfoAgent
from app.services.parsing.agents.schemas import (
    CredentialsResult,
    PersonalResult,
    SupplementalResult,
)
from app.services.parsing.agents.structure import StructureAgent
from app.services.parsing.agents.supplemental import SupplementalAgent
from app.services.parsing.agents.validator import ValidatorAgent
from app.services.parsing.agents.work import WorkExperienceAgent
from app.services.parsing.rule_parser import RuleExtracted

log = get_logger(__name__)

# Each agent receives the full résumé text, and the WorkAgent sends it once PER
# role. Cap the text so a pathologically long résumé can't blow the model context
# window or run up unbounded token cost across the fan-out. Kept in line with the
# single-shot parser's MAX_TOTAL_CHARS so the multi-agent path (used for the LONGER
# résumés that trip multi_agent_min_chars) doesn't silently see LESS text than the
# fallback would — the earlier 30K cap dropped the tail of 30-60K CVs with no
# signal. Truncation now emits a warning (see parse()).
_MAX_AGENT_CHARS = 60_000

# Per-stage soft deadlines. The orchestrator bounds ITSELF so a slow stage returns
# whatever it has (partial + warning) instead of letting the pipeline's outer
# wait_for cancel the whole parse and discard all completed work. The sum stays
# comfortably under the pipeline's orchestrator timeout so this graceful path wins,
# and the whole parse stays inside the pipeline's ≤2-minute budget.
_STRUCTURE_TIMEOUT = 20   # one sequential call
# Per-role work fan-out + section agents. A dense resume (e.g. 12 roles) needs
# well over 60s here — at 60s the WorkExperienceAgent was cancelled and the parse
# came back with experience=0. Sized against the larger pipeline budget so the
# work stage can actually finish before the graceful net fires.
_STAGE2_TIMEOUT    = 120
_VALIDATOR_TIMEOUT = 15   # re-extraction of mismatched roles only


def _unwrap[T](result: object, default: T, agent: str, warnings: list[str]) -> T:
    if isinstance(result, Exception):
        log.warning("agent_section_failed", agent=agent, error=str(result))
        warnings.append(f"{agent} failed; that section may be incomplete.")
        return default
    return result  # type: ignore[return-value]


async def _run_sections_bounded(
    specs: list[tuple[str, Awaitable[object]]], *, timeout: float
) -> list[object]:
    """Run section coroutines concurrently under a soft deadline.

    Returns a list aligned 1:1 with `specs`: the agent's result, the exception it
    raised, or a TimeoutError for any section still running when `timeout` elapses
    (those are cancelled). Every element is safe to feed to `_unwrap`, so a slow or
    failed section degrades to its default instead of cancelling the whole parse.
    """
    tasks = [asyncio.ensure_future(coro) for _, coro in specs]
    done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        # Let the cancellations settle so no "Task was destroyed" warnings escape.
        await asyncio.gather(*pending, return_exceptions=True)

    out: list[object] = []
    for (name, _), task in zip(specs, tasks):
        if task in done:
            out.append(task.exception() or task.result())
        else:
            log.warning("agent_section_timeout", agent=name, timeout=timeout)
            out.append(TimeoutError(f"{name} timed out after {timeout}s"))
    return out


def _stage_timeout(default: float, deadline: float | None, reserve: float) -> float:
    """Cap a stage's default budget by the time left before the overall deadline,
    holding back `reserve` seconds for the stages that follow it. Never below 5s
    so a stage isn't started just to be cancelled immediately."""
    if deadline is None:
        return default
    return max(5.0, min(default, deadline - time.monotonic() - reserve))


async def parse(
    text: str, anchors: RuleExtracted, budget: float | None = None
) -> tuple[ParsedResumeAI, int, list[str]]:
    """Parse with the multi-agent pipeline.

    `budget` (seconds) is an optional overall soft deadline: per-stage timeouts
    shrink so the orchestrator finishes (possibly partial, with warnings) inside
    it instead of being cancelled from outside and losing completed work.
    """
    deadline = time.monotonic() + budget if budget else None
    meter = TokenMeter()
    warnings: list[str] = []
    if len(text) > _MAX_AGENT_CHARS:
        log.warning("agent_text_truncated", original_chars=len(text), cap=_MAX_AGENT_CHARS)
        warnings.append(
            f"Résumé exceeded {_MAX_AGENT_CHARS} characters and was truncated; "
            "content beyond that was not parsed."
        )
        text = text[:_MAX_AGENT_CHARS]

    structure_agent = StructureAgent()
    personal_agent  = PersonalInfoAgent()
    work_agent      = WorkExperienceAgent()
    education_agent = EducationAgent()
    cred_agent      = CredentialsAgent()
    supp_agent      = SupplementalAgent()
    validator_agent = ValidatorAgent()

    # Budget to hold back for the stages AFTER structure (Stage 2 + validation).
    # The fixed 40s suits the large async budget, but on the tighter SYNC budget it
    # would starve the structure call (e.g. only ~6s of a 43s budget), so scale it
    # down proportionally. Capped at 40 so any budget ≥ ~67s (the async path) keeps
    # today's exact behaviour.
    struct_reserve = min(40.0, budget * 0.6) if budget else 40.0

    # ── Stage 1: structure ────────────────────────────────────────────────────
    try:
        structure = await asyncio.wait_for(
            structure_agent.run(text, meter),
            # Hold back enough budget for Stage 2 to do real work.
            _stage_timeout(_STRUCTURE_TIMEOUT, deadline, reserve=struct_reserve),
        )
    except (AIParsingError, TimeoutError) as exc:
        log.warning("structure_failed", error=str(exc))
        warnings.append("Structure mapping failed; work history extracted without per-role verification.")
        structure = None
    roles = structure.roles if structure else []
    log.info("orchestrator_structure", roles=len(roles))

    # ── Stage 2: parallel section extraction (self-bounded) ───────────────────
    # Run the sections as explicit tasks under a soft deadline. Any section still
    # in flight when the deadline hits is cancelled and degrades to its empty
    # default (with a warning) — so one slow section can't time out the whole
    # orchestrator and force the pipeline to throw everything away and re-parse.
    raw = await _run_sections_bounded(
        [
            ("PersonalInfoAgent",  personal_agent.run(text, anchors, meter)),
            ("WorkExperienceAgent", work_agent.run(text, roles, meter)),
            ("EducationAgent",     education_agent.run(text, meter)),
            ("CredentialsAgent",   cred_agent.run(text, meter)),
            ("SupplementalAgent",  supp_agent.run(text, meter)),
        ],
        timeout=_stage_timeout(_STAGE2_TIMEOUT, deadline, reserve=5),
    )

    pres: PersonalResult        = _unwrap(raw[0], PersonalResult(), "PersonalInfoAgent", warnings)
    personal: PersonalInfo      = pres.personal
    if pres.summary_off_topic and personal.summary:
        warnings.append(
            "The professional summary appears unrelated to the candidate's healthcare "
            "background — it may be boilerplate copied from an unrelated résumé. Review before use."
        )
    work: list[ExperienceItem]  = _unwrap(raw[1], [], "WorkExperienceAgent", warnings)
    education: list[EducationItem] = _unwrap(raw[2], [], "EducationAgent", warnings)
    creds: CredentialsResult    = _unwrap(raw[3], CredentialsResult(), "CredentialsAgent", warnings)
    supp: SupplementalResult    = _unwrap(raw[4], SupplementalResult(), "SupplementalAgent", warnings)

    # ── Stage 4: validation / re-extraction ───────────────────────────────────
    # Skipped entirely when the deadline leaves no useful time — the validator is
    # a refinement pass, never worth jeopardising an already-complete extraction.
    if work and roles and (deadline is None or deadline - time.monotonic() > 8):
        try:
            work, vwarn = await asyncio.wait_for(
                validator_agent.run(work, roles, text, meter),
                _stage_timeout(_VALIDATOR_TIMEOUT, deadline, reserve=2),
            )
            warnings.extend(vwarn)
        except (AIParsingError, TimeoutError) as exc:
            log.warning("validator_failed", error=str(exc))

    parsed = ParsedResumeAI(
        personal_info=personal,
        experience=work,
        education=education,
        skills=creds.skills,
        certifications=creds.certifications,
        licenses=creds.licenses,
        projects=supp.projects,
        languages=supp.languages,
        references=supp.references,
        awards=supp.awards,
        publications=supp.publications,
        professional_associations=creds.professional_associations,
    )

    # If literally nothing came back, signal failure so the pipeline can fall back
    # to the single-shot parser rather than returning an empty husk.
    if _is_empty(parsed):
        raise AIParsingError("Multi-agent orchestrator produced no usable data")

    log.info("orchestrator_complete", tokens=meter.total, by_agent=meter.by_agent,
             experience=len(parsed.experience), warnings=len(warnings))
    return parsed, meter.total, warnings


async def parse_light(
    text: str, anchors: RuleExtracted, budget: float
) -> tuple[ParsedResumeAI, int, list[str]]:
    """Section-only extraction: personal / education / credentials / supplemental
    in parallel — NO structure, NO per-role work stage, NO validator.

    The per-role work stage is the slow, cancellation-prone part of the full
    orchestrator; on the tight SYNC budget it gets cancelled and drops the whole
    work section. This lightweight pass deliberately omits it so the high-value
    SEMANTIC sections (headline, secondary phone, education locations, skills,
    certifications, licenses) come back reliably and fast. The caller backfills
    `experience` from the deterministic parser. `experience` is left empty here.
    """
    meter = TokenMeter()
    warnings: list[str] = []
    if len(text) > _MAX_AGENT_CHARS:
        text = text[:_MAX_AGENT_CHARS]

    personal_agent = PersonalInfoAgent()
    education_agent = EducationAgent()
    cred_agent      = CredentialsAgent()
    supp_agent      = SupplementalAgent()

    raw = await _run_sections_bounded(
        [
            ("PersonalInfoAgent", personal_agent.run(text, anchors, meter)),
            ("EducationAgent",    education_agent.run(text, meter)),
            ("CredentialsAgent",  cred_agent.run(text, meter)),
            ("SupplementalAgent", supp_agent.run(text, meter)),
        ],
        timeout=max(5.0, budget),
    )
    pres: PersonalResult        = _unwrap(raw[0], PersonalResult(), "PersonalInfoAgent", warnings)
    education: list[EducationItem] = _unwrap(raw[1], [], "EducationAgent", warnings)
    creds: CredentialsResult    = _unwrap(raw[2], CredentialsResult(), "CredentialsAgent", warnings)
    supp: SupplementalResult    = _unwrap(raw[3], SupplementalResult(), "SupplementalAgent", warnings)

    parsed = ParsedResumeAI(
        personal_info=pres.personal,
        education=education,
        skills=creds.skills,
        certifications=creds.certifications,
        licenses=creds.licenses,
        projects=supp.projects,
        languages=supp.languages,
        references=supp.references,
        awards=supp.awards,
        publications=supp.publications,
        professional_associations=creds.professional_associations,
    )
    log.info("orchestrator_light_complete", tokens=meter.total, education=len(education),
             skills=len(creds.skills), warnings=len(warnings))
    return parsed, meter.total, warnings


def _is_empty(p: ParsedResumeAI) -> bool:
    pi = p.personal_info
    has_contact = any([pi.full_name, pi.email, pi.phone])
    return not any([
        has_contact, p.experience, p.education, p.skills,
        p.certifications, p.licenses,
    ])
