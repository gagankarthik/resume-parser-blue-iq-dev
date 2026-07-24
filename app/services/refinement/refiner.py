"""The LLM refiner: turn per-agent correction examples into prompt rules.

For each agent that has enough signal, one structured-output call asks the model to
generalise the reviewer corrections into a short list of concise, imperative rules
that would have prevented those mistakes - the "agent-based instruction refinement"
step. Output is validated (`RuleProposal`) and capped, then stored as a PENDING
proposal for review (see `app.db.instructions`); nothing is applied until approved.

This runs OFF the parse hot path (admin-triggered), so it uses `structured_parse`
directly with its own throwaway token meter rather than the pipeline's BaseAgent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.llm.client import structured_parse
from app.services.refinement.aggregator import AgentCorrections, aggregate

log = get_logger(__name__)

# One-line description of what each agent extracts, so the refiner writes rules that
# fit the agent's job. Keyed by the agent `name`.
_AGENT_ROLE: dict[str, str] = {
    "PersonalInfoAgent":  "candidate contact + identity (full_name, email, phone, location, links, summary)",
    "WorkExperienceAgent": "each work-history role (company/facility, title, dates, location, duty bullets, specialties, agency)",
    "EducationAgent":     "education entries (institution, degree, field, years, gpa)",
    "CredentialsAgent":   "skills, certifications, licenses, and professional associations",
    "SupplementalAgent":  "projects, languages, references, awards, publications",
}


class RuleProposal(BaseModel):
    """Structured output from the refiner for a single agent."""

    rules: list[str] = Field(
        default_factory=list,
        description="Concise, imperative extraction rules that would prevent the "
                    "observed corrections. Each ≤ 200 chars, self-contained, no examples "
                    "of specific candidates.",
    )


@dataclass
class AgentRefinement:
    """Result for one agent: the proposed rules + the signal they came from."""

    agent: str
    proposed_rules: list[str]
    examples_used: int
    top_fields: list[tuple[str, int]]


_SYSTEM = """You improve the prompt of ONE extraction agent in a healthcare résumé parser.

You are given the fields that human reviewers most often had to CORRECT in this
agent's output, with real before/after pairs (before = what the agent produced,
after = the reviewer's correction). Generalise them into a short list of RULES that,
if added to the agent's instructions, would prevent those mistakes on future résumés.

Requirements for each rule:
- Imperative and specific ("Do X", "Never Y"), ≤ 200 characters.
- Generalise the PATTERN - never reference a specific candidate, employer, or value
  from the examples (no names, emails, phone numbers).
- Actionable from résumé text alone; do not invent policy the examples don't support.
- Do NOT restate rules the agent surely already follows unless a correction shows it
  is being violated.
Return only rules clearly supported by the corrections. Fewer, higher-signal rules
are better than many weak ones. If the corrections show no generalisable pattern,
return an empty list."""


def _format_corrections(ac: AgentCorrections, max_rules: int) -> str:
    lines: list[str] = []
    lines.append(f"Agent: {ac.agent} — extracts {_AGENT_ROLE.get(ac.agent, ac.agent)}.")
    lines.append(f"Total corrected fields observed: {ac.total}.")
    lines.append("")
    lines.append("Most-corrected fields (path — count):")
    for path, count in ac.top_fields(10):
        lines.append(f"  {path} — {count}")
    lines.append("")
    lines.append("Correction examples (before -> after):")
    for ex in ac.examples:
        lines.append(f"  [{ex.field}] {ex.before!r} -> {ex.after!r}")
    lines.append("")
    lines.append(f"Propose at most {max_rules} rules.")
    return "\n".join(lines)


async def refine_agent(ac: AgentCorrections, *, max_rules: int) -> list[str]:
    """One refiner call for one agent. Returns proposed rules (possibly empty)."""
    settings = get_settings()
    user = _format_corrections(ac, max_rules)
    result = await structured_parse(
        system=_SYSTEM,
        user=user,
        response_format=RuleProposal,
        model=settings.openai_model,           # the reasoning-heavier primary model
        max_tokens=1024,
        label=f"refiner:{ac.agent}",
    )
    proposal: RuleProposal = result.parsed  # type: ignore[assignment]
    # Defensive cap + de-dup + strip; the schema is advisory, enforce hard limits here.
    seen: set[str] = set()
    rules: list[str] = []
    for raw in proposal.rules:
        rule = (raw or "").strip()
        if rule and rule.lower() not in seen:
            seen.add(rule.lower())
            rules.append(rule[:200])
        if len(rules) >= max_rules:
            break
    return rules


async def generate_refinements(
    feedback_records: list[dict],
    *,
    min_examples: int | None = None,
    max_rules: int | None = None,
) -> list[AgentRefinement]:
    """Aggregate feedback and run the refiner for every agent with enough signal.

    `min_examples` - an agent needs at least this many concrete correction examples
    before it is worth a refiner call (guards against overfitting to one-off edits).
    Agents below the threshold, or that yield no rules, are omitted from the result.
    """
    settings = get_settings()
    min_examples = settings.refinement_min_examples if min_examples is None else min_examples
    max_rules = settings.refinement_max_rules_per_agent if max_rules is None else max_rules

    by_agent = aggregate(feedback_records)
    eligible = [ac for ac in by_agent.values() if len(ac.examples) >= min_examples]
    if not eligible:
        log.info("refinement_no_eligible_agents",
                  agents=len(by_agent), min_examples=min_examples)
        return []

    async def _one(ac: AgentCorrections) -> AgentRefinement | None:
        try:
            rules = await refine_agent(ac, max_rules=max_rules)
        except Exception as exc:  # noqa: BLE001 - one agent failing must not sink the run
            log.warning("refiner_agent_failed", agent=ac.agent, error=str(exc))
            return None
        if not rules:
            return None
        return AgentRefinement(
            agent=ac.agent,
            proposed_rules=rules,
            examples_used=len(ac.examples),
            top_fields=ac.top_fields(10),
        )

    results = await asyncio.gather(*[_one(ac) for ac in eligible])
    return [r for r in results if r is not None]
