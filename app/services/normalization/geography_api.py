"""
GigHealth geographies API client + catalog transform.

The Partner API exposes its country/state reference data at
``GET /api/v1/external/geographies`` (auth: ``x-api-key``, permission
``geographies``). The response nests states under each country::

    data[]                -> country {id, country, code}
      states[]            -> state   {id, state, statecode}

The ``id`` values are the ``countryId`` / ``stateId`` inputs the Cities endpoint
requires - and they are STABLE but NOT sequential (Alaska is id 1, New York id 35),
so they must be read from this endpoint, never assumed.

This is slow-changing REFERENCE data, so - like specialties and facilities - it is
snapshotted (``app/data/geography_catalog.json``) and bundled into the image; the
parser resolves a resume's country/state to a platform id offline, with no live
call on the request path. ``fetch_payload`` is used only by
``scripts/refresh_geography_catalog.py`` to regenerate that snapshot.

The flatten/transform is pure and unit-testable: it normalises each country into
``{id, country, code, states:[{id, state, statecode}]}`` preserving the platform's
exact names and ids.
"""

from __future__ import annotations

from app.services.normalization import gig_api

DEFAULT_API_URL = "https://api.gighealth.com/api/v1/external/geographies"


def fetch_payload(api_url: str, api_key: str, *, timeout: float = 30.0) -> dict:
    """GET the Gig geographies API and return the decoded JSON envelope.

    Raises ``gig_api.GigApiError`` on HTTP error / non-JSON / ``success: false`` so the
    refresh script fails loudly - the request path never calls this (it reads the
    bundled snapshot). 429s (per-second burst or monthly quota) are retried with
    backoff, per the partner guide.
    """
    return gig_api.get_sync_envelope(api_url, api_key, timeout=timeout)


def flatten_payload(payload: dict) -> list[dict]:
    """Normalise the geographies envelope into clean country rows (order preserved).

    Each row is ``{id, country, code, states:[{id, state, statecode}]}``; a country
    or state missing an id/name is dropped rather than emitted half-formed.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for country in data:
        row = _country_row(country)
        if row is not None:
            rows.append(row)
    return rows


def _country_row(country: object) -> dict | None:
    if not isinstance(country, dict):
        return None
    cid = country.get("id")
    name = _clean(country.get("country"))
    if cid is None or not name:
        return None
    states: list[dict] = []
    for st in country.get("states") or []:
        srow = _state_row(st)
        if srow is not None:
            states.append(srow)
    return {
        "id": str(cid),
        "country": name,                              # EXACT platform name
        "code": _clean(country.get("code")),
        "states": states,
    }


def _state_row(state: object) -> dict | None:
    if not isinstance(state, dict):
        return None
    sid = state.get("id")
    name = _clean(state.get("state"))
    if sid is None or not name:
        return None
    return {
        "id": str(sid),
        "state": name,
        "statecode": _clean(state.get("statecode")),
    }


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None
