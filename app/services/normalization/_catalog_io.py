"""
Shared file loader for the offline reference catalogs (specialty, facility, geography).

Every catalog is OPTIONAL by design: when the path is unset or the file is
missing/empty/garbled, ``load_rows`` logs why and returns ``None``, the catalog module
returns an empty catalog, and the matcher leaves the id null. Nothing here raises on a
bad file - a bad catalog must never break parsing.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


def load_rows(
    path: str | None,
    name: str,
    envelope_keys: tuple[str, ...],
    *,
    allow_csv: bool = True,
) -> list | None:
    """Read a catalog file into its raw rows, or ``None`` when there is no usable file.

    ``name`` prefixes the log events (``<name>_catalog_unset`` / ``_missing`` /
    ``_load_failed``). JSON may be a bare list or a dict envelope whose first
    ``envelope_keys`` entry holding a list is used; a ``.csv`` file (when
    ``allow_csv``) is read as a header row plus dict rows.
    """
    if not path:
        log.info(f"{name}_catalog_unset")
        return None

    p = Path(path)
    if not p.is_file():
        log.warning(f"{name}_catalog_missing", path=str(p))
        return None

    try:
        return _read_rows(p, envelope_keys, allow_csv)
    except Exception as exc:  # never let a bad catalog break parsing
        log.warning(f"{name}_catalog_load_failed", path=str(p), error=str(exc))
        return None


def _read_rows(p: Path, envelope_keys: tuple[str, ...], allow_csv: bool) -> list:
    text = p.read_text(encoding="utf-8-sig")
    if allow_csv and p.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))

    data = json.loads(text)
    if isinstance(data, dict):
        for key in envelope_keys:
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []
