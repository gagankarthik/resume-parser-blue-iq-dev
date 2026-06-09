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

NEVER DROP an item that is listed under a credentials/certifications/licenses heading — every listed item must land in exactly one of the three buckets below. A section literally titled "Certifications and Licenses" mixes both types, so split its items by what each one IS, not by the heading.

CLASSIFY each item into exactly one bucket:
- skills[]: clinical specialties, units, and competencies (e.g. "ICU", "Med Surg/Tele", "Epic", "Venipuncture"), one item each — not sentences.
- certifications[]: time-limited or general credentials that are NOT a professional practice license — clinical certs (BLS, ACLS, PALS, CCRN, CEN, NRP, TNCC, OCN, ARRT…) AND non-clinical credentials listed on the résumé such as "CPR", "First Aid", "CNA", "Driver's License". Keep these even though they are not state licenses — do NOT discard a listed credential just because it is non-clinical. A bare date next to a cert ("BLS: 12/2024") is AMBIGUOUS — put it in the neutral `date` field, NOT expiry, unless the résumé labels it issued/expires.
- licenses[]: STATE / professional PRACTICE licenses. Two forms qualify:
  • Explicit state licenses (e.g. "Florida RN License #RN9411204", "Active NYS Registered Nurse License", "Compact/Multistate RN License", "Radiologic Technologist License (TX)").
  • A bare nursing/allied PRACTICE credential listed on its own (RN, LPN, LVN) — these ARE professional licenses even when no number or state is written. Put them here with license_type set to the credential and the missing fields left null; do NOT file RN/LPN/LVN as a certification.
  Capture name, license_type (credential as written, e.g. "RN" — do NOT expand), state (as written, keep "NY"), license_number VERBATIM including any letter prefix, status ("Active"/"In progress"), and dates only if stated. Set is_compact=true ONLY if it literally says compact/multistate/eNLC.
- An in-progress / pending licence is still captured, with status reflecting that. Never omit a licence number when one is written."""


class CredentialsAgent(BaseAgent):
    name = "CredentialsAgent"

    async def run(self, text: str, meter: TokenMeter) -> CredentialsResult:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn skills, certifications, and licenses."
        return await self._structured_call(_SYSTEM, user, CredentialsResult, meter, max_tokens=3072)
