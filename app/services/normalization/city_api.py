"""
GigHealth cities API client - live fuzzy city search.

Unlike geographies/facilities/specialties, the cities endpoint is NOT bulk
reference data that can be snapshotted: it fuzzy-matches a single city name WITHIN
a given state and returns the top matches ordered by descending score::

    GET /api/v1/external/cities?countryId={id}&stateId={id}&cityName={name}
    -> data[] {id, city, stateId, state, statecode, countryId, score}

``countryId`` / ``stateId`` come from the geographies catalog (resolved offline by
``geography_matcher``); ``score`` is a 0-1 match confidence. Because this is a
per-lookup matching call (and counts against the partner monthly quota), it is used
only by the opt-in ``city_resolver`` enrichment - never bulk-cached - and the async
client here keeps the transport concern out of the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.services.normalization import gig_api

DEFAULT_API_URL = "https://api.gighealth.com/api/v1/external/cities"


@dataclass(frozen=True)
class CityMatch:
    """One fuzzy city match returned by the cities endpoint."""

    id:         str
    city:       str
    state_id:   str | None
    state:      str | None
    statecode:  str | None
    country_id: str | None
    score:      float


def parse_matches(payload: dict) -> list[CityMatch]:
    """Parse the cities envelope into ordered ``CityMatch`` records (pure/testable).

    Rows missing an id or city are dropped; ``score`` is coerced to a 0-1 float
    (defaulting to 0.0). Order is preserved (the API returns them best-first).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[CityMatch] = []
    for row in data:
        m = _row(row)
        if m is not None:
            out.append(m)
    return out


def rows_to_matches(rows: list[dict]) -> list[CityMatch]:
    """Convert already-unwrapped envelope rows into ordered ``CityMatch`` records."""
    return [m for m in (_row(r) for r in rows) if m is not None]


def _row(row: object) -> CityMatch | None:
    if not isinstance(row, dict):
        return None
    cid = row.get("id")
    city = row.get("city")
    if cid is None or not isinstance(city, str) or not city.strip():
        return None
    return CityMatch(
        id=str(cid),
        city=city.strip(),
        state_id=_id(row.get("stateId")),
        state=_clean(row.get("state")),
        statecode=_clean(row.get("statecode")),
        country_id=_id(row.get("countryId")),
        score=_score(row.get("score")),
    )


async def search(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    *,
    country_id: str,
    state_id: str,
    city_name: str,
    timeout: float = 10.0,
) -> list[CityMatch]:
    """Fuzzy-search cities within a state. Returns matches best-first, or [].

    Raises ``gig_api.GigApiError`` on a failed call so the caller can tell an auth /
    permission / quota problem apart from a genuine no-match. This used to swallow
    every HTTP error into an empty list, which made a missing API key look exactly
    like "no city matched" - the resolver's caller is responsible for degrading
    gracefully, but it must degrade with a reason.
    """
    rows = await gig_api.get_async(
        client, api_url, api_key,
        params={"countryId": country_id, "stateId": state_id, "cityName": city_name},
        timeout=timeout,
    )
    return rows_to_matches(rows)


def _id(raw: object) -> str | None:
    return str(raw) if raw is not None and not isinstance(raw, bool) else None


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _score(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return 0.0
    try:
        return min(max(float(raw), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0
