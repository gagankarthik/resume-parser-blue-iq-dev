"""
Specialty reference catalog — the source of truth for specialty IDs + keywords.

The platform delivers a catalog of clinical specialties, each with an id, the
canonical specialty name, an optional fuller name, and search keywords. This
module loads that catalog from `settings.specialty_catalog_path` (JSON list, or a
CSV with the same columns) and exposes punctuation/case-insensitive lookup indexes
keyed off `healthcare_taxonomy._match_key`, so the specialty matcher can resolve a
résumé specialty string to a catalog id without enumerating every spelling.

Design:
  • The catalog is OPTIONAL. When the path is unset or the file is missing/empty,
    `get_catalog()` returns an empty catalog (logged once) and the matcher falls
    back to canonical NAMES from the built-in taxonomy with id=None. Nothing here
    raises on a missing/garbled file — a bad catalog must never break parsing.
  • Loaded once and cached at module level (same pattern as the taxonomy's
    `_*_BY_KEY` dicts). `reload()` is provided for tests.

Expected record shape (per specialty), extra keys ignored::

    {"id": "1042", "specialty": "Medical Surgical",
     "full_name": "Medical Surgical / Telemetry",
     "keywords": ["med surg", "ms/tele"], "group": "Med Surg / Tele",
     "profession": "RN"}
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.normalization.healthcare_taxonomy import _match_key

log = get_logger(__name__)


@dataclass(frozen=True)
class SpecialtyRecord:
    """One catalog specialty."""

    id:            str
    name:          str
    full_name:     str | None = None
    group:         str | None = None
    keywords:      tuple[str, ...] = ()
    profession:    str | None = None
    profession_id: str | None = None
    group_id:      str | None = None
    category:      str | None = None
    category_id:   str | None = None


# Résumés spell professions out ("Registered Nurse"), but the platform catalog
# keys them by short code ("RN"). Map the common full titles / abbreviations to the
# catalog's profession key(s) so scoped matching (and the AI-tier profession guard)
# line up. Keys are already _match_key-normalized (lowercase; "&"→"and"; slash kept).
_PROFESSION_ALIASES: dict[str, tuple[str, ...]] = {
    # Nursing
    "registered nurse": ("rn",),
    "licensed practical nurse": ("lpn/lvn", "lpn", "lvn"),
    "licensed vocational nurse": ("lpn/lvn", "lpn", "lvn"),
    "lpn": ("lpn/lvn", "lvn"),
    "lvn": ("lpn/lvn", "lpn"),
    "nurse practitioner": ("np",),
    "advanced practice registered nurse": ("np",),
    "aprn": ("np",),
    "certified nursing assistant": ("cna",),
    "certified nurse assistant": ("cna",),
    "certified nurse aide": ("cna",),
    "nursing assistant": ("cna",),
    "nurse aide": ("cna",),
    "certified medication aide": ("cna",),
    # EMS
    "emergency medical technician": ("emt",),
    # Allied health
    "patient care tech": ("patient care technician",),
    "pct": ("patient care technician",),
    "certified medical assistant": ("medical assistant",),
    "registered medical assistant": ("medical assistant",),
    "respiratory therapist": ("respiratory and neuro",),
    "registered respiratory therapist": ("respiratory and neuro",),
    "certified respiratory therapist": ("respiratory and neuro",),
    "physical therapist": ("therapy and rehab",),
    "physical therapist assistant": ("therapy and rehab",),
    "occupational therapist": ("therapy and rehab",),
    "speech language pathologist": ("therapy and rehab",),
    "radiologic technologist": ("radiology and cardiology",),
    "radiology technologist": ("radiology and cardiology",),
    "rad tech": ("radiology and cardiology",),
    "surgical technologist": ("surgical services",),
    "surgical tech": ("surgical services",),
    "operating room technician": ("surgical services",),
    "medical laboratory technician": ("laboratory",),
    "medical laboratory scientist": ("laboratory",),
    "medical technologist": ("laboratory",),
}


def profession_keys(profession: str | None) -> list[str]:
    """Lookup keys a profession string can match on.

    The platform names some professions as a pair ("LPN/ LVN"); split those so a
    résumé that says just "LPN" (or "LVN") still scopes to that profession. Résumés
    also spell titles out ("Registered Nurse") where the catalog uses codes ("RN"),
    so common titles/abbreviations are aliased to the catalog key(s). Returns the
    full key first, then each slash-separated part, then any aliased catalog keys.
    """
    if not profession or not profession.strip():
        return []
    full = _match_key(profession)
    keys = [full]
    for part in full.split("/"):
        part = part.strip()
        if part and part not in keys:
            keys.append(part)
    for k in list(keys):
        for alias in _PROFESSION_ALIASES.get(k, ()):
            if alias not in keys:
                keys.append(alias)
    return keys


@dataclass
class SpecialtyCatalog:
    """Loaded catalog plus the lookup indexes the matcher needs.

    Two index layers per field: a flat, profession-agnostic one (first spelling
    wins) and a profession-scoped one keyed ``(profession_key, value_key)``. The
    same specialty NAME maps to a different id per profession, so a role with a
    known credential resolves via the scoped index first, then falls back to flat.
    """

    records:        list[SpecialtyRecord]   = field(default_factory=list)
    by_name_key:    dict[str, SpecialtyRecord] = field(default_factory=dict)
    by_full_key:    dict[str, SpecialtyRecord] = field(default_factory=dict)
    by_keyword_key: dict[str, SpecialtyRecord] = field(default_factory=dict)
    by_prof_name_key:    dict[tuple[str, str], SpecialtyRecord] = field(default_factory=dict)
    by_prof_full_key:    dict[tuple[str, str], SpecialtyRecord] = field(default_factory=dict)
    by_prof_keyword_key: dict[tuple[str, str], SpecialtyRecord] = field(default_factory=dict)
    # profession match-key → platform profession id (RN→"1", CNA→"18", …).
    profession_ids:      dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.records

    def profession_id_for(self, profession: str | None) -> str | None:
        """Resolve a résumé profession string to its platform id (via aliases)."""
        for key in profession_keys(profession):
            pid = self.profession_ids.get(key)
            if pid is not None:
                return pid
        return None

    def find(
        self,
        index: dict[str, SpecialtyRecord],
        prof_index: dict[tuple[str, str], SpecialtyRecord],
        value_key: str,
        prof_lookup_keys: list[str],
    ) -> SpecialtyRecord | None:
        """Prefer a profession-scoped hit, else the flat (first-wins) hit."""
        for pk in prof_lookup_keys:
            rec = prof_index.get((pk, value_key))
            if rec is not None:
                return rec
        return index.get(value_key)


# Module-level cache. None = not yet loaded this process.
_catalog: SpecialtyCatalog | None = None


def get_catalog() -> SpecialtyCatalog:
    """Return the cached catalog, loading it on first use."""
    global _catalog
    if _catalog is None:
        _catalog = _load(get_settings().specialty_catalog_path)
    return _catalog


def reload(path: str | None = None) -> SpecialtyCatalog:
    """Force a reload (used by tests). Falls back to the configured path."""
    global _catalog
    if path is None:
        path = get_settings().specialty_catalog_path
    _catalog = _load(path)
    return _catalog


def _load(path: str | None) -> SpecialtyCatalog:
    if not path:
        log.info("specialty_catalog_unset")
        return SpecialtyCatalog()

    p = Path(path)
    if not p.is_file():
        log.warning("specialty_catalog_missing", path=str(p))
        return SpecialtyCatalog()

    try:
        rows = _read_rows(p)
    except Exception as exc:  # never let a bad catalog break parsing
        log.warning("specialty_catalog_load_failed", path=str(p), error=str(exc))
        return SpecialtyCatalog()

    records = [r for row in rows if (r := _to_record(row)) is not None]
    catalog = _build_indexes(records)
    log.info("specialty_catalog_loaded", path=str(p), count=len(records))
    return catalog


def _read_rows(p: Path) -> list[dict]:
    """Parse the catalog file into a list of raw dict rows (JSON or CSV)."""
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))

    data = json.loads(text)
    # Accept either a bare list or a {"specialties": [...]} envelope.
    if isinstance(data, dict):
        for key in ("specialties", "records", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def _to_record(row: object) -> SpecialtyRecord | None:
    if not isinstance(row, dict):
        return None
    raw_id = row.get("id") if row.get("id") is not None else row.get("specialty_id")
    name = row.get("specialty") or row.get("name")
    if raw_id is None or not isinstance(name, str) or not name.strip():
        return None

    full = row.get("full_name") or row.get("specialty_full") or row.get("SpecialtyFull")
    group = row.get("group") or row.get("Group")
    profession = row.get("profession") or row.get("Profession")

    raw_keywords = row.get("keywords")
    if isinstance(raw_keywords, str):
        # CSV keywords arrive as one delimited cell ("med surg|ms/tele").
        parts = [k for chunk in raw_keywords.split("|") for k in chunk.split(",")]
    elif isinstance(raw_keywords, list):
        parts = raw_keywords
    else:
        parts = []
    keywords = tuple(
        k.strip() for k in parts if isinstance(k, str) and k.strip()
    )

    return SpecialtyRecord(
        id=str(raw_id).strip(),
        name=name.strip(),
        full_name=full.strip() if isinstance(full, str) and full.strip() else None,
        group=group.strip() if isinstance(group, str) and group.strip() else None,
        keywords=keywords,
        profession=profession.strip() if isinstance(profession, str) and profession.strip() else None,
        profession_id=_clean_id(row.get("profession_id")),
        group_id=_clean_id(row.get("group_id")),
        category=(c.strip() if isinstance(c := row.get("category"), str) and c.strip() else None),
        category_id=_clean_id(row.get("category_id")),
    )


def _clean_id(raw: object) -> str | None:
    """Coerce a catalog id to a trimmed string, or None when absent/blank."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    return text or None


