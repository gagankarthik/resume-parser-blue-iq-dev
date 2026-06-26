"""
Healthcare profession and specialty taxonomy — matching/lookup logic.

The reference DATA (abbreviation maps, specialty lists, group mapping) now lives
in taxonomy_data.py. This module builds the punctuation-insensitive lookup indexes
over that data and exposes the resolution functions. The data names are re-exported
here (imported into this namespace) so existing
`from ...healthcare_taxonomy import PROFESSION_ABBREVIATIONS` imports keep working.

Provides:
  resolve_specialty / normalize_specialty — raw string → canonical name
  get_specialty_group                     — canonical specialty → group label
  expand_profession                       — credential abbrev → full name
  _match_key                              — punctuation/whitespace-insensitive key
"""

import re

from app.services.normalization.taxonomy_data import (
    ALL_SPECIALTIES,
    PROFESSION_ABBREVIATIONS,
    SPECIALTY_ABBREVIATIONS,
    SPECIALTY_GROUPS,
)

# ── Punctuation-robust matching ───────────────────────────────────────────────
# Resumes and the source spreadsheet disagree on punctuation: en-dash vs hyphen
# ("Ultrasound Tech – General" vs "...- General"), slash spacing ("Med Surg / Tele"
# vs "Med Surg/ Tele"), hyphen-vs-space ("Med-Surg" vs "Med Surg"), and ampersand
# vs "and" ("Labor & Delivery" vs "Labor and Delivery"). We match on a normalized
# key so every variant resolves to the same canonical name without enumerating
# each spelling.


def _match_key(s: str) -> str:
    """Punctuation/whitespace-insensitive lookup key for a specialty string.

    Hyphens and whitespace are treated as the same separator, so "Med-Surg",
    "Med Surg", and "Med - Surg" all collapse to one key. An ampersand is treated
    as "and" ("Labor & Delivery" → "Labor and Delivery"). Slashes are kept (they
    are meaningful — "Med Surg / Tele" — but their surrounding spacing is removed).
    Both the canonical names and the lookup string get this treatment, so the
    transform stays internally consistent.
    """
    s = s.replace("–", "-").replace("—", "-")  # en/em dash → hyphen
    s = s.lower().strip()
    s = s.replace("&", " and ")      # ampersand ≡ "and" (Labor & Delivery)
    s = re.sub(r"\s*/\s*", "/", s)   # collapse spacing around slashes
    s = re.sub(r"[\s-]+", " ", s)    # hyphens + whitespace → single space
    return s.strip()


_SPECIALTY_BY_KEY:   dict[str, str] = {_match_key(s): s for s in ALL_SPECIALTIES}
_ABBREV_BY_KEY:      dict[str, str] = {_match_key(k): v for k, v in SPECIALTY_ABBREVIATIONS.items()}
_PROF_ABBREV_BY_KEY: dict[str, str] = {_match_key(k): v for k, v in PROFESSION_ABBREVIATIONS.items()}
_PROF_NAME_BY_KEY:   dict[str, str] = {_match_key(v): v for v in PROFESSION_ABBREVIATIONS.values()}
_GROUP_BY_KEY:       dict[str, str] = {_match_key(k): g for k, g in SPECIALTY_GROUPS.items()}


def resolve_specialty(raw: str) -> str | None:
    """
    Resolve a raw skill string to a canonical specialty or profession name.

    Resolution order (all punctuation/case-insensitive):
      1. Canonical specialty name (e.g. "Intensive Care Unit")
      2. Specialty abbreviation/shorthand (e.g. "ICU", "Med Surg/ Tele")
      3. Full profession/credential name (e.g. "Registered Nurse")
      4. Profession/credential abbreviation (e.g. "RN", "OT")
      5. Credential-prefix expansion — "OT - Acute Care" → "Occupational
         Therapist – Acute Care" — so every "<credential> - <setting>" variant
         from the taxonomy resolves without enumerating each setting.

    Returns the canonical name, or None when the string is out-of-taxonomy.
    """
    if not raw or not raw.strip():
        return None

    key = _match_key(raw)
    if key in _SPECIALTY_BY_KEY:
        return _SPECIALTY_BY_KEY[key]
    if key in _ABBREV_BY_KEY:
        return _ABBREV_BY_KEY[key]
    if key in _PROF_NAME_BY_KEY:
        return _PROF_NAME_BY_KEY[key]
    if key in _PROF_ABBREV_BY_KEY:
        return _PROF_ABBREV_BY_KEY[key]

    # Credential-prefix expansion: a single leading token, then a separator.
    m = re.match(r"^\s*([A-Za-z]+)\s*[-–/]\s*(.+)$", raw)
    if m:
        head_key = _match_key(m.group(1))
        expanded = _PROF_ABBREV_BY_KEY.get(head_key) or _ABBREV_BY_KEY.get(head_key)
        if expanded:
            candidate = _match_key(f"{expanded}-{m.group(2)}")
            if candidate in _SPECIALTY_BY_KEY:
                return _SPECIALTY_BY_KEY[candidate]

    return None


def get_specialty_group(specialty: str) -> str | None:
    """Return the group label for a specialty name (punctuation-insensitive)."""
    if specialty in SPECIALTY_GROUPS:
        return SPECIALTY_GROUPS[specialty]
    return _GROUP_BY_KEY.get(_match_key(specialty))


def expand_profession(abbrev: str) -> str:
    """Expand a credential abbreviation to full name, or return as-is."""
    return PROFESSION_ABBREVIATIONS.get(abbrev.lower().strip(), abbrev)


def normalize_specialty(raw: str) -> str:
    """Normalize a specialty string to its canonical name, or return it cleaned."""
    return resolve_specialty(raw) or raw.strip()
