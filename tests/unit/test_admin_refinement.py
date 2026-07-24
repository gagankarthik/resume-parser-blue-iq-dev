"""Admin refinement endpoints: generate proposals, review, approve/reject."""

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints import admin_refinement as ar
from app.services.refinement.refiner import AgentRefinement


async def test_generate_saves_pending_proposals(monkeypatch):
    saved: list = []
    approved: list = []

    monkeypatch.setattr(ar.db, "list_feedback_for_company", lambda cid, since: [{"changed": True}])
    monkeypatch.setattr(ar.db, "save_proposal",
                        lambda scope, agent, rules, n: saved.append((scope, agent, rules, n)))
    monkeypatch.setattr(ar.db, "approve", lambda scope, agent: approved.append(agent))

    async def fake_generate(feedback, min_examples=None, max_rules=None):
        return [AgentRefinement(
            agent="PersonalInfoAgent",
            proposed_rules=["Strip credential suffixes from full_name."],
            examples_used=7,
            top_fields=[("personal_info.full_name", 7)],
        )]

    monkeypatch.setattr(ar.refiner, "generate_refinements", fake_generate)

    out = await ar.generate(ar.GenerateRequest(company_id="acme", days=30, auto_apply=False))

    assert out["agents_updated"] == 1
    assert out["auto_applied"] is False
    assert out["proposals"][0]["status"] == "pending"
    assert saved and saved[0][1] == "PersonalInfoAgent"
    assert approved == []  # not auto-approved


async def test_generate_auto_apply_approves_and_invalidates(monkeypatch):
    approved: list = []
    invalidated: list = []

    monkeypatch.setattr(ar.db, "list_feedback_for_company", lambda cid, since: [])
    monkeypatch.setattr(ar.db, "save_proposal", lambda *a, **k: None)
    monkeypatch.setattr(ar.db, "approve", lambda scope, agent: approved.append(agent))
    monkeypatch.setattr(ar.refinement_store, "invalidate", lambda scope: invalidated.append(scope))

    async def fake_generate(feedback, min_examples=None, max_rules=None):
        return [AgentRefinement("WorkExperienceAgent", ["Never put the agency in company."], 5, [])]

    monkeypatch.setattr(ar.refiner, "generate_refinements", fake_generate)

    out = await ar.generate(ar.GenerateRequest(company_id="acme", auto_apply=True))
    assert out["auto_applied"] is True
    assert approved == ["WorkExperienceAgent"]
    assert invalidated  # cache cleared so the rules take effect


async def test_generate_across_all_companies_when_no_company_id(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ar.db, "list_companies",
                        lambda: [{"company_id": "a"}, {"company_id": "b"}])
    monkeypatch.setattr(ar.db, "list_feedback_for_company",
                        lambda cid, since: seen.append(cid) or [])
    monkeypatch.setattr(ar.db, "save_proposal", lambda *a, **k: None)

    async def fake_generate(feedback, min_examples=None, max_rules=None):
        return []

    monkeypatch.setattr(ar.refiner, "generate_refinements", fake_generate)
    await ar.generate(ar.GenerateRequest(company_id=None))
    assert seen == ["a", "b"]


async def test_approve_unknown_agent_is_404():
    with pytest.raises(HTTPException) as ei:
        await ar.approve("NotAnAgent")
    assert ei.value.status_code == 404


async def test_approve_without_pending_is_404(monkeypatch):
    monkeypatch.setattr(ar.db, "approve", lambda scope, agent: None)
    with pytest.raises(HTTPException) as ei:
        await ar.approve("PersonalInfoAgent")
    assert ei.value.status_code == 404


async def test_approve_invalidates_cache(monkeypatch):
    invalidated: list = []
    monkeypatch.setattr(ar.db, "approve",
                        lambda scope, agent: {"status": "active", "rules": ["r"]})
    monkeypatch.setattr(ar.refinement_store, "invalidate", lambda scope: invalidated.append(scope))
    out = await ar.approve("PersonalInfoAgent")
    assert out["status"] == "active"
    assert invalidated