# Name prefixes that mark a MORE-SPECIFIC variant of a base specialty. Several
# records legitimately share a full name / keyword ("Emergency Room" is the full
# name of both "ER" and "Pediatric ER"; "Intensive Care Unit" of "ICU", "COVID ICU",
# "Trauma ICU"). When a bare résumé phrase collides, the base specialty is the right
# default — so a variant never shadows a base one in the shared indexes.
_SPECIFICITY_PREFIXES: frozenset[str] = frozenset({
    "pediatric", "peds", "paediatric", "adult", "geriatric",
    "covid", "trauma", "neuro", "outpatient", "inpatient",
})


def _is_base_name(name: str) -> bool:
    parts = _match_key(name).split()
    return not (parts and parts[0] in _SPECIFICITY_PREFIXES)


def _prefer(new: SpecialtyRecord, existing: SpecialtyRecord) -> bool:
    """True when `new` should replace `existing` for a shared full-name/keyword key.

    A base specialty beats a more-specific variant; within the same class the
    shorter (fewer-word, then fewer-char) name wins, so the plainest specialty is the
    default for an ambiguous phrase. Deterministic — independent of catalog order.
    """
    new_base, old_base = _is_base_name(new.name), _is_base_name(existing.name)
    if new_base != old_base:
        return new_base
    new_words, old_words = len(new.name.split()), len(existing.name.split())
    if new_words != old_words:
        return new_words < old_words
    return len(new.name) < len(existing.name)


