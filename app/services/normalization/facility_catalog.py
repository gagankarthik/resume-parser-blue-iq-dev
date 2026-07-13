"""
Facility reference catalog - the source of truth for platform facility IDs.

The platform delivers a directory of facilities, each with an id, the canonical
facility name, and (optionally) its parent health system. This module loads that
directory from ``settings.facility_catalog_path`` (JSON list, or the
``{"facilities": [...]}`` envelope the refresh script writes, or a CSV with the
same columns) and exposes a punctuation/case-insensitive name index keyed off
``healthcare_taxonomy._match_key``, so the facility matcher can resolve a resume
employer/facility string to a catalog id.

Design mirrors ``specialty_catalog`` (loaded once, cached at module level,
``reload()`` for tests) but is simpler - facilities are not profession-scoped, so a
single flat name index plus the record list (for the fuzzy tier) is all the matcher
needs:

  * The catalog is OPTIONAL. When the path is unset or the file is missing/empty,
    ``get_catalog()`` returns an empty catalog (logged once) and the matcher leaves
    ``facility_id`` null - parsing is never broken by a missing/garbled file.

Expected record shape (per facility), extra keys ignored::

    {"id": "3022", "name": "60th Medical Group - Travis AFB",
     "health_system": "Defense Health Agency", "health_system_id": "181"}
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
class FacilityRecord:
    """One catalog facility."""

    id:               str
    name:             str
    health_system:    str | None = None
    health_system_id: str | None = None


@dataclass
class FacilityCatalog:
    """Loaded catalog plus the name index the matcher needs.

    ``by_name_key`` is first-wins (a curated earlier row is never reordered by a
    later duplicate name); ``records`` backs the matcher's fuzzy tier.
    """

    records:     list[FacilityRecord] = field(default_factory=list)
    by_name_key: dict[str, FacilityRecord] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.records


# Module-level cache. None = not yet loaded this process.
_catalog: FacilityCatalog | None = None


def get_catalog() -> FacilityCatalog:
    """Return the cached catalog, loading it on first use."""
    global _catalog
    if _catalog is None:
        _catalog = _load(get_settings().facility_catalog_path)
    return _catalog


def reload(path: str | None = None) -> FacilityCatalog:
    """Force a reload (used by tests). Falls back to the configured path."""
    global _catalog
    if path is None:
        path = get_settings().facility_catalog_path
    _catalog = _load(path)
    return _catalog


def _load(path: str | None) -> FacilityCatalog:
    if not path:
        log.info("facility_catalog_unset")
        return FacilityCatalog()

    p = Path(path)
    if not p.is_file():
        log.warning("facility_catalog_missing", path=str(p))
        return FacilityCatalog()

    try:
        rows = _read_rows(p)
    except Exception as exc:  # never let a bad catalog break parsing
        log.warning("facility_catalog_load_failed", path=str(p), error=str(exc))
        return FacilityCatalog()

    records = [r for row in rows if (r := _to_record(row)) is not None]
    catalog = _build_indexes(records)
    log.info("facility_catalog_loaded", path=str(p), count=len(records))
    return catalog


def _read_rows(p: Path) -> list[dict]:
    """Parse the catalog file into a list of raw dict rows (JSON or CSV)."""
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))

    data = json.loads(text)
    # Accept either a bare list or a {"facilities": [...]} envelope.
    if isinstance(data, dict):
        for key in ("facilities", "records", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def _to_record(row: object) -> FacilityRecord | None:
    if not isinstance(row, dict):
        return None
    raw_id = row.get("id") if row.get("id") is not None else row.get("facility_id")
    name = row.get("name") or row.get("facility") or row.get("company")
    if raw_id is None or not isinstance(name, str) or not name.strip():
        return None
    return FacilityRecord(
        id=str(raw_id).strip(),
        name=name.strip(),
        health_system=_clean(row.get("health_system") or row.get("healthSystemName")),
        health_system_id=_clean_id(row.get("health_system_id") or row.get("healthSystemId")),
    )


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _clean_id(raw: object) -> str | None:
    """Coerce a catalog id to a trimmed string, or None when absent/blank."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    return text or None


def _build_indexes(records: list[FacilityRecord]) -> FacilityCatalog:
    by_name: dict[str, FacilityRecord] = {}
    for rec in records:
        # First-wins: the platform can list two facilities with the same display
        # name (different campuses); keep the first so lookup is deterministic and
        # independent of catalog order. The fuzzy tier still sees every record.
        by_name.setdefault(_match_key(rec.name), rec)
    return FacilityCatalog(records=records, by_name_key=by_name)
