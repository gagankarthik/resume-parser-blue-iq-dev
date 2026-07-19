"""
Multi-agent orchestrator tests.

LLM calls are mocked at the `_structured_call` boundary so the REAL agent and
orchestrator logic runs - structure-driven per-role extraction, validator bullet
reconciliation, per-section failure isolation, and final assembly.
"""

import pytest

from app.core.exceptions import AIParsingError
from app.models.schemas import ExperienceItem, PersonalInfo
from app.services.parsing import orchestrator
from app.services.parsing.agents.base import BaseAgent, TokenMeter
from app.services.parsing.agents.schemas import (
    CredentialsResult,
    EducationResult,
    PersonalResult,
    ResumeStructure,
    RoleBoundary,
    SupplementalResult,
    WorkResult,
)
from app.services.parsing.agents.validator import ValidatorAgent
from app.services.parsing.agents.work import WorkExperienceAgent
from app.services.parsing.rule_parser import RuleExtracted


def _canned(overrides=None):
    """Return a fake _structured_call that answers by requested response model."""
    overrides = overrides or {}

    async def fake(self, system, user, response_format, meter, *, max_tokens=None):
        meter.add(self.name, 1)
        name = response_format.__name__
        if name in overrides:
            return overrides[name](system, user)
        return {
            "ResumeStructure": lambda: ResumeStructure(
                roles=[RoleBoundary(company="Mercy Hospital", title="RN - ICU", bullet_count=0)]
            ),
            "PersonalResult": lambda: PersonalResult(
                personal=PersonalInfo(full_name="Jane Smith", email="jane@example.com")
            ),
            "ExperienceItem": lambda: ExperienceItem(company="Mercy Hospital", role="RN - ICU"),
            "EducationResult": lambda: EducationResult(education=[]),
            "CredentialsResult": lambda: CredentialsResult(skills=["ICU"], certifications=[], licenses=[]),
            "SupplementalResult": lambda: SupplementalResult(),
            "WorkResult": lambda: WorkResult(work_experience=[]),
        }[name]()

    return fake


async def test_orchestrator_assembles_all_sections(monkeypatch):
    monkeypatch.setattr(BaseAgent, "_structured_call", _canned())
    parsed, tokens, warnings = await orchestrator.parse("résumé text", RuleExtracted())

    assert parsed.personal_info.full_name == "Jane Smith"
    assert parsed.skills == ["ICU"]
    assert len(parsed.experience) == 1
    assert parsed.experience[0].company == "Mercy Hospital"
    assert tokens > 0
    assert warnings == []


async def test_orchestrator_flags_off_topic_summary(monkeypatch):
    def off_topic(s, u):
        return PersonalResult(
            personal=PersonalInfo(full_name="Jane Smith", summary="Award-winning dance instructor."),
            summary_off_topic=True,
        )

    monkeypatch.setattr(BaseAgent, "_structured_call", _canned({"PersonalResult": off_topic}))
    parsed, _tokens, warnings = await orchestrator.parse("text", RuleExtracted())

    # Summary copied verbatim, but a review warning is surfaced.
    assert parsed.personal_info.summary == "Award-winning dance instructor."
    assert any("unrelated" in w for w in warnings)


async def test_orchestrator_isolates_one_failing_section(monkeypatch):
    def boom(system, user):
        raise AIParsingError("PersonalInfoAgent down")

    monkeypatch.setattr(BaseAgent, "_structured_call", _canned({"PersonalResult": boom}))
    parsed, _tokens, warnings = await orchestrator.parse("text", RuleExtracted())

    # Personal section degraded to empty, but the rest still came through.
    assert parsed.personal_info.full_name is None
    assert parsed.skills == ["ICU"]
    assert any("PersonalInfoAgent" in w for w in warnings)


async def test_orchestrator_raises_when_everything_empty(monkeypatch):
    empty = {
        "ResumeStructure": lambda s, u: ResumeStructure(roles=[]),
        "PersonalResult": lambda s, u: PersonalResult(personal=PersonalInfo()),
        "EducationResult": lambda s, u: EducationResult(education=[]),
        "CredentialsResult": lambda s, u: CredentialsResult(),
        "SupplementalResult": lambda s, u: SupplementalResult(),
        "WorkResult": lambda s, u: WorkResult(work_experience=[]),
    }
    monkeypatch.setattr(BaseAgent, "_structured_call", _canned(empty))
    with pytest.raises(AIParsingError):
        await orchestrator.parse("text", RuleExtracted())


