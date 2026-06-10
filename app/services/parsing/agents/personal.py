"""PersonalInfoAgent — name, post-nominal credentials, contact, summary."""

from __future__ import annotations

import json

from app.services.parsing.rule_parser import RuleExtracted

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import PersonalResult

_SYSTEM = f"""You extract the PERSONAL / CONTACT section of a healthcare résumé into the given schema.

{CORE_RULES}

RULES:
- full_name: the candidate's name ONLY — strip trailing credential/licence/degree suffixes.
- credentials: the post-nominals that follow the name (e.g. "Jane Smith, RN, BSN, MPH, CCRN" → ["RN","BSN","MPH","CCRN"]), each a separate item, in order. NEVER drop these.
- location: the candidate's FULL home address line as written, including street/number if present — do NOT shorten to city/state/zip.
- personal.summary: the professional summary/objective text if present, copied VERBATIM, else null. Do NOT rewrite or clean it.
- summary_off_topic: set true ONLY when that summary is clearly unrelated to the candidate's healthcare profession/work history — e.g. leftover boilerplate from an unrelated occupation (a dance instructor, retail, etc.). When unsure, leave it false. Either way, still copy the summary verbatim.
- Contact anchors: the pre-extracted email/phone/URL lists below were found by regex and are authoritative — use them as given. But if a list is EMPTY, the regex found nothing (OCR may have garbled it, e.g. an underlined hyperlink): extract that field from the résumé text YOURSELF, repairing obvious OCR artifacts (stray spaces inside an email address, '(@' for '@'). Never leave the email null when one is visible in the text."""


class PersonalInfoAgent(BaseAgent):
    name = "PersonalInfoAgent"

    async def run(self, text: str, anchors: RuleExtracted, meter: TokenMeter) -> PersonalResult:
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
        return await self._structured_call(_SYSTEM, user, PersonalResult, meter, max_tokens=2048)
