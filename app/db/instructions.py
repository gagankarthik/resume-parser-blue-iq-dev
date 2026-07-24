"""Learned agent-instruction packs (table: agent_instructions).

One item per (scope, agent). `scope` is "global" by default (rules learned across
all companies) but may be a company_id for tenant-specific tuning. Each item holds:

  rules           list[str]  - the ACTIVE learned rules currently applied at parse
  proposed_rules  list[str]  - the latest proposal awaiting review (may be absent)
  status          str        - "active" | "pending" | "disabled" | "none"
  version         int        - bumped on every new proposal
  examples_used   int        - how many correction examples backed the proposal
  updated_at      str        - ISO8601

The refiner writes a proposal (status -> "pending") WITHOUT touching `rules`, so a
generation run never changes live parsing. `approve` promotes the proposal to
`rules` (status -> "active"); only "active" rules are applied on the hot path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key

from app.core.config import get_settings
from app.db._client import _get_dynamodb


def _table():
    settings = get_settings()
    return _get_dynamodb(settings).Table(settings.dynamodb_table_agent_instructions)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def get_instruction(scope: str, agent: str) -> dict | None:
    """One (scope, agent) pack, or None if never proposed."""
    resp = _table().get_item(Key={"scope": scope, "agent": agent})
    return resp.get("Item")


def list_for_scope(scope: str) -> list[dict]:
    """All agent packs for a scope."""
    items: list[dict] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("scope").eq(scope)}
    table = _table()
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def active_rules(scope: str, agent: str) -> list[str]:
    """The rules to apply at parse time - only when the pack is 'active'."""
    item = get_instruction(scope, agent)
    if not item or item.get("status") != "active":
        return []
    return list(item.get("rules") or [])


def save_proposal(scope: str, agent: str, proposed_rules: list[str], examples_used: int) -> dict:
    """Store a new proposal for review WITHOUT changing the active rules.

    Bumps `version`, sets status to 'pending', and preserves any currently-active
    `rules` so live parsing is untouched until the proposal is approved.
    """
    existing = get_instruction(scope, agent) or {}
    item = {
        "scope": scope,
        "agent": agent,
        "rules": list(existing.get("rules") or []),
        "proposed_rules": list(proposed_rules),
        "status": "pending",
        "version": int(existing.get("version", 0)) + 1,
        "examples_used": int(examples_used),
        "updated_at": _now(),
    }
    _table().put_item(Item=item)
    return item


def approve(scope: str, agent: str) -> dict | None:
    """Promote the pending proposal to the active rule set. No-op if none pending."""
    item = get_instruction(scope, agent)
    if not item or not item.get("proposed_rules"):
        return None
    item["rules"] = list(item["proposed_rules"])
    item["proposed_rules"] = []
    item["status"] = "active"
    item["updated_at"] = _now()
    _table().put_item(Item=item)
    return item


def reject(scope: str, agent: str) -> dict | None:
    """Discard the pending proposal, keeping the current active rules (if any)."""
    item = get_instruction(scope, agent)
    if not item:
        return None
    item["proposed_rules"] = []
    item["status"] = "active" if item.get("rules") else "none"
    item["updated_at"] = _now()
    _table().put_item(Item=item)
    return item


def set_disabled(scope: str, agent: str, disabled: bool) -> dict | None:
    """Turn an agent's active rules off (status='disabled') or back on ('active')."""
    item = get_instruction(scope, agent)
    if not item:
        return None
    if disabled:
        item["status"] = "disabled"
    else:
        item["status"] = "active" if item.get("rules") else "none"
    item["updated_at"] = _now()
    _table().put_item(Item=item)
    return item