async def test_orchestrator_recovers_experience_when_work_stage_fails(monkeypatch):
    """If the whole work stage is cancelled/raises (e.g. a per-role call looped to the
    token ceiling, or a dense CV overran the deadline) but the structure map found
    roles, experience must be recovered as employer stubs - never dropped to empty
    with a bare warning (the "no experience at all" regression)."""
    monkeypatch.setattr(BaseAgent, "_structured_call", _canned())

    async def work_dies(self, text, roles, meter):
        raise TimeoutError("work stage cancelled")

    monkeypatch.setattr(WorkExperienceAgent, "run", work_dies)
    # No budget/deadline is passed, so the validator's re-extraction pass can run and
    # is harmless here; the key assertion is that experience is not empty.
    parsed, _tokens, warnings = await orchestrator.parse("text with jobs", RuleExtracted())

    assert len(parsed.experience) == 1                      # recovered, not dropped
    assert parsed.experience[0].company == "Mercy Hospital"  # identity kept from structure map
    assert parsed.experience[0].role == "RN - ICU"
    assert not any("WorkExperienceAgent failed" in w for w in warnings)  # failure warning replaced
    assert any("could not be fully extracted" in w for w in warnings)


async def test_orchestrator_carries_professional_associations(monkeypatch):
    def with_assocs(s, u):
        return CredentialsResult(
            skills=["ICU"],
            professional_associations=[
                "Sigma Theta Tau International Honor Society of Nursing Member",
                "Sepsis Clinical Services Committee",
            ],
        )

    monkeypatch.setattr(BaseAgent, "_structured_call", _canned({"CredentialsResult": with_assocs}))
    parsed, _tokens, _warnings = await orchestrator.parse("text", RuleExtracted())

    assert len(parsed.professional_associations) == 2
    assert "Sepsis Clinical Services Committee" in parsed.professional_associations


# -- WorkExperienceAgent: per-role extraction + structure-map seeding ---------

async def test_work_agent_extracts_one_entry_per_role_and_seeds_identity(monkeypatch):
    agent = WorkExperienceAgent()

    async def fake(system, user, response_format, meter, *, max_tokens=None):
        # Model returns a blank role/agency to prove the structure map seeds them.
        return ExperienceItem(company="Facility", role="", description=["b1", "b2"])

    monkeypatch.setattr(agent, "_structured_call", fake)
    roles = [
        RoleBoundary(company="VT Psychiatric", title="Travel RN", profession="RN",
                     agency_name="Supplemental Healthcare", is_travel_assignment=True, bullet_count=2),
        RoleBoundary(company="Berlin Health", title="Travel RN", profession="RN",
                     agency_name="Supplemental Healthcare", is_travel_assignment=True, bullet_count=2),
    ]
    out = await agent.run("text", roles, TokenMeter())

    assert len(out) == 2                       # one entry per facility — not flattened
    assert out[0].role == "Travel RN"          # seeded from the structure map, not "Unknown"
    assert out[0].agency_name == "Supplemental Healthcare"
    assert out[0].profession == "RN"


async def test_work_agent_stubs_failed_role_to_keep_alignment(monkeypatch):
    """A role whose extraction keeps failing is backfilled from the structure map
    (not dropped), so the output stays 1:1 with `roles` - guarding the validator's
    positional pairing against the label-shift / duplicate-employer regression."""
    agent = WorkExperienceAgent()

    async def fake(system, user, response_format, meter, *, max_tokens=None):
        # The Woodlands role never extracts; Amedisys always does.
        if "Woodlands" in user:
            raise RuntimeError("transient model error")
        return ExperienceItem(company="Amedisys", role="Psychiatric RN", description=["b1"])

    monkeypatch.setattr(agent, "_structured_call", fake)
    roles = [
        RoleBoundary(company="Woodlands Assisted Living", title="Supervisory RN",
                     start_date="03/2013", end_date="07/2015", bullet_count=0),
        RoleBoundary(company="Amedisys", title="Psychiatric RN", bullet_count=0),
    ]
    out = await agent.run("text", roles, TokenMeter())

    assert len(out) == 2                                 # neither employer dropped
    assert out[0].company == "Woodlands Assisted Living"  # stub keeps its own slot
    assert out[0].role == "Supervisory RN"
    assert out[0].start_date == "03/2013"
    assert out[1].company == "Amedisys"                  # no shift onto the next role


