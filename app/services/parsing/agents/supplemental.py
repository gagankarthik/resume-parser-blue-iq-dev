"""SupplementalAgent - projects, languages, references, awards, publications."""

from __future__ import annotations

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import SupplementalResult

_SYSTEM = f"""You extract the SUPPLEMENTAL sections of a healthcare résumé into the given schema.

{CORE_RULES}

RULES:
- references[]: listed referees only (name, relationship/title, company, email, phone). Keep each referee's own credentials within their name/title if written (e.g. "Jane Doe, RN, BSN"). If the résumé only says "References available upon request", leave empty.
- awards[]: each award/honor/recognition as a short string (include year in parentheses if stated). Academic honors count — "Summa Cum Laude" on a degree line is an award; do not drop it.
- publications[]: each publication/poster/research item as a single citation string.
- languages[]: spoken/written languages, one per item.
- projects[]: only explicitly labeled projects."""


class SupplementalAgent(BaseAgent):
    name = "SupplementalAgent"

    async def run(self, text: str, meter: TokenMeter) -> SupplementalResult:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn the supplemental sections."
        return await self._structured_call(_SYSTEM, user, SupplementalResult, meter, max_tokens=3072)
