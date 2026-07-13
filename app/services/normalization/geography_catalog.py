"""
Geography reference catalog - the source of truth for platform country/state IDs.

Loads the bundled geographies snapshot (``settings.geography_catalog_path``,
generated from the Gig geographies API by
``scripts/refresh_geography_catalog.py``) and exposes the lookup indexes the
geography matcher needs to resolve a resume's country/state string to a platform
``countryId`` / ``stateId``:

  * countries - indexed by punctuation/case-insensitive name and by ISO code;
  * states    - indexed BOTH flat (first-wins across all countries) and scoped by
    ``(country_id, key)`` on the statecode and the state name, so a state resolves
    unambiguously once its country is known ("NY" -> 35 within the US) and still
    resolves flat when it is not.

Design mirrors ``specialty_catalog`` / ``facility_catalog``: loaded once and cached,
``reload()`` for tests, and OPTIONAL - a missing/garbled snapshot yields an empty
catalog (logged once) and the matcher simply leaves the ids null. A bad catalog
never breaks parsing.

Snapshot record shape (per country), extra keys ignored::

    {"id": "1", "country": "United States", "code": "US",
     "states": [{"id": "35", "state": "New York", "statecode": "NY"}, ...]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.normalization.healthcare_taxonomy import _match_key

log = get_logger(__name__)


@dataclass(frozen=True)
class StateRecord:
    """One catalog state/province, carrying its parent country id."""

    id:         str
    name:       str
    statecode:  str | None
    country_id: str


@dataclass(frozen=True)
class CountryRecord:
    """One catalog country."""

    id:      str
    name:    str
    code:    str | None = None


# Common ways a resume writes a country, mapped to the platform ISO code the
# geographies snapshot carries. Keys are _match_key-normalized (lowercase; "&"->
# "and"). Lets "USA" / "U.S.A." / "America" resolve to the "US" country record.
_COUNTRY_CODE_ALIASES: dict[str, str] = {
    "usa": "US",
    "us": "US",
    "u s a": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "u k": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "uae": "AE",
}


@dataclass
class GeographyCatalog:
    """Loaded catalog plus the country/state lookup indexes the matcher needs."""

    countries:      list[CountryRecord] = field(default_factory=list)
    states:         list[StateRecord]   = field(default_factory=list)
    country_by_name:  dict[str, CountryRecord] = field(default_factory=dict)
    country_by_code:  dict[str, CountryRecord] = field(default_factory=dict)
    # Flat, country-agnostic state indexes (first-wins).
    state_by_code:    dict[str, StateRecord] = field(default_factory=dict)
    state_by_name:    dict[str, StateRecord] = field(default_factory=dict)
    # Country-scoped state indexes, keyed (country_id, value_key).
    state_by_country_code: dict[tuple[str, str], StateRecord] = field(default_factory=dict)
    state_by_country_name: dict[tuple[str, str], StateRecord] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.countries and not self.states


# Module-level cache. None = not yet loaded this process.
_catalog: GeographyCatalog | None = None


def get_catalog() -> GeographyCatalog:
    """Return the cached catalog, loading it on first use."""
    global _catalog
    if _catalog is None:
        _catalog = _load(get_settings().geography_catalog_path)
    return _catalog


def reload(path: str | None = None) -> GeographyCatalog:
    """Force a reload (used by tests). Falls back to the configured path."""
    global _catalog
    if path is None:
        path = get_settings().geography_catalog_path
    _catalog = _load(path)
    return _catalog


def _load(path: str | None) -> GeographyCatalog:
    if not path:
        log.info("geography_catalog_unset")
        return GeographyCatalog()

    p = Path(path)
    if not p.is_file():
        log.warning("geography_catalog_missing", path=str(p))
        return GeographyCatalog()

    try:
        rows = _read_rows(p)
    except Exception as exc:  # never let a bad catalog break parsing
        log.warning("geography_catalog_load_failed", path=str(p), error=str(exc))
        return GeographyCatalog()

    catalog = _build_indexes(rows)
    log.info(
        "geography_catalog_loaded", path=str(p),
        countries=len(catalog.countries), states=len(catalog.states),
    )
    return catalog


def _read_rows(p: Path) -> list[dict]:
    """Parse the geographies snapshot into a list of country dicts."""
    text = p.read_text(encoding="utf-8-sig")
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("geographies", "countries", "records", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _build_indexes(rows: list[dict]) -> GeographyCatalog:
    countries: list[CountryRecord] = []
    states: list[StateRecord] = []
    country_by_name: dict[str, CountryRecord] = {}
    country_by_code: dict[str, CountryRecord] = {}
    state_by_code: dict[str, StateRecord] = {}
    state_by_name: dict[str, StateRecord] = {}
    state_by_country_code: dict[tuple[str, str], StateRecord] = {}
    state_by_country_name: dict[tuple[str, str], StateRecord] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("id")
        cname = _clean(row.get("country"))
        if cid is None or not cname:
            continue
        country_id = str(cid).strip()
        code = _clean(row.get("code"))
        country = CountryRecord(id=country_id, name=cname, code=code)
        countries.append(country)
        country_by_name.setdefault(_match_key(cname), country)
        if code:
            country_by_code.setdefault(code.upper(), country)

        for st in row.get("states") or []:
            if not isinstance(st, dict):
                continue
            sid = st.get("id")
            sname = _clean(st.get("state"))
            if sid is None or not sname:
                continue
            statecode = _clean(st.get("statecode"))
            state = StateRecord(
                id=str(sid).strip(), name=sname,
                statecode=statecode, country_id=country_id,
            )
            states.append(state)
            name_key = _match_key(sname)
            state_by_name.setdefault(name_key, state)
            state_by_country_name.setdefault((country_id, name_key), state)
            if statecode:
                code_key = statecode.upper()
                state_by_code.setdefault(code_key, state)
                state_by_country_code.setdefault((country_id, code_key), state)

    return GeographyCatalog(
        countries=countries,
        states=states,
        country_by_name=country_by_name,
        country_by_code=country_by_code,
        state_by_code=state_by_code,
        state_by_name=state_by_name,
        state_by_country_code=state_by_country_code,
        state_by_country_name=state_by_country_name,
    )


def country_aliases() -> dict[str, str]:
    """Match-key -> ISO code aliases for common resume spellings of a country."""
    return _COUNTRY_CODE_ALIASES
