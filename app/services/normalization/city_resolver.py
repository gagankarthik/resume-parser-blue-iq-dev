"""
City id enrichment — opt-in, live, best-effort.

The cities endpoint is a per-lookup fuzzy search (not bulk reference data), so —
like the tier-4 specialty AI resolution — it runs as an async enrichment AFTER the
deterministic normalization, not on the offline path. For each role that has a city
plus a resolved ``country_id`` + ``state_id`` (stamped offline by
``geography_matcher``), it queries ``/cities`` and stamps the best match's id onto
``city_id`` with the API's ``score`` as ``city_confidence``.

Guardrails:
  • Gated by ``settings.enable_city_api_match`` (default off) AND a configured API
    key — the default request path makes NO network call and burns no partner quota.
  • Distinct ``(country_id, state_id, city)`` lookups are de-duplicated within one
    résumé, and the total is capped (``settings.city_api_max_lookups``).
  • A match below ``CITY_ACCEPT_MIN`` is left unmatched (city_id null) for review
    rather than stamping a low-confidence guess.
  • Any failure is swallowed — city enrichment never fails a parse.
"""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI
from app.services.normalization import city_api
from app.services.normalization.healthcare_taxonomy import _match_key

log = get_logger(__name__)

# Minimum API score for a city match to be trusted; below it the role is left
# unmatched (city_id null) rather than stamped with a low-confidence guess.
CITY_ACCEPT_MIN = 0.5


async def resolve_cities(parsed: ParsedResumeAI) -> int:
    """Stamp platform ``city_id`` + confidence onto roles via the live cities API.

    Returns the number of distinct API lookups performed (0 when disabled / nothing
    to do). Best-effort: mutates ``parsed.experience[*]`` in place; any error leaves
    the deterministic result untouched.
    """
    settings = get_settings()
    api_key = settings.gig_specialties_api_key
    if not settings.enable_city_api_match or not api_key:
        return 0

    # Gather the roles that can be resolved: a city plus both geography ids (set
    # offline by the geography matcher). Group by the lookup key so identical
    # city/state/country triples cost a single API call.
    pending: dict[tuple[str, str, str], list] = {}
    for exp in parsed.experience:
        if exp.city_id is not None:
            continue  # already resolved (e.g. a DynamoDB reload)
        city = (exp.city or "").strip()
        if not city or not exp.country_id or not exp.state_id:
            continue
        key = (exp.country_id, exp.state_id, _match_key(city))
        pending.setdefault(key, []).append(exp)
    if not pending:
        return 0

    keys = list(pending)[: max(0, settings.city_api_max_lookups)]
    if not keys:
        return 0

    lookups = 0
    async with httpx.AsyncClient() as client:
        for country_id, state_id, _city_key in keys:
            exps = pending[(country_id, state_id, _city_key)]
            city_name = (exps[0].city or "").strip()
            matches = await city_api.search(
                client, settings.gig_cities_api_url, api_key,
                country_id=country_id, state_id=state_id, city_name=city_name,
            )
            lookups += 1
            best = matches[0] if matches else None
            if best is None or best.score < CITY_ACCEPT_MIN:
                continue
            for exp in exps:
                exp.city_id = best.id
                exp.city_confidence = best.score

    log.info("city_api_tier", lookups=lookups, groups=len(keys))
    return lookups
