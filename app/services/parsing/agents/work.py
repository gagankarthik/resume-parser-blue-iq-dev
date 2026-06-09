"""WorkExperienceAgent — Stage 2: extract each role independently.

Driven by the StructureAgent's role map: one focused LLM call per role, told the
exact bullet count to honour. This is what fixes dropped employers and the
"travel assignment flattened into Unknown role" bug — each facility is extracted
as its own entry that already knows its profession/agency from the structure map.
Falls back to a single full-document extraction when no structure map exists.
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.models.schemas import ExperienceItem

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import RoleBoundary, WorkResult

log = get_logger(__name__)

_SYSTEM_ONE = f"""You extract ONE work-history role from a healthcare résumé into the given schema.

{CORE_RULES}

{{bullet_instruction}}

FIELD RULES:
- company = facility/employer name. role = job title (include the credential if written, e.g. "RN - MICU").
- If this is a travel/agency assignment, set agency_name and keep the role's own profession/specialties.
- location = the FULL address line as written (street/suite included). Also fill city/state/country/zip ONLY if stated (keep state as written, e.g. "NY"; never invent country or zip).
- employer_phone = the employer/facility phone number if one is written next to this role (e.g. "304-287-2120"), copied verbatim; null otherwise. Do NOT confuse it with the candidate's own contact phone.
- description = an ARRAY, one item per responsibility/duty bullet, copied VERBATIM. If the role is written as prose, split each duty sentence into its own item. Never merge separate bullets; never split one bullet into several.
- achievements = only items with measurable results.
- Fill profession, specialties, shift, charting_system, nurse_to_patient_ratio, beds_in_unit, reason_for_leaving, position_held, and the teaching/magnet/trauma flags ONLY when explicitly stated."""

_SYSTEM_FULL = f"""You extract ALL work-history roles from a healthcare résumé into {{ "work_experience": [ ... ] }}.

{CORE_RULES}

RULES:
- Include EVERY role, even old/short ones. Separate each role/assignment as its own entry (critical for travel nurses).
- For a travel/agency umbrella role listing multiple facilities, output one entry per facility, each inheriting the umbrella profession/role and setting agency_name. NEVER use "Unknown" as a role.
- description = an ARRAY, one item per duty bullet copied verbatim (split prose into sentences). location = full address line as written.
- employer_phone = the facility/employer phone number written next to a role (verbatim), null otherwise — never the candidate's own phone."""


class WorkExperienceAgent(BaseAgent):
    name = "WorkExperienceAgent"

    async def run(self, text: str, roles: list[RoleBoundary], meter: TokenMeter) -> list[ExperienceItem]:
        if not roles:
            return await self._extract_full(text, meter)

        # First pass: every role in parallel (the global semaphore bounds real concurrency).
        first = await asyncio.gather(
            *[self._extract_one(text, r, meter) for r in roles],
            return_exceptions=True,
        )

        results: list[ExperienceItem | None] = [None] * len(roles)
        retry: list[int] = []
        for i, res in enumerate(first):
            if isinstance(res, Exception):
                log.warning("work_role_failed", company=roles[i].company, error=str(res))
                retry.append(i)
            else:
                results[i] = res

        # One retry pass for transient failures so a single hiccup doesn't drop a job.
        if retry:
            again = await asyncio.gather(
                *[self._extract_one(text, roles[i], meter) for i in retry],
                return_exceptions=True,
            )
            for idx, res in zip(retry, again):
                if not isinstance(res, Exception):
                    results[idx] = res

        # Keep the output 1:1 with the structure map. A role that failed even after
        # retry is backfilled with a stub from its RoleBoundary rather than dropped,
        # so (a) the employer is never lost and (b) downstream positional pairing in
        # the ValidatorAgent (work[i] ↔ roles[i]) stays correct — collapsing the list
        # here is what caused labels to shift up and employers to duplicate.
        for i, res in enumerate(results):
            if res is None:
                log.warning("work_role_stubbed", company=roles[i].company)
                results[i] = self._stub_from_role(roles[i])

        return [r for r in results if r is not None]

    @staticmethod
    def _stub_from_role(role: RoleBoundary) -> ExperienceItem:
        """Minimal entry from the structure map for a role whose focused extraction
        failed — preserves the employer/identity (no duty bullets) so it isn't lost."""
        return ExperienceItem(
            company=role.company,
            role=role.title or role.profession or "Unknown",
            start_date=role.start_date,
            end_date=role.end_date,
            agency_name=role.agency_name,
            profession=role.profession,
        )

    async def _extract_one(self, text: str, role: RoleBoundary, meter: TokenMeter) -> ExperienceItem:
        if role.bullet_count > 0:
            bullet_instruction = (
                f"CRITICAL BULLET COUNT: this role has EXACTLY {role.bullet_count} responsibility "
                f"bullet(s). Extract all {role.bullet_count} into description[] (verbatim). "
                "Do NOT add, skip, or merge any bullet."
            )
        else:
            bullet_instruction = (
                "Extract every responsibility/duty into description[] (verbatim, one per item). "
                "Leave it empty only if the role truly lists no duties."
            )
        system = _SYSTEM_ONE.format(bullet_instruction=bullet_instruction)
        user = (
            f"Extract this role: {role.company} | {role.title or ''} | "
            f"{role.start_date or ''}–{role.end_date or ''}"
            + (f" | agency: {role.agency_name}" if role.agency_name else "")
            + "\n\n=== RESUME TEXT ===\n"
            f"{text}\n=== END ===\n\nReturn ONLY this single role."
        )
        item = await self._structured_call(system, user, ExperienceItem, meter, max_tokens=4096)
        # Seed identity/agency from the structure map when the focused pass left them blank.
        if (not item.role) or item.role.strip().lower() == "unknown":
            item.role = role.title or role.profession or item.role
        if role.agency_name and not item.agency_name:
            item.agency_name = role.agency_name
        if role.profession and not item.profession:
            item.profession = role.profession
        return item

    async def _extract_full(self, text: str, meter: TokenMeter) -> list[ExperienceItem]:
        log.info("work_full_fallback")
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nExtract all work experience."
        result = await self._structured_call(_SYSTEM_FULL, user, WorkResult, meter, max_tokens=8192)
        return result.work_experience
