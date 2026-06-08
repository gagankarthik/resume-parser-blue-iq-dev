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
from app.services.parsing.agents.schemas import CredentialsResult, SupplementalResult
from app.services.parsing.agents.structure import StructureAgent
from app.services.parsing.agents.supplemental import SupplementalAgent
from app.services.parsing.agents.validator import ValidatorAgent
from app.services.parsing.agents.work import WorkExperienceAgent
from app.services.parsing.rule_parser import RuleExtracted

log = get_logger(__name__)

# Each agent receives the full résumé text, and the WorkAgent sends it once PER
# role. Cap the text so a pathologically long résumé can't blow the model context
# window or run up unbounded token cost across the fan-out. Generous enough to
# hold a 25-year multi-page CV.
_MAX_AGENT_CHARS = 30_000


def _unwrap[T](result: object, default: T, agent: str, warnings: list[str]) -> T:
    if isinstance(result, Exception):
        log.warning("agent_section_failed", agent=agent, error=str(result))
        warnings.append(f"{agent} failed; that section may be incomplete.")
        return default
    return result  # type: ignore[return-value]


async def parse(text: str, anchors: RuleExtracted) -> tuple[ParsedResumeAI, int, list[str]]:
    meter = TokenMeter()
    warnings: list[str] = []
    text = text[:_MAX_AGENT_CHARS]

    structure_agent = StructureAgent()
    personal_agent  = PersonalInfoAgent()
    work_agent      = WorkExperienceAgent()
    education_agent = EducationAgent()
    cred_agent      = CredentialsAgent()
    supp_agent      = SupplementalAgent()
    validator_agent = ValidatorAgent()

    # ── Stage 1: structure ────────────────────────────────────────────────────
    try:
        structure = await structure_agent.run(text, meter)
    except AIParsingError as exc:
        log.warning("structure_failed", error=str(exc))
        warnings.append("Structure mapping failed; work history extracted without per-role verification.")
        structure = None
    roles = structure.roles if structure else []
    log.info("orchestrator_structure", roles=len(roles))

    # ── Stage 2: parallel section extraction ──────────────────────────────────
    results = await asyncio.gather(
        personal_agent.run(text, anchors, meter),
        work_agent.run(text, roles, meter),
        education_agent.run(text, meter),
        cred_agent.run(text, meter),
        supp_agent.run(text, meter),
        return_exceptions=True,
    )

    personal: PersonalInfo      = _unwrap(results[0], PersonalInfo(), "PersonalInfoAgent", warnings)
    work: list[ExperienceItem]  = _unwrap(results[1], [], "WorkExperienceAgent", warnings)
    education: list[EducationItem] = _unwrap(results[2], [], "EducationAgent", warnings)
    creds: CredentialsResult    = _unwrap(results[3], CredentialsResult(), "CredentialsAgent", warnings)
    supp: SupplementalResult    = _unwrap(results[4], SupplementalResult(), "SupplementalAgent", warnings)

    # ── Stage 4: validation / re-extraction ───────────────────────────────────
    if work and roles:
        try:
            work, vwarn = await validator_agent.run(work, roles, text, meter)
            warnings.extend(vwarn)
        except AIParsingError as exc:
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
    )

    # If literally nothing came back, signal failure so the pipeline can fall back
    # to the single-shot parser rather than returning an empty husk.
    if _is_empty(parsed):
        raise AIParsingError("Multi-agent orchestrator produced no usable data")

    log.info("orchestrator_complete", tokens=meter.total, by_agent=meter.by_agent,
             experience=len(parsed.experience), warnings=len(warnings))
    return parsed, meter.total, warnings


def _is_empty(p: ParsedResumeAI) -> bool:
    pi = p.personal_info
    has_contact = any([pi.full_name, pi.email, pi.phone])
    return not any([
        has_contact, p.experience, p.education, p.skills,
        p.certifications, p.licenses,
    ])
