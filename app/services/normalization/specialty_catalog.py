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
     "keywords": ["med surg", "ms/tele"], "group": "Med Surg / Tele"}
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

    id:        str
    name:      str
    full_name: str | None = None
    group:     str | None = None
    keywords:  tuple[str, ...] = ()


@dataclass
class SpecialtyCatalog:
    """Loaded catalog plus the lookup indexes the matcher needs."""

    records:       list[SpecialtyRecord]   = field(default_factory=list)
    by_name_key:   dict[str, SpecialtyRecord] = field(default_factory=dict)
    by_full_key:   dict[str, SpecialtyRecord] = field(default_factory=dict)
    by_keyword_key: dict[str, SpecialtyRecord] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.records


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
    )


def _build_indexes(records: list[SpecialtyRecord]) -> SpecialtyCatalog:
    by_name: dict[str, SpecialtyRecord] = {}
    by_full: dict[str, SpecialtyRecord] = {}
    by_keyword: dict[str, SpecialtyRecord] = {}

    for rec in records:
        # First write wins so an earlier (more canonical) row is not shadowed by a
        # later duplicate spelling.
        by_name.setdefault(_match_key(rec.name), rec)
        if rec.full_name:
            by_full.setdefault(_match_key(rec.full_name), rec)
        for kw in rec.keywords:
            by_keyword.setdefault(_match_key(kw), rec)

    return SpecialtyCatalog(
        records=records,
        by_name_key=by_name,
        by_full_key=by_full,
        by_keyword_key=by_keyword,
    )
