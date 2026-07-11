"""
Facility → catalog-id matcher.

Resolves a résumé employer/facility string (``ExperienceItem.company``) to a
platform facility id + confidence. The facilities API returns NO score of its own,
so confidence is derived from match quality, mirroring the specialty matcher's
deterministic tiers (there is no AI tier here):

  1. name  — the facility name matches a catalog facility name exactly
             (after punctuation/case normalisation).              conf 1.00
  2. fuzzy — a near-identical spelling/typo above a conservative
             similarity floor; confidence is the similarity,
             capped below the exact tier.                          conf ≤ 0.94

A string that matches neither returns ``matched=False`` / ``facility_id=None`` —
never a guess. When no catalog is loaded (snapshot absent), every lookup is a
graceful miss, so ``facility_id`` simply stays null until the snapshot is supplied.

Facility names carry generic noise ("Medical Center", "Regional", "Hospital") that
would make a loose fuzzy match dangerous, so the fuzzy floor is deliberately high
and matching is whole-string (never a sub-phrase) — a role's facility is either the
platform's facility or left for review, never silently mapped to a look-alike.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from app.services.normalization.facility_catalog import (
    FacilityCatalog,
    FacilityRecord,
    get_catalog,
)
from app.services.normalization.healthcare_taxonomy import _match_key

# Per-tier confidence. Tunable in one place.
CONF_NAME      = 1.0
CONF_FUZZY_MAX = 0.94   # a near-miss/typo; graded by similarity, never an exact tier
CONF_UNMATCHED = 0.0

# Conservative similarity floor — only a near-identical spelling auto-matches.
FUZZY_THRESHOLD = 0.90

# Employer strings the extractor emits when it could not read a facility name.
_PLACEHOLDERS = frozenset({"", "unknown", "unknown institution", "n/a", "none"})


@dataclass(frozen=True)
class FacilityMatch:
    """Result of resolving one facility string against the catalog."""

    name:             str
    facility_id:      str | None = None
    health_system:    str | None = None
    health_system_id: str | None = None
    confidence:       float = CONF_UNMATCHED
    matched:          bool = False
    match_tier:       str | None = None


def _candidate_keys(text: str) -> list[str]:
    """Ordered, de-duplicated match keys to try for one facility phrase.

    A résumé often trails a facility name with a parenthetical ("Mercy Hospital
    (Main Campus)") or a city ("Mercy Hospital, St. Louis"); probe the phrase as
    written first, then with parentheticals removed, then the head before the first
    comma — so the cleanest whole-name spelling still resolves at full confidence.
    """
    keys: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        key = _match_key(value)
        if key and key not in keys:
            keys.append(key)

    add(text)
    add(re.sub(r"\([^)]*\)", " ", text))          # drop parentheticals
    head, sep, _ = text.partition(",")
    if sep:
        add(head)
    return keys


def _fuzzy_lookup(
    catalog: FacilityCatalog, keys: list[str], *, threshold: float = FUZZY_THRESHOLD
) -> tuple[FacilityRecord, float] | None:
    """Resolve a near-miss spelling by whole-string similarity against every record.

    Uses difflib's cheap ratio pre-filters so the scan stays fast. Returns the
    closest record above ``threshold`` (best ratio wins); an identical (ratio 1.0)
    match is treated as exact and scored 1.0.
    """
    if catalog.is_empty:
        return None

    best: tuple[float, FacilityRecord] | None = None
    for rec in catalog.records:
        tkey = _match_key(rec.name)
        for cand in keys:
            sm = difflib.SequenceMatcher(None, cand, tkey)
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            ratio = sm.ratio()
            if ratio >= threshold and (best is None or ratio > best[0]):
                best = (ratio, rec)
    if best is None:
        return None
    ratio, rec = best
    if ratio >= 1.0:
        return rec, CONF_NAME
    return rec, min(round(ratio, 2), CONF_FUZZY_MAX)


def match(company: str | None) -> FacilityMatch:
    """Resolve one raw facility/employer string to a catalog id + confidence."""
    text = (company or "").strip()
    if not text or text.lower() in _PLACEHOLDERS:
        return FacilityMatch(name=text or "Unknown")

    catalog = get_catalog()
    keys = _candidate_keys(text)

    # Tier 1: exact name over the candidate spellings.
    for key in keys:
        rec = catalog.by_name_key.get(key)
        if rec is not None:
            return _matched(rec, CONF_NAME, "name")

    # Tier 2: conservative fuzzy match for a near-miss spelling/typo.
    hit = _fuzzy_lookup(catalog, keys)
    if hit is not None:
        rec, conf = hit
        return _matched(rec, conf, "name" if conf >= CONF_NAME else "fuzzy")

    # No catalog id — surfaced for review, never guessed.
    return FacilityMatch(name=text, confidence=CONF_UNMATCHED, matched=False)


def _matched(rec: FacilityRecord, confidence: float, tier: str) -> FacilityMatch:
    return FacilityMatch(
        name=rec.name,
        facility_id=rec.id,
        health_system=rec.health_system,
        health_system_id=rec.health_system_id,
        confidence=confidence,
        matched=True,
        match_tier=tier,
    )
