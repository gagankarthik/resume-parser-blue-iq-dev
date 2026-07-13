"""CredentialsAgent - skills, certifications, and state licenses in one pass.

Kept together because the model must decide, per item, whether something is a
free-form skill, a certification (BLS/ACLS/CCRN...), or a STATE LICENSE (with a
number). Seeing them together prevents a state RN licence from being filed as a
certification or lost entirely.
"""

from __future__ import annotations

from .base import BaseAgent, TokenMeter
from .prompts import CORE_RULES
from .schemas import CredentialsResult

_SYSTEM = f"""You extract a healthcare résumé's SKILLS, CERTIFICATIONS, LICENSES, and PROFESSIONAL ASSOCIATIONS into the given schema.

{CORE_RULES}

NEVER DROP an item that is listed under a credentials/certifications/licenses/associations heading — every listed item must land in exactly one of the four buckets below. A section titled e.g. "Professional Associations/Certifications/Licenses/Collaboratives" mixes all the types, so split its items by what each one IS, not by the heading.

SOURCE OF TRUTH: classify items the résumé actually LISTS (in its skills/credentials/certifications/licenses sections or stated in the body). Do NOT invent a license OR certification from the post-nominal letters after the candidate's name ("Jane Smith, RN, BSN" alone creates NO license entry) or from a job title — post-nominals are captured separately as personal credentials.

CLASSIFY each item into exactly one bucket:
- skills[]: clinical specialties, units, and competencies (e.g. "ICU", "Med Surg/Tele", "Epic", "Venipuncture"), one item each — not sentences. NEVER put certifications (CPR, BLS, ACLS, PALS…), licenses, driver's licenses, or academic degrees (BSN, MSN) in skills[] — those belong in the other buckets.
- certifications[]: time-limited or general credentials that are NOT a professional practice license — clinical certs (BLS, ACLS, PALS, CCRN, CEN, NRP, TNCC, OCN, ARRT…) AND non-clinical credentials listed on the résumé such as "CPR", "First Aid", "CNA", "Driver's License". Keep these even though they are not state licenses — do NOT discard a listed credential just because it is non-clinical. Cert dates: "Completed/Issued/Awarded <date>" (e.g. "Steps to Leadership Completed December 2024") → issued_date, NEVER expiry_date. "Expires/valid through <date>" → expiry_date. A bare unlabeled date next to a cert ("BLS: 12/2024") is AMBIGUOUS — put it in the neutral `date` field.
  Certs are usually listed one name per line, sometimes with the issuing organization (e.g. "American Heart Association", "American Red Cross") on a nearby line. EVERY line that names a certification, license course, or training — e.g. "Advanced Life Support", "NIH Stroke Scale Certification", "Basic Life Support (BLS)" — is its OWN entry. NEVER drop such a line and NEVER absorb it into an adjacent cert, even when it has no issuer or when an issuer/date sits beside a DIFFERENT cert. Two cert names on consecutive lines are TWO entries, not one. An issuer organization and date attach to the single cert they belong with; a cert with no issuer of its own keeps issuer/date null rather than borrowing a neighbour's.
- licenses[]: STATE / professional PRACTICE licenses. Two forms qualify:
  • Explicit state licenses (e.g. "Florida RN License #RN9411204", "Active NYS Registered Nurse License", "Compact/Multistate RN License", "Radiologic Technologist License (TX)").
  • A bare nursing/allied PRACTICE credential (RN, LPN, LVN) LISTED in a credentials/certifications/licenses section — these ARE professional licenses even when no number or state is written. Put them here with license_type set to the credential and the missing fields left null; do NOT file RN/LPN/LVN as a certification. (But post-nominals after the name alone do NOT create a license — see SOURCE OF TRUTH above.)
  Capture name, license_type (credential as written, e.g. "RN" — do NOT expand), state (as written, keep "NY"), license_number VERBATIM including any letter prefix, status ("Active"/"In progress"), and dates only if stated. Set is_compact=true ONLY if it literally says compact/multistate/eNLC.
- An in-progress / pending licence is still captured, with status reflecting that. Never omit a licence number when one is written.
- professional_associations[]: society/association MEMBERSHIPS, honor societies, committees, collaboratives, and process-owner roles, each verbatim (e.g. "Sigma Theta Tau International Honor Society of Nursing Member", "American Association of Critical Care Nurses Member", "Sepsis Clinical Services Committee", "SJHS Sepsis Process Owner"). These are NOT certifications or licenses — and never drop them."""


class CredentialsAgent(BaseAgent):
    name = "CredentialsAgent"

    async def run(self, text: str, meter: TokenMeter) -> CredentialsResult:
        user = f"=== RESUME TEXT ===\n{text}\n=== END ===\n\nReturn skills, certifications, and licenses."
        return await self._structured_call(_SYSTEM, user, CredentialsResult, meter, max_tokens=3072)
