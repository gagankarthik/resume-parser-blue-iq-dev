"""
Structured-output response schemas for the multi-agent parser.

Every agent returns one of these models via OpenAI structured outputs
(`beta.chat.completions.parse`), so the schema guarantee we rely on for the
single-shot parser is preserved per-agent. The section payloads reuse the SAME
item models as the single-shot `ParsedResumeAI`, so the orchestrator can drop
them straight into the final document with identical validation/sanitisation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.schemas import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LicenseItem,
    PersonalInfo,
    ProjectItem,
    ReferenceItem,
    _coerce_list,
)

# ── Stage 2: personal section ─────────────────────────────────────────────────


class PersonalResult(BaseModel):
    """PersonalInfo plus a review flag for an off-topic summary.

    The summary is always copied verbatim into `personal.summary` (we never
    fabricate or rewrite it). `summary_off_topic` is a separate signal so the
    orchestrator can warn a reviewer when the summary is boilerplate that clearly
    belongs to a different field — without polluting the returned PersonalInfo.
    """

    personal:          PersonalInfo = Field(default_factory=PersonalInfo)
    summary_off_topic: bool         = Field(
        False,
        description="True ONLY if the professional summary is clearly unrelated to "
                    "the candidate's healthcare profession/work history (e.g. copied "
                    "boilerplate from an unrelated occupation). Still copy the summary "
                    "verbatim into personal.summary regardless of this flag.",
    )

# ── Stage 1: structure map ────────────────────────────────────────────────────


class RoleBoundary(BaseModel):
    """One work-history role located by the StructureAgent.

    This is a *map*, not the full extraction — it pins down how many roles exist
    and how many responsibility bullets each one has, so the WorkAgent extracts
    each role independently and the ValidatorAgent can detect dropped bullets.
    Travel/agency umbrella roles are decomposed here: each facility assignment is
    its own RoleBoundary carrying the umbrella's profession + agency_name.
    """

    company:              str        = Field(..., description="Employer/facility name for this role")
    title:                str | None = Field(None, description="Job title/role as written (e.g. 'Travel RN - ICU'), null if none stated")
    profession:           str | None = Field(None, description="Credential for this role as written (e.g. 'RN', 'RT'); do NOT expand")
    start_date:           str | None = Field(None, description="Start date as written")
    end_date:             str | None = Field(None, description="End date as written, or 'Present'")
    agency_name:          str | None = Field(None, description="Staffing/travel agency, if this is an agency assignment")
    is_travel_assignment: bool       = Field(False, description="True if this is one facility under a travel/agency umbrella role")
    bullet_count:         int        = Field(0, ge=0, description="EXACT number of responsibility/duty bullets or duty sentences for this role in the source text. 0 only if the role truly lists no duties.")

    @field_validator("company", mode="before")
    @classmethod
    def _req_company(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) and v.strip() else "Unknown"

    @field_validator("bullet_count", mode="before")
    @classmethod
    def _coerce_count(cls, v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            return 0
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0


class ResumeStructure(BaseModel):
    roles: list[RoleBoundary] = Field(default_factory=list, description="Every work-history role, most recent first, travel assignments decomposed")

    @field_validator("roles", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> list:
        return _coerce_list(v)


# ── Stage 2: section results ──────────────────────────────────────────────────


class WorkResult(BaseModel):
    """Used only by the full-document fallback when no structure map is available."""

    work_experience: list[ExperienceItem] = Field(default_factory=list)

    @field_validator("work_experience", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> list:
        return _coerce_list(v)


class EducationResult(BaseModel):
    education: list[EducationItem] = Field(default_factory=list)

    @field_validator("education", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> list:
        return _coerce_list(v)


class CredentialsResult(BaseModel):
    """Skills + certifications + state licenses + professional associations —
    kept in one agent because the LLM must decide, per item, whether it is a
    skill, a certification, a state licence, or a membership/committee, and
    seeing them together prevents double-classification. (Résumés often mix all
    of these under one heading like "Professional Associations/Certifications/
    Licenses/Collaboratives".)"""

    skills:         list[str]                = Field(default_factory=list)
    certifications: list[CertificationItem]  = Field(default_factory=list)
    licenses:       list[LicenseItem]        = Field(default_factory=list)
    professional_associations: list[str]     = Field(default_factory=list)

    @field_validator("certifications", "licenses", mode="before")
    @classmethod
    def _coerce_objs(cls, v: object) -> list:
        return _coerce_list(v)

    @field_validator("skills", "professional_associations", mode="before")
    @classmethod
    def _coerce_strs(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [str(i).strip() for i in items if i and str(i).strip()]


class SupplementalResult(BaseModel):
    projects:     list[ProjectItem]   = Field(default_factory=list)
    languages:    list[str]           = Field(default_factory=list)
    references:   list[ReferenceItem] = Field(default_factory=list)
    awards:       list[str]           = Field(default_factory=list)
    publications: list[str]           = Field(default_factory=list)

    @field_validator("projects", "references", mode="before")
    @classmethod
    def _coerce_objs(cls, v: object) -> list:
        return _coerce_list(v)

    @field_validator("languages", "awards", "publications", mode="before")
    @classmethod
    def _coerce_strs(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [str(i).strip() for i in items if i and str(i).strip()]
