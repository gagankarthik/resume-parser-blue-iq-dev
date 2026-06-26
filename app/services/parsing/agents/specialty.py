"""SpecialtyMatchAgent — tier-4 specialty resolution via one batched LLM call.

When the deterministic tiers (name / full_name / keywords) leave specialties
unmatched, this agent is given the list of unmatched specialty strings plus a
filtered shortlist of catalog candidates (id + name + full name) and asked to pick
the best catalog id for each — or none. It returns one structured result for the
whole résumé, so tier 4 costs a single LLM call regardless of how many specialties
missed.

The model only ever returns catalog ids it was shown; the matcher validates each
returned id against the shortlist before trusting it, so a hallucinated id is
dropped rather than surfaced.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.schemas import _coerce_list, _sanitize_str

from .base import BaseAgent, TokenMeter

_SYSTEM = """You map clinical specialty phrases from a healthcare résumé to a catalog of known specialties.

You are given:
- UNMATCHED: specialty phrases taken verbatim from one résumé that an exact/keyword
  match could not resolve.
- CANDIDATES: the ONLY valid catalog specialties, each as "<id> | <name> | <full name>".

For EACH unmatched phrase, choose the single best CANDIDATE that means the same
clinical specialty/unit, and return its exact id. Rules:
- Use ONLY ids that appear in CANDIDATES. Never invent an id.
- If no candidate is a confident match, return specialty_id = null for that phrase
  (do NOT force a weak match).
- confidence is your 0.0–1.0 certainty that the chosen id is correct (0.0 when null).
- Return one entry per UNMATCHED phrase, echoing the phrase back in `raw`."""


class SpecialtyAIMatch(BaseModel):
    raw:          str        = Field(..., description="The unmatched specialty phrase, echoed verbatim")
    specialty_id: str | None = Field(None, description="Chosen catalog id from CANDIDATES, or null if none fits")
    confidence:   float      = Field(0.0, description="0.0–1.0 certainty the id is correct; 0.0 when null")

    @field_validator("raw", mode="before")
    @classmethod
    def _req_raw(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) and v.strip() else ""

    @field_validator("specialty_id", mode="before")
    @classmethod
    def _opt_id(cls, v: object) -> str | None:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int | float):
            return str(int(v)) if float(v).is_integer() else str(v)
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> float:
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            return 0.0
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0


class SpecialtyAIResult(BaseModel):
    matches: list[SpecialtyAIMatch] = Field(default_factory=list)

    @field_validator("matches", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> list:
        return _coerce_list(v)


class SpecialtyMatchAgent(BaseAgent):
    name = "SpecialtyMatchAgent"

    async def run(
        self,
        unmatched: list[str],
        candidates: list[str],
        meter: TokenMeter,
    ) -> list[SpecialtyAIMatch]:
        """Resolve `unmatched` phrases against the `candidates` shortlist.

        `candidates` are pre-formatted "<id> | <name> | <full name>" lines. Returns
        the model's per-phrase choices (unvalidated ids — the caller checks them
        against the catalog before trusting).
        """
        if not unmatched or not candidates:
            return []

        user = (
            "UNMATCHED:\n"
            + "\n".join(f"- {u}" for u in unmatched)
            + "\n\nCANDIDATES:\n"
            + "\n".join(candidates)
        )
        result = await self._structured_call(
            _SYSTEM, user, SpecialtyAIResult, meter, max_tokens=2048
        )
        return result.matches
