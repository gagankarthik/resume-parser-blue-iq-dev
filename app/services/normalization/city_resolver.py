"""City id enrichment — live, best-effort, and loud when it cannot run.

The cities endpoint is a per-lookup fuzzy search (not bulk reference data), so — like
the tier-4 specialty AI resolution — it runs as an async enrichment AFTER the
deterministic normalization, not on the offline path. For each role that has a city
plus a resolved ``country_id`` + ``state_id`` (stamped offline by ``geography_matcher``),
it queries ``/cities`` and stamps the best match's id onto ``city_id`` with the API's
``score`` as ``city_confidence``.

Guardrails:
  • Gated by ``settings.enable_city_api_match`` AND a configured API key. When either
    is absent this is a no-op — but it now says so in the logs. It used to return
    silently, which meant a Lambda deployed without ``GIG_SPECIALTIES_API_KEY``
    produced ``city_id: null`` on every role and looked exactly like a parser bug.
  • Distinct ``(country_id, state_id, city)`` lookups are de-duplicated within one
    résumé AND cached across résumés on a warm worker (the partner guide asks
    integrators to cache rather than call on every transaction — city rows are
    slow-changing and every call counts against the monthly quota).
  • A match below ``CITY_ACCEPT_MIN`` is left unmatched (city_id null) for review
    rather than stamping a low-confidence guess.
  • A failed call is logged with its cause (auth / forbidden / rate_limited / …) and
    leaves the deterministic result intact — enrichment never fails a parse.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI
from app.services.normalization import city_api, gig_api
from app.services.normalization.healthcare_taxonomy import _match_key

log = get_logger(__name__)

# Minimum API score for a city match to be trusted.
#
# Measured against the live endpoint (see the partner guide's own example, and a direct
# probe of this résumé's cities):
#
#     "Buffalo"       -> Buffalo         score 1.0      exact
#     "Williamsville" -> Williamsville   score 1.0      exact
#     "Newport News"  -> Newport News    score 1.0      exact
#     "New Yrok"      -> New York        score 0.385    typo of the RIGHT city
#     "Williamsville" -> Willisville     score 0.6      the WRONG city
#     "Williamsville" -> Williamson      score 0.5      the WRONG city
#
# The score cannot separate "a typo of the right city" (0.385) from "a similar-looking
# wrong city" (0.5–0.6) — the wrong answers actually score HIGHER. So a fuzzy band is
# not safely usable in either direction, and the old 0.5 floor would stamp `Williamson`
# onto a résumé whose real city simply wasn't in that state's list. A wrong city_id is
# worse than a null one: it silently corrupts placement data, and it violates the
# never-fabricate-an-id invariant the specialty and facility matchers both hold.
#
# So: accept exact / near-exact only, and leave anything else null for human review.
CITY_ACCEPT_MIN = 0.9

# Cap concurrent city lookups so a résumé with many distinct cities can't open an
# unbounded number of sockets or trip the partner's per-second rate limit (a 429).
_MAX_CONCURRENCY = 4

# Process-level cache of resolved lookups, shared across résumés on a warm worker.
# Bounded, and negative results are cached too — a city that does not resolve will not
# resolve on the next résumé either, and re-asking just burns quota.
_CACHE: OrderedDict[tuple[str, str, str], tuple[str, float] | None] = OrderedDict()
_CACHE_MAX = 2048


def _cache_get(key: tuple[str, str, str]) -> tuple[str, float] | None | object:
    """Return the cached value, or ``_MISS`` when the key has never been looked up.

    A cached *negative* (None) is a real answer, so it must be distinguishable from a
    cache miss — hence the sentinel rather than a bare None.
    """
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return _MISS


_MISS = object()


def _cache_put(key: tuple[str, str, str], value: tuple[str, float] | None) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _reset_cache() -> None:
    """Test hook — the cache is process-global and would leak between tests."""
    _CACHE.clear()


async def resolve_cities(parsed: ParsedResumeAI) -> int:
    """Stamp platform ``city_id`` + confidence onto roles via the live cities API.

    Returns the number of distinct API lookups performed (0 when disabled, unkeyed, or
    fully served from cache). Best-effort: mutates ``parsed.experience[*]`` in place;
    any error leaves the deterministic result untouched.
    """
    settings = get_settings()
    api_key = settings.gig_specialties_api_key

    # Say so. A silent return here is what made a missing key look like a parser bug:
    # every role came back city_id=null with nothing in the logs to explain it.
    if not settings.enable_city_api_match:
        log.info("city_api_disabled", reason="enable_city_api_match is false")
        return 0
    if not api_key:
        log.warning(
            "city_api_no_key",
            reason="GIG_SPECIALTIES_API_KEY is not configured — city_id will be null on "
                   "every role. Country/state ids still resolve offline.",
        )
        return 0

    # Gather the roles that can be resolved: a city plus both geography ids (set offline
    # by the geography matcher). Group by lookup key so identical triples cost one call.
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

    def _apply(key: tuple[str, str, str], hit: tuple[str, float] | None) -> None:
        if hit is None:
            return
        city_id, score = hit
        for exp in pending[key]:
            exp.city_id = city_id
            exp.city_confidence = score

    # Serve what we can from the cross-résumé cache before spending any quota.
    to_fetch: list[tuple[str, str, str]] = []
    cached = 0
    for key in pending:
        hit = _cache_get(key)
        if hit is _MISS:
            to_fetch.append(key)
        else:
            cached += 1
            _apply(key, hit)

    to_fetch = to_fetch[: max(0, settings.city_api_max_lookups)]
    if not to_fetch:
        if cached:
            log.info("city_api_tier", lookups=0, cached=cached)
        return 0

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    stats = {"matched": 0, "no_match": 0, "below_threshold": 0, "failed": 0}

    async def _resolve(client: httpx.AsyncClient, key: tuple[str, str, str]) -> None:
        country_id, state_id, _city_key = key
        city_name = (pending[key][0].city or "").strip()
        try:
            async with sem:
                matches = await city_api.search(
                    client, settings.gig_cities_api_url, api_key,
                    country_id=country_id, state_id=state_id, city_name=city_name,
                )
        except gig_api.GigApiError as exc:
            # Loud, and classified: `auth` means the key is bad, `forbidden` means the
            # key lacks the `cities` permission, `rate_limited` means quota. These are
            # three completely different fixes and used to be one silent empty list.
            stats["failed"] += 1
            log.warning(
                "city_api_lookup_failed",
                kind=exc.kind, status=exc.status, error=exc.message,
                country_id=country_id, state_id=state_id,
            )
            return  # not cached: a transient/auth failure is not an answer about the city

        best = matches[0] if matches else None
        if best is None:
            stats["no_match"] += 1
            _cache_put(key, None)
            return
        if best.score < CITY_ACCEPT_MIN:
            stats["below_threshold"] += 1
            log.info(
                "city_api_below_threshold",
                city=city_name, best=best.city, score=best.score, floor=CITY_ACCEPT_MIN,
            )
            _cache_put(key, None)
            return

        hit = (best.id, best.score)
        _cache_put(key, hit)
        _apply(key, hit)
        stats["matched"] += 1

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_resolve(client, k) for k in to_fetch))

    log.info("city_api_tier", lookups=len(to_fetch), cached=cached, **stats)
    return len(to_fetch)
