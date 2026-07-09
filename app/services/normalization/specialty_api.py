"""
GigHealth specialties API client + catalog transform.

The placement platform ("Gig") exposes its specialty taxonomy at
``GET /api/v1/external/specialities`` (auth: ``x-api-key``). The response nests
specialties four levels deep::

    data[]              → clinical category   ("Nursing", "Allied Health")
      professions[]     → profession / credential ("RN", "CNA", "LPN/ LVN", ...)
        specialityGroups[]      → group ("ICU", "Med Surg", ...)
          specialities[]        → the specialty  {id, name, fullName}
        ungroupedSpecialities[] → specialties with no group

This module flattens that tree into the FLAT record shape the specialty catalog
loader consumes — ``{id, specialty, full_name, group, profession, keywords[]}`` —
**preserving the platform's exact specialty names** (we never re-word "Med Surg"
into "Med-Surg" etc.). It is deliberately import-light: the flatten/transform is
pure and unit-testable, and the network fetch (`fetch_payload`) is only used by
``scripts/refresh_specialty_catalog.py`` to regenerate the bundled snapshot — never
on the request hot path.

Why a snapshot instead of a live fetch per parse: the catalog is ~1k records that
change rarely, and the parser runs in Lambda where a per-invocation HTTP call would
add latency and a hard network dependency. The snapshot (``app/data/
specialty_catalog.json``) is bundled into the image and refreshed on demand.

The same specialty NAME (e.g. "ICU") carries a DIFFERENT id under each profession
(RN ICU=56, CNA ICU=757), so every record keeps its `profession`; the matcher uses
it to pick the right id for a role's credential.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

DEFAULT_API_URL = "https://api.gighealth.com/api/v1/external/specialities"


# ── Curated keyword overlay ───────────────────────────────────────────────────
# Extra search phrases that let the deterministic keyword tier differentiate
# sub-types the platform records only as an acronym (the task's "burn → BICU, ICU
# variants, PICU"). Keyed by the EXACT platform specialty name; applied to that
# specialty under EVERY profession. Kept intentionally SPECIFIC/multi-word so a
# phrase maps to exactly one specialty (the catalog's keyword index is first-wins,
# so a generic word like "medical" or "pediatric" would collide — avoid those).
CURATED_KEYWORDS: dict[str, tuple[str, ...]] = {
    # ICU family — the differentiator is the modifier, not "ICU".
    "BICU":        ("burn icu", "burn intensive care", "burn unit"),
    "MICU":        ("medical icu",),
    "SICU":        ("surgical icu",),
    "CTICU":       ("cardiothoracic icu", "cardio thoracic icu"),
    "CVICU":       ("cardiovascular icu", "cardiac icu"),
    "CVCC":        ("cardiovascular critical care",),
    "CCU":         ("coronary care unit",),
    "Neuro ICU":   ("neuro icu", "neurological icu", "neuroscience icu"),
    "Trauma ICU":  ("trauma icu", "ticu"),
    "PICU":        ("pediatric icu", "peds icu", "paediatric icu"),
    "NICU":        ("neonatal icu", "newborn icu"),
    "CVPICU":      ("cardiovascular picu", "cardiac picu"),
    # Stepdown / intermediate.
    "DOU":         ("definitive observation unit",),
    "PCU":         ("progressive care unit",),
    "IMC":         ("intermediate care unit", "intermediate care"),
    "IMCU":        ("intermediate care unit", "intermediate care"),
    "TCU":         ("transitional care unit",),
    # Peri-operative.
    "PACU":        ("post anesthesia care unit", "recovery room"),
    "PreOp":       ("pre op", "pre operative", "preoperative"),
    "CVOR":        ("cardiovascular or", "cardiovascular operating room"),
    "CTOR":        ("cardio thoracic or", "cardiothoracic operating room"),
    # Med-Surg / Tele shorthand résumés commonly write out.
    "Med Surg":       ("medical surgical", "med/surg"),
    "Med Surg/ Tele": ("med surg tele", "medical surgical telemetry", "ms/tele"),
    "Telemetry":      ("tele",),
    # Emergency / other common expansions.
    "Correctional": ("corrections", "correctional facility"),
    "ER":          ("emergency department", "emergency dept"),
    "EP Lab":      ("electrophysiology lab",),
    "GI Lab":      ("gastrointestinal lab", "gi/endo"),
    "L&D":         ("labor delivery",),
    "OB/GYN":      ("obstetrics gynecology", "ob gyn"),
}


def keywords_for(name: str) -> tuple[str, ...]:
    """Curated extra keywords for a specialty name (empty tuple when none)."""
    return CURATED_KEYWORDS.get(name.strip(), ())


def fetch_payload(api_url: str, api_key: str, *, timeout: float = 30.0) -> dict:
    """GET the Gig specialties API and return the decoded JSON envelope.

    Raises on HTTP error / non-JSON so the refresh script fails loudly — the
    request path never calls this (it reads the bundled snapshot).
    """
    resp = httpx.get(api_url, headers={"x-api-key": api_key}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def flatten_payload(payload: dict) -> list[dict]:
    """Flatten the Gig API envelope into catalog rows (order preserved)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return list(_iter_rows(data))


def _iter_rows(categories: list) -> Iterator[dict]:
    for category in categories:
        if not isinstance(category, dict):
            continue
        cat = {"category": _clean(category.get("name")), "category_id": _id(category)}
        for profession in category.get("professions") or []:
            if not isinstance(profession, dict):
                continue
            prof = {**cat, "profession": _clean(profession.get("name")),
                    "profession_id": _id(profession)}
            for group in profession.get("specialityGroups") or []:
                if not isinstance(group, dict):
                    continue
                ctx = {**prof, "group": _clean(group.get("name")), "group_id": _id(group)}
                for spec in group.get("specialities") or []:
                    row = _row(spec, ctx)
                    if row is not None:
                        yield row
            ungrouped = {**prof, "group": None, "group_id": None}
            for spec in profession.get("ungroupedSpecialities") or []:
                row = _row(spec, ungrouped)
                if row is not None:
                    yield row


def _row(spec: object, ctx: dict) -> dict | None:
    if not isinstance(spec, dict):
        return None
    spec_id = spec.get("id")
    name = _clean(spec.get("name"))
    if spec_id is None or not name:
        return None
    return {
        "id": str(spec_id),
        "specialty": name,                      # EXACT platform name — never re-worded
        "full_name": _clean(spec.get("fullName")),
        "group": ctx.get("group"),
        "group_id": ctx.get("group_id"),
        "profession": ctx.get("profession"),
        "profession_id": ctx.get("profession_id"),
        "category": ctx.get("category"),
        "category_id": ctx.get("category_id"),
        "keywords": list(keywords_for(name)),
    }


def _id(obj: dict) -> str | None:
    raw = obj.get("id")
    return str(raw) if raw is not None else None


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None
