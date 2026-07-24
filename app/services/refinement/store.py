"""Hot-path applicator for learned agent rules.

Every section agent runs its system prompt through `augment_system()` (called once
inside `BaseAgent._structured_call`). This appends the ACTIVE learned rules for
that agent - the corrections reviewers taught us - so parsing improves without a
code change.

Design constraints (this is on the parse hot path):
  * NEVER fail a parse. Any DynamoDB error -> treat as "no rules" and log.
  * NEVER add a DynamoDB read per LLM call. The active rule set for the scope is
    loaded once and cached in-process for `refinement_cache_ttl_seconds`; a warm
    container serves thousands of agent calls from that single snapshot. A closed
    proposal takes effect within one TTL window (or immediately after an admin
    mutation calls `invalidate()`).
  * Be a no-op unless `refinement_enabled` AND an approved pack exists, so default
    behaviour is byte-identical to before this feature.
"""

from __future__ import annotations

import time

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db import instructions as instructions_db

log = get_logger(__name__)

# scope -> {agent: [rules]}, with a monotonic-clock expiry. Process-global on
# purpose: it should survive across warm-container invocations like the LLM breakers.
_cache: dict[str, dict[str, list[str]]] = {}
_expiry: dict[str, float] = {}


def invalidate(scope: str | None = None) -> None:
    """Drop the cached snapshot so the next parse reloads. Called after an admin
    approve/reject/disable so a change is visible without waiting out the TTL."""
    if scope is None:
        _cache.clear()
        _expiry.clear()
    else:
        _cache.pop(scope, None)
        _expiry.pop(scope, None)


def _load_scope(scope: str) -> dict[str, list[str]]:
    """Active rules for every agent in a scope: {agent: [rules]}. Empty on any error."""
    try:
        items = instructions_db.list_for_scope(scope)
    except Exception as exc:  # noqa: BLE001 - hot path must never raise
        log.warning("refinement_load_failed", scope=scope, error=str(exc))
        return {}
    return {
        it["agent"]: list(it.get("rules") or [])
        for it in items
        if it.get("status") == "active" and it.get("rules")
    }


def _rules_for(agent_name: str) -> list[str]:
    settings = get_settings()
    if not settings.refinement_enabled:
        return []
    scope = settings.refinement_scope
    now = time.monotonic()
    if scope not in _cache or now >= _expiry.get(scope, 0.0):
        _cache[scope] = _load_scope(scope)
        _expiry[scope] = now + settings.refinement_cache_ttl_seconds
    return _cache[scope].get(agent_name, [])


def augment_system(agent_name: str, system: str) -> str:
    """Append the agent's active learned rules to its system prompt.

    Returns `system` unchanged when refinement is disabled or the agent has no
    approved rules - the common case, so overhead is a dict lookup.
    """
    rules = _rules_for(agent_name)
    if not rules:
        return system
    block = "\n".join(f"- {r}" for r in rules)
    return (
        f"{system}\n\n"
        "LEARNED RULES (from reviewer corrections on past parses - apply with the "
        "same authority as the rules above; if one conflicts with an explicit rule "
        "above, the explicit rule wins):\n"
        f"{block}"
    )
