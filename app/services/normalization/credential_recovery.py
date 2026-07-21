"""
Deterministic backstop: recover credential items the AI parse dropped.

The AI captures skills / certifications / licenses / professional associations
reliably on clean text, but a messy extraction (a two-column PDF whose lines
interleave, a dense document, or a mixed heading like "Professional Associations/
Certifications/Licenses/Collaboratives") can make the model miss items - most often
a state LICENSE or a MEMBERSHIP/COMMITTEE, which have no obvious keyword like "BLS".

This pass rescans the résumé text and adds any license or professional-association
line the parse left out. It is:
  * ADDITIVE - it never removes or overrides what the model found;
  * CONSERVATIVE - licenses need a licence number or a practice credential on a
    credentials-section line; associations need an explicit membership/committee
    word AND must sit under a detected credentials heading (so a duty bullet that
    merely mentions "committee" is never mistaken for a membership);
  * DEDUPED - an item already present (by number, or normalised name) is skipped.

Runs BEFORE `normalize()` so recovered items flow through the same casing,
cross-bucket hygiene, and dedup as the model's own output.
"""

from __future__ import annotations

import re

from app.models.schemas import LicenseItem, ParsedResumeAI
from app.services.parsing import section_detector

# A line that names a professional practice LICENSE.
_LICENSE_WORD_RE = re.compile(r"\blicen[sc]e\b", re.IGNORECASE)
# A driver's licence is a certification, not a professional practice licence - the
# credentials agent keeps it in certifications[]; never promote it here.
_DRIVERS_RE = re.compile(r"\bdriver'?s?\b", re.IGNORECASE)
# A licence/permit number: '#'-prefixed, or a bare letter-prefixed long digit run
# (e.g. "RN9411204"). A plain phone/date/ID without that shape is not matched.
_LICENSE_NUM_RE = re.compile(
    r"#\s*([A-Za-z]{0,4}\d{3,}[\dA-Za-z-]*)"
    r"|(?<!\w)([A-Za-z]{1,4}\d{5,})"
)
# Practice credentials that head a state-licence line.
_PRACTICE_CRED_RE = re.compile(
    r"\b(RN|LPN|LVN|APRN|CNM|CRNA|NP|RRT|CRT|CNA|COTA|PTA|SLP|MSW|LCSW|LMSW|ARRT|RT|OT|PT|"
    r"Registered Nurse|Licensed Practical Nurse|Licensed Vocational Nurse|"
    r"Advanced Practice Registered Nurse|Nurse Practitioner)\b"
)

# A line that names a professional-ASSOCIATION membership / committee / role. Only
# explicit membership words trigger it - NOT a bare "Association"/"Society", which
# would wrongly grab a certification's issuer line ("American Heart Association").
_ASSOC_RE = re.compile(
    r"\b(member|membership|committee|collaboratives?|councils?|chapter|caucus|"
    r"task\s*force|process\s+owner|board\s+member|honou?r\s+society|\bfellow\b)\b",
    re.IGNORECASE,
)

# Leading list-marker/bullet characters to strip off a line before matching.
_BULLET_CHARS = " \t-•*·—–▪◦‣"


def _key(value: str) -> str:
    """Case/punctuation-insensitive matching key for dedup across buckets."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def recover(text: str, parsed: ParsedResumeAI) -> None:
    """Recover dropped licenses and professional associations from the résumé text.

    Mutates `parsed` in place, additively. Safe to call on every parse.
    """
    if not text or not text.strip():
        return
    sections = section_detector.detect(text)
    # The mixed credentials block (certs/licenses/associations) lands under
    # "certifications" (see section_detector). Fall back to full_text only for
    # licenses, which carry their own strong number signal.
    cred_text = sections.get("certifications", "")
    _recover_licenses(text, cred_text, parsed)
    if cred_text:
        _recover_associations(cred_text, parsed)


def _recover_licenses(full_text: str, cred_text: str, parsed: ParsedResumeAI) -> None:
    existing_nums = {
        (lic.license_number or "").upper() for lic in parsed.licenses if lic.license_number
    }
    existing_keys = {_key(lic.name) for lic in parsed.licenses}
    seen: set[str] = set()

    # The credentials section first (list items), then the whole text (to catch a
    # numbered licence line that scrambled out of its section during extraction).
    for in_cred_section, source in ((True, cred_text), (False, full_text)):
        for raw in source.splitlines():
            line = raw.strip(_BULLET_CHARS)
            if not line or line in seen:
                continue
            if not _LICENSE_WORD_RE.search(line) or _DRIVERS_RE.search(line):
                continue
            num_m = _LICENSE_NUM_RE.search(line)
            cred_m = _PRACTICE_CRED_RE.search(line)
            # Strong signal required: a licence number, OR a practice credential on a
            # credentials-section line (a list item, not a prose sentence elsewhere).
            if not (num_m or (cred_m and in_cred_section)):
                continue
            number = (num_m.group(1) or num_m.group(2)) if num_m else None
            if number and number.upper() in existing_nums:
                continue
            if _key(line) in existing_keys:
                continue
            parsed.licenses.append(
                LicenseItem(
                    name=line,
                    license_type=cred_m.group(1) if cred_m else None,
                    license_number=number,
                )
            )
            seen.add(line)
            existing_keys.add(_key(line))
            if number:
                existing_nums.add(number.upper())


def _recover_associations(cred_text: str, parsed: ParsedResumeAI) -> None:
    existing = {_key(a) for a in parsed.professional_associations}
    cert_keys = {_key(c.name) for c in parsed.certifications}

    for raw in cred_text.splitlines():
        line = raw.strip(_BULLET_CHARS)
        # A membership/committee line is short; a paragraph is not.
        if not line or len(line) > 120:
            continue
        if not _ASSOC_RE.search(line):
            continue
        if _LICENSE_WORD_RE.search(line):
            continue  # a licence line, handled by _recover_licenses
        k = _key(line)
        if not k or k in existing or k in cert_keys:
            continue
        parsed.professional_associations.append(line)
        existing.add(k)
