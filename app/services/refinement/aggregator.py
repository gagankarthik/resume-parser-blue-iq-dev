"""Aggregate stored feedback into per-agent correction examples.

Pure, deterministic, no I/O - takes the raw feedback records
(`db.list_feedback_for_company`) and produces, for each agent, the fields most
often corrected plus a bounded set of concrete before/after examples the refiner
can generalise from. Values are truncated so a huge résumé blob can't blow the
refiner's token budget or copy large PII spans into a proposal.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.services.refinement.field_map import agent_for_path

# Bound the signal handed to the refiner. Examples are the expensive input (tokens
# + PII surface); these caps keep a single generation call cheap and focused.
_MAX_VALUE_CHARS      = 200   # truncate any single before/after value
_MAX_EXAMPLES_PER_AGENT = 24  # hard cap on examples fed to one refiner call
_MAX_EXAMPLES_PER_FIELD = 4   # don't let one noisy field crowd out the rest

_INDEX_RE = re.compile(r"\[(\d+)\]")


@dataclass
class Correction:
    """One reviewer edit: a leaf field the parser got wrong, with both values."""

    field: str          # normalised path (list indices collapsed to [])
    before: object       # parser value (truncated)
    after: object        # corrected value (truncated)


@dataclass
class AgentCorrections:
    """Everything the refiner needs for one agent."""

    agent: str
    total: int = 0                                   # corrected leaves seen
    field_counts: Counter = field(default_factory=Counter)  # normalised path -> count
    examples: list[Correction] = field(default_factory=list)

    def top_fields(self, n: int = 10) -> list[tuple[str, int]]:
        return self.field_counts.most_common(n)


def _normalise_path(path: str) -> str:
    """Collapse concrete list indices so ``experience[0].role`` and
    ``experience[2].role`` count as the same recurring mistake."""
    return _INDEX_RE.sub("[]", path)


def _truncate(value: object) -> object:
    """Bound a value's size; keep scalars/None as-is when short."""
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + "…"
    if isinstance(value, (dict, list)):
        text = str(value)
        return text[:_MAX_VALUE_CHARS] + "…" if len(text) > _MAX_VALUE_CHARS else text
    return value


def _get_by_path(obj: object, path: str) -> object:
    """Resolve a dotted/indexed leaf path against a JSON-like object.

    Returns None for any missing key/index - a corrected field is frequently one
    the parser omitted (before=None) or the reviewer deleted (after=None).
    """
    cur = obj
    for token in path.split("."):
        name = token
        indices: list[int] = []
        b = token.find("[")
        if b != -1:
            name = token[:b]
            indices = [int(m) for m in _INDEX_RE.findall(token)]
        if name:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(name)
        for i in indices:
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
    return cur


def aggregate(feedback_records: list[dict]) -> dict[str, AgentCorrections]:
    """Group corrected fields by owning agent.

    Only records flagged `changed` with a non-empty `changed_fields` contribute.
    Returns a mapping of agent name -> AgentCorrections (agents with no signal are
    absent).
    """
    out: dict[str, AgentCorrections] = {}
    per_field_seen: Counter = Counter()  # (agent, field) -> examples kept

    for rec in feedback_records:
        if not rec.get("changed"):
            continue
        changed_fields = rec.get("changed_fields") or []
        original = rec.get("original") or {}
        updated = rec.get("updated") or {}

        for path in changed_fields:
            agent = agent_for_path(path)
            if agent is None:
                continue
            norm = _normalise_path(path)
            bucket = out.setdefault(agent, AgentCorrections(agent=agent))
            bucket.total += 1
            bucket.field_counts[norm] += 1

            # Keep a bounded, representative set of concrete examples: cap per agent
            # and per field so one repetitive field can't monopolise the sample.
            if (
                len(bucket.examples) < _MAX_EXAMPLES_PER_AGENT
                and per_field_seen[(agent, norm)] < _MAX_EXAMPLES_PER_FIELD
            ):
                before = _truncate(_get_by_path(original, path))
                after = _truncate(_get_by_path(updated, path))
                # Skip a non-signal example where both sides resolved to the same
                # value (can happen when only a sibling index actually differed).
                if before != after:
                    bucket.examples.append(Correction(field=norm, before=before, after=after))
                    per_field_seen[(agent, norm)] += 1

    return out