async def test_work_agent_restores_facility_when_agency_displaces_company(monkeypatch):
    """If the model returns the staffing agency as `company`, the structure map's
    facility name must win (regression: every travel site came back as
    company='Supplemental Healthcare', real facility lost into description)."""
    agent = WorkExperienceAgent()

    async def fake(system, user, response_format, meter, *, max_tokens=None):
        return ExperienceItem(
            company="Supplemental Healthcare", role="Travel RN",
            agency_name="Supplemental Healthcare", description=["b1"],
        )

    monkeypatch.setattr(agent, "_structured_call", fake)
    roles = [RoleBoundary(company="Brattleboro Memorial Hospital", title="Travel RN",
                          agency_name="Supplemental Healthcare",
                          is_travel_assignment=True, bullet_count=1)]
    out = await agent.run("text", roles, TokenMeter())

    assert out[0].company == "Brattleboro Memorial Hospital"
    assert out[0].agency_name == "Supplemental Healthcare"


async def test_work_agent_clears_agency_on_standalone_employer(monkeypatch):
    """An employer with its own title/dates (not under a travel umbrella) must not
    inherit a neighbouring agency (the Woodlands Assisted Living mislabel)."""
    agent = WorkExperienceAgent()

    async def fake(system, user, response_format, meter, *, max_tokens=None):
        return ExperienceItem(
            company="Woodlands Assisted Living", role="Supervisory RN",
            agency_name="Supplemental Healthcare",  # wrongly inherited by the model
            employer_phone="304-287-2120", description=["b1"],
        )

    monkeypatch.setattr(agent, "_structured_call", fake)
    roles = [RoleBoundary(company="Woodlands Assisted Living", title="Supervisory RN",
                          start_date="03/2013", end_date="07/2015", bullet_count=1)]
    out = await agent.run("text", roles, TokenMeter())

    assert out[0].agency_name is None
    assert out[0].company == "Woodlands Assisted Living"
    assert out[0].employer_phone == "304-287-2120"


# -- ValidatorAgent: bullet-count reconciliation ------------------------------

async def test_validator_reextracts_and_fixes_mismatch(monkeypatch):
    agent = ValidatorAgent()

    async def fake_reextract(text, role, meter):
        return ExperienceItem(company="A", role="RN", description=["b1", "b2", "b3"])

    monkeypatch.setattr(agent, "_reextract", fake_reextract)
    work = [ExperienceItem(company="A", role="RN", description=["b1"])]   # 1 of 3
    roles = [RoleBoundary(company="A", bullet_count=3)]

    out, warnings = await agent.run(work, roles, "text", TokenMeter())
    assert len(out[0].description) == 3
    assert warnings == []


async def test_validator_keeps_closer_extraction_and_warns_on_residual(monkeypatch):
    agent = ValidatorAgent()

    async def fake_reextract(text, role, meter):
        return ExperienceItem(company="A", role="RN", description=["b1", "b2"])  # 2, still != 3

    monkeypatch.setattr(agent, "_reextract", fake_reextract)
    work = [ExperienceItem(company="A", role="RN", description=["b1"])]   # 1 (further from 3)
    roles = [RoleBoundary(company="A", bullet_count=3)]

    out, warnings = await agent.run(work, roles, "text", TokenMeter())
    assert len(out[0].description) == 2        # kept the closer of the two
    assert warnings and "review" in warnings[0]


async def test_validator_noop_when_counts_match(monkeypatch):
    agent = ValidatorAgent()
    called = False

    async def fake_reextract(text, role, meter):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(agent, "_reextract", fake_reextract)
    work = [ExperienceItem(company="A", role="RN", description=["b1", "b2"])]
    roles = [RoleBoundary(company="A", bullet_count=2)]

    out, warnings = await agent.run(work, roles, "text", TokenMeter())
    assert not called
    assert warnings == []
