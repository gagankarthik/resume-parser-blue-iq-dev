"""
Geography -> catalog-id matcher (offline country + state resolution).

Resolves a resume's country/state strings to platform ids + confidence against the
bundled geographies snapshot - deterministically and with no network call:

  * resolve_country("United States" | "USA" | "US")  -> countryId 1, conf 1.0
  * resolve_state("NY" | "New York", country_id="1")  -> stateId 35,  conf 1.0

Country/state names and codes are canonical reference data, so a match is exact
(confidence 1.0) or absent (id ``None``, confidence 0.0 - surfaced for review,
never guessed). State resolution is scoped to the resolved country when known, so a
statecode shared across countries resolves to the right one, and falls back to the
flat (first-wins) index otherwise. When no catalog is loaded, every lookup is a
graceful miss.

The platform ``cityId`` is NOT resolved here - cities are a live fuzzy-search
endpoint (see ``city_resolver``); these two ids feed that search as its
``countryId`` / ``stateId`` inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.normalization.geography_catalog import (
    GeographyCatalog,
    get_catalog,
)
from app.services.normalization.healthcare_taxonomy import _match_key

CONF_EXACT     = 1.0
CONF_UNMATCHED = 0.0

_PLACEHOLDERS = frozenset({"", "unknown", "n/a", "none", "null"})


@dataclass(frozen=True)
class GeoMatch:
    """Result of resolving one country/state string against the catalog."""

    id:          str | None = None
    name:        str | None = None
    code:        str | None = None      # country ISO code / statecode
    country_id:  str | None = None      # set on a state match
    confidence:  float = CONF_UNMATCHED
    matched:     bool = False


_UNMATCHED = GeoMatch()


def _blank(value: str | None) -> bool:
    return not value or value.strip().lower() in _PLACEHOLDERS


def resolve_country(country: str | None, *, catalog: GeographyCatalog | None = None) -> GeoMatch:
    """Resolve a country string to a platform country id + confidence."""
    if _blank(country):
        return _UNMATCHED
    cat = catalog or get_catalog()
    text = (country or "").strip()

    rec = cat.country_by_name.get(_match_key(text))
    if rec is None:
        rec = cat.country_by_code.get(text.upper())
    if rec is None:
        # Common spelling alias ("USA" -> code "US").
        from app.services.normalization.geography_catalog import country_aliases
        code = country_aliases().get(_match_key(text))
        if code:
            rec = cat.country_by_code.get(code.upper())
    if rec is None:
        return _UNMATCHED
    return GeoMatch(id=rec.id, name=rec.name, code=rec.code,
                    confidence=CONF_EXACT, matched=True)


def resolve_state(
    state: str | None,
    country_id: str | None = None,
    *,
    catalog: GeographyCatalog | None = None,
) -> GeoMatch:
    """Resolve a state string to a platform state id + confidence.

    Scoped to ``country_id`` when supplied (so a statecode shared across countries
    resolves to the right one), else uses the flat first-wins index. A state written
    as its code ("NY") or its full name ("New York") both resolve.
    """
    if _blank(state):
        return _UNMATCHED
    cat = catalog or get_catalog()
    text = (state or "").strip()
    code_key = text.upper()
    name_key = _match_key(text)

    if country_id:
        # Country is known - resolve ONLY within it. Falling back to the flat index
        # here could return a different country's state (e.g. "Alberta" under a US
        # country_id), yielding an inconsistent (country_id, state) pair.
        rec = (cat.state_by_country_code.get((country_id, code_key))
               or cat.state_by_country_name.get((country_id, name_key)))
    else:
        # Country unknown - use the flat, first-wins index across all countries.
        rec = cat.state_by_code.get(code_key) or cat.state_by_name.get(name_key)
    if rec is None:
        return _UNMATCHED
    return GeoMatch(id=rec.id, name=rec.name, code=rec.statecode,
                    country_id=rec.country_id, confidence=CONF_EXACT, matched=True)
