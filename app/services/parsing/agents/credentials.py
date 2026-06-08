"""CredentialsAgent — skills, certifications, and state licenses in one pass.

Kept together because the model must decide, per item, whether something is a
free-form skill, a certification (BLS/ACLS/CCRN…), or a STATE LICENSE (with a
number). Seeing them together prevents a state RN licence from being filed as a
certification or lost entirely.
"""

from __future__ import annotations

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import CredentialsResult

_SYSTEM = f"""You extract a healthcare résumé's SKILLS, CERTIFICATIONS, and LICENSES into the given schema.

{CORE_RULES}

CLASSIFY each item into exactly one bucket:
- skills[]: clinical specialties, units, and competencies (e.g. "ICU", "Med Surg/Tele", "Epic", "Venipuncture"), one item each — not sentences.
- certifications[]: time-limited professional certs (BLS, ACLS, PALS, CCRN, CEN, NRP, TNCC, OCN, ARRT…). A bare date next to a cert ("BLS: 12/2024") is AMBIGUOUS — put it in the neutral `date` field, NOT expiry, unless the résumé labels it issued/expires.
- licenses[]: STATE/professional licenses (e.g. "Florida RN License #RN9411204", "Active NYS Registered Nurse License", "Compact/Multistate RN License", "Radiologic Technologist License (TX)"). Capture name, license_type (credential as written, e.g. "RN"), state (as written, keep "NY"), license_number VERBATIM including any letter prefix, status ("Active"/"In progress"), and dates only if stated. Set is_compact=true ONLY if it literally says compact/multistate/eNLC. A licence is NEVER a certification — do not put it in certifications[].
- An in-progress / pending licence is still captured, with status reflecting that. Never omit a licence number."""


class CredentialsAgent(BaseAgent):
    name = "CredentialsAgent"

    async def run(self, text: str, meter: TokenMeter) -> CredentialsResult:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn skills, certifications, and licenses."
        return await self._structured_call(_SYSTEM, user, CredentialsResult, meter, max_tokens=3072)
