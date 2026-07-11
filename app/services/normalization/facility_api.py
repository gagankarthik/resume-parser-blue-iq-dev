"""
GigHealth facilities API client + catalog transform.

The placement platform ("Gig") exposes its facility directory at
``GET /api/v1/external/facilities`` (auth: ``x-api-key``). Unlike the specialties
tree, the facilities response is a FLAT list already::

    data[] → {id, name, healthSystemId, healthSystemName}

``healthSystemId`` / ``healthSystemName`` may be null (an independent facility with
no parent health system). This module normalises each entry into the flat record
shape the facility catalog loader consumes — ``{id, name, health_system,
health_system_id}`` — **preserving the platform's exact facility names**.

Mirrors ``specialty_api``: the flatten/transform is pure and unit-testable, and the
network fetch (`fetch_payload`) is only used by
``scripts/refresh_facility_catalog.py`` to regenerate the bundled snapshot
(``app/data/facility_catalog.json``) — never on the request hot path. The parser
loads the committed snapshot so a Lambda invocation makes no live HTTP call.
"""

from __future__ import annotations

import httpx

DEFAULT_API_URL = "https://api.gighealth.com/api/v1/external/facilities"


def fetch_payload(api_url: str, api_key: str, *, timeout: float = 30.0) -> dict:
    """GET the Gig facilities API and return the decoded JSON envelope.

    Raises on HTTP error / non-JSON so the refresh script fails loudly — the
    request path never calls this (it reads the bundled snapshot).
    """
    resp = httpx.get(api_url, headers={"x-api-key": api_key}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def flatten_payload(payload: dict) -> list[dict]:
    """Flatten the Gig facilities envelope into catalog rows (order preserved)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        row = _row(item)
        if row is not None:
            rows.append(row)
    return rows


def _row(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    fac_id = item.get("id")
    name = _clean(item.get("name"))
    if fac_id is None or not name:
        return None
    return {
        "id": str(fac_id),
        "name": name,                                  # EXACT platform name — never re-worded
        "health_system": _clean(item.get("healthSystemName")),
        "health_system_id": _id(item.get("healthSystemId")),
    }


def _id(raw: object) -> str | None:
    return str(raw) if raw is not None and not isinstance(raw, bool) else None


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None