def _build_indexes(records: list[SpecialtyRecord]) -> SpecialtyCatalog:
    by_name: dict[str, SpecialtyRecord] = {}
    by_full: dict[str, SpecialtyRecord] = {}
    by_keyword: dict[str, SpecialtyRecord] = {}
    by_prof_name: dict[tuple[str, str], SpecialtyRecord] = {}
    by_prof_full: dict[tuple[str, str], SpecialtyRecord] = {}
    by_prof_keyword: dict[tuple[str, str], SpecialtyRecord] = {}
    profession_ids: dict[str, str] = {}

    def put(index: dict, key: object, rec: SpecialtyRecord) -> None:
        """Insert unless a better (base / plainer) record already holds the key."""
        cur = index.get(key)
        if cur is None or _prefer(rec, cur):
            index[key] = rec

    for rec in records:
        name_key = _match_key(rec.name)
        full_key = _match_key(rec.full_name) if rec.full_name else None
        keyword_keys = [_match_key(kw) for kw in rec.keywords]
        prof_keys = profession_keys(rec.profession)

        # A specialty NAME is the strongest signal — keep it first-wins so a curated
        # earlier row isn't reordered. Full names and keywords are frequently shared
        # across a base specialty and its variants, so those resolve the collision in
        # favour of the base specialty (`put`).
        by_name.setdefault(name_key, rec)
        if full_key:
            put(by_full, full_key, rec)
        for kk in keyword_keys:
            put(by_keyword, kk, rec)

        # Profession-scoped: the same name maps to a different id per profession,
        # so key on (profession, value) to pick the id for a role's credential.
        for pk in prof_keys:
            by_prof_name.setdefault((pk, name_key), rec)
            if full_key:
                put(by_prof_full, (pk, full_key), rec)
            for kk in keyword_keys:
                put(by_prof_keyword, (pk, kk), rec)
            if rec.profession_id:
                profession_ids.setdefault(pk, rec.profession_id)

    return SpecialtyCatalog(
        records=records,
        by_name_key=by_name,
        by_full_key=by_full,
        by_keyword_key=by_keyword,
        by_prof_name_key=by_prof_name,
        by_prof_full_key=by_prof_full,
        by_prof_keyword_key=by_prof_keyword,
        profession_ids=profession_ids,
    )
