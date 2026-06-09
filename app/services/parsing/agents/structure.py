"""StructureAgent — Stage 1: map every work-history role and its bullet count.

It does NOT extract the full role; it pins down how many roles exist, their
identity, and how many duty bullets each has, so the WorkAgent can extract each
role independently and the ValidatorAgent can detect dropped bullets. Travel /
agency umbrella roles are decomposed here into one boundary per facility.
"""

from __future__ import annotations

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import ResumeStructure

_SYSTEM = f"""You are a healthcare résumé STRUCTURE analyst. Read the résumé and locate EVERY work-history role.
Do NOT extract responsibilities here — only map the roles.

For each role return: company, title, profession, start_date, end_date, agency_name, is_travel_assignment, bullet_count.

{CORE_RULES}

CRITICAL RULES:
- Find EVERY role, including old ones and short assignments. Do not skip any.
- bullet_count = the EXACT number of responsibility/duty bullets (or duty sentences in prose) listed under that role in the source text. Count carefully — this number is used to verify the later extraction. Use 0 only if the role genuinely lists no duties.
- TRAVEL / AGENCY roles: when one umbrella role (e.g. "Travel RN — Supplemental Healthcare") lists several facilities/sites beneath it, output ONE role per facility. Each MUST inherit the umbrella profession (e.g. "RN") and title, set agency_name to the staffing agency, and set is_travel_assignment=true. NEVER collapse them into one, and NEVER leave such a facility with an empty/Unknown title.
- Decompose a facility under a travel/agency umbrella ONLY when it is clearly listed beneath that umbrella heading. A facility that has its OWN job title and OWN date range (e.g. "Supervisory RN / On-Call RN, 03/2013–07/2015 at Woodlands Assisted Living") is a SEPARATE employer — give it its own role with its own title/dates, do NOT set agency_name, and do NOT attach its title or dates to a neighbouring employer. Each employer keeps its own role and date range; never shift a title or date range from one employer onto another.
- Keep roles in the order written (most recent first if that's how the résumé lists them)."""


class StructureAgent(BaseAgent):
    name = "StructureAgent"

    async def run(self, text: str, meter: TokenMeter) -> ResumeStructure:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn the structure map."
        return await self._structured_call(_SYSTEM, user, ResumeStructure, meter, max_tokens=4096)
