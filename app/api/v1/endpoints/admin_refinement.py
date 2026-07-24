"""
Admin endpoints for the feedback-driven instruction-refinement loop
(gated by X-Admin-Token, same as the rest of /admin).

  POST   /api/v1/admin/refinement/generate            aggregate feedback -> propose rules
  GET    /api/v1/admin/refinement                      list current packs (per scope)
  POST   /api/v1/admin/refinement/{agent}/approve      apply a pending proposal
  POST   /api/v1/admin/refinement/{agent}/reject       discard a pending proposal
  POST   /api/v1/admin/refinement/{agent}/disable      turn an agent's rules on/off

Generation reads stored feedback (reviewer corrections), runs the LLM refiner, and
stores each agent's proposal as PENDING. Nothing affects parsing until a proposal is
approved (or `refinement_auto_apply` is set). Mutations invalidate the parse-side
cache so an approval takes effect on the next parse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.dependencies import require_admin
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.refinement import refiner
from app.services.refinement import store as refinement_store
from app.services.refinement.field_map import AGENT_NAMES

router = APIRouter(prefix="/admin/refinement", dependencies=[Depends(require_admin)], tags=["Admin"])
log = get_logger(__name__)


class GenerateRequest(BaseModel):
    # Which company's feedback to learn from. Omit to aggregate across ALL companies.
    company_id: str | None = None
    # Look-back window over feedback records.
    days: int = Field(default=90, ge=1, le=365)
    # Override the config thresholds for this run (optional).
    min_examples: int | None = Field(default=None, ge=1)
    max_rules: int | None = Field(default=None, ge=1, le=20)
    # Auto-approve proposals from this run (defaults to the config setting).
    auto_apply: bool | None = None


def _collect_feedback(company_id: str | None, since_iso: str) -> list[dict]:
    """Feedback records to learn from - one company or all of them."""
    if company_id:
        return db.list_feedback_for_company(company_id, since_iso)
    records: list[dict] = []
    for company in db.list_companies():
        cid = company.get("company_id")
        if cid:
            records.extend(db.list_feedback_for_company(cid, since_iso))
    return records


@router.post("/generate", summary="Generate instruction proposals from feedback")
async def generate(payload: GenerateRequest) -> dict:
    settings = get_settings()
    scope = settings.refinement_scope
    auto_apply = settings.refinement_auto_apply if payload.auto_apply is None else payload.auto_apply

    since_iso = (datetime.now(UTC) - timedelta(days=payload.days)).isoformat()
    feedback = _collect_feedback(payload.company_id, since_iso)

    refinements = await refiner.generate_refinements(
        feedback,
        min_examples=payload.min_examples,
        max_rules=payload.max_rules,
    )

    results = []
    for r in refinements:
        db.save_proposal(scope, r.agent, r.proposed_rules, r.examples_used)
        if auto_apply:
            db.approve(scope, r.agent)
        results.append({
            "agent": r.agent,
            "proposed_rules": r.proposed_rules,
            "examples_used": r.examples_used,
            "top_fields": [{"field": f, "count": c} for f, c in r.top_fields],
            "status": "active" if auto_apply else "pending",
        })

    if auto_apply and results:
        refinement_store.invalidate(scope)

    log.info("refinement_generated", scope=scope, company_id=payload.company_id,
             feedback_records=len(feedback), agents=len(results), auto_apply=auto_apply)
    return {
        "scope": scope,
        "feedback_records": len(feedback),
        "agents_updated": len(results),
        "auto_applied": auto_apply,
        "proposals": results,
    }


@router.get("", summary="List instruction packs for the active scope")
async def list_packs(scope: str | None = None) -> dict:
    scope = scope or get_settings().refinement_scope
    packs = db.list_for_scope(scope)
    return {
        "scope": scope,
        "packs": [
            {
                "agent": p.get("agent"),
                "status": p.get("status"),
                "version": int(p.get("version", 0) or 0),
                "rules": list(p.get("rules") or []),
                "proposed_rules": list(p.get("proposed_rules") or []),
                "examples_used": int(p.get("examples_used", 0) or 0),
                "updated_at": p.get("updated_at", ""),
            }
            for p in sorted(packs, key=lambda x: str(x.get("agent", "")))
        ],
    }


def _check_agent(agent: str) -> None:
    if agent not in AGENT_NAMES:
        raise api_error(
            404, ErrorCode.INVALID_REQUEST,
            f"Unknown agent '{agent}'. Valid agents: {sorted(AGENT_NAMES)}",
        )


@router.post("/{agent}/approve", summary="Approve a pending proposal (apply its rules)")
async def approve(agent: str, scope: str | None = None) -> dict:
    _check_agent(agent)
    scope = scope or get_settings().refinement_scope
    item = db.approve(scope, agent)
    if not item:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "No pending proposal to approve for this agent")
    refinement_store.invalidate(scope)
    log.info("refinement_approved", scope=scope, agent=agent, rules=len(item.get("rules") or []))
    return {"scope": scope, "agent": agent, "status": item.get("status"), "rules": item.get("rules", [])}


@router.post("/{agent}/reject", summary="Reject a pending proposal (discard it)")
async def reject(agent: str, scope: str | None = None) -> dict:
    _check_agent(agent)
    scope = scope or get_settings().refinement_scope
    item = db.reject(scope, agent)
    if not item:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "No pack found for this agent")
    refinement_store.invalidate(scope)
    return {"scope": scope, "agent": agent, "status": item.get("status")}


class DisableRequest(BaseModel):
    disabled: bool = True


@router.post("/{agent}/disable", summary="Turn an agent's active rules off or back on")
async def disable(agent: str, payload: DisableRequest, scope: str | None = None) -> dict:
    _check_agent(agent)
    scope = scope or get_settings().refinement_scope
    item = db.set_disabled(scope, agent, payload.disabled)
    if not item:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "No pack found for this agent")
    refinement_store.invalidate(scope)
    return {"scope": scope, "agent": agent, "status": item.get("status")}
