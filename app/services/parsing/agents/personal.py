"""PersonalInfoAgent — name, post-nominal credentials, contact, summary."""

from __future__ import annotations

import json

from app.models.schemas import PersonalInfo
from app.services.parsing.rule_parser import RuleExtracted

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES

_SYSTEM = f"""You extract the PERSONAL / CONTACT section of a healthcare résumé into the given schema.

{CORE_RULES}

RULES:
- full_name: the candidate's name ONLY — strip trailing credential/licence/degree suffixes.
- credentials: the post-nominals that follow the name (e.g. "Jane Smith, RN, BSN, MPH, CCRN" → ["RN","BSN","MPH","CCRN"]), each a separate item, in order. NEVER drop these.
- location: the candidate's FULL home address line as written, including street/number if present — do NOT shorten to city/state/zip.
- summary: the professional summary/objective text if present, else null.
- Use the pre-extracted contact anchors for email/phone/URLs; do not re-derive them."""


class PersonalInfoAgent(BaseAgent):
    name = "PersonalInfoAgent"

    async def run(self, text: str, anchors: RuleExtracted, meter: TokenMeter) -> PersonalInfo:
        anchors_block = json.dumps(
            {
                "emails": anchors.emails,
                "phones": anchors.phones,
                "linkedin_urls": anchors.linkedin_urls,
                "github_urls": anchors.github_urls,
                "portfolio_urls": anchors.portfolio_urls,
            },
            indent=2,
        )
        user = (
            f"PRE-EXTRACTED CONTACT ANCHORS (use directly):\n{anchors_block}\n\n"
            f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn the personal information."
        )
        return await self._structured_call(_SYSTEM, user, PersonalInfo, meter, max_tokens=2048)
