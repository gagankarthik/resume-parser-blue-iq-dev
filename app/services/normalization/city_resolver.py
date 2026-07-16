"""City id enrichment - live, best-effort, and loud when it cannot run.

The cities endpoint is a per-lookup fuzzy search (not bulk reference data), so - like
the tier-4 specialty AI resolution - it runs as an async enrichment AFTER the
deterministic normalization, not on the offline path. For each role that has a city
plus a resolved ``country_id`` + ``state_id`` (stamped offline by ``geography_matcher``),
it queries ``/cities`` and stamps the best match's id onto ``city_id`` with the API's
``score`` as ``city_confidence``.

Guardrails:
  * Gated by ``settings.enable_city_api_match`` AND a configured API key. When either
    is absent this is a no-op - but it now says so in the logs. It used to return
    silently, which meant a Lambda deployed without ``GIG_SPECIALTIES_API_KEY``
    produced ``city_id: null`` on every role and looked exactly like a parser bug.
  * Distinct ``(country_id, state_id, city)`` lookups are de-duplicated within one
    resume AND cached across resumes on a warm worker (the partner guide asks
    integrators to cache rather than call on every transaction - city rows are
    slow-changing and every call counts against the monthly quota).
  * A match below ``CITY_ACCEPT_MIN`` is left unmatched (city_id null) for review
    rather than stamping a low-confidence guess.
  * A failed call is logged with its cause (auth / forbidden / rate_limited / ...) and
    leaves the deterministic result intact - enrichment never fails a parse.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI
from app.services.normalization import (
    city_api,
    geography_catalog,
    geography_matcher,
    gig_api,
)
from app.services.normalization.healthcare_taxonomy import _match_key

log = get_logger(__name__)

# Minimum API score for a city match to be trusted.
#
# Measured against the live endpoint (see the partner guide's own example, and a direct
# probe of this resume's cities):
#
#     "Buffalo"       -> Buffalo         score 1.0      exact
#     "Williamsville" -> Williamsville   score 1.0      exact
#     "Newport News"  -> Newport News    score 1.0      exact
#     "New Yrok"      -> New York        score 0.385    typo of the RIGHT city
#     "Williamsville" -> Willisville     score 0.6      the WRONG city
#     "Williamsville" -> Williamson      score 0.5      the WRONG city
#
# The score cannot separate "a typo of the right city" (0.385) from "a similar-looking
# wrong city" (0.5-0.6) - the wrong answers actually score HIGHER. So a fuzzy band is
# not safely usable in either direction, and the old 0.5 floor would stamp `Williamson`
# onto a resume whose real city simply wasn't in that state's list. A wrong city_id is
# worse than a null one: it silently corrupts placement data, and it violates the
# never-fabricate-an-id invariant the specialty and facility matchers both hold.
#
# So: accept exact / near-exact only, and leave anything else null for human review.
CITY_ACCEPT_MIN = 0.9

# Cap concurrent city lookups so a resume with many distinct cities can't open an
# unbounded number of sockets or trip the partner's per-second rate limit (a 429).
_MAX_CONCURRENCY = 4

# Process-level cache of resolved lookups, shared across resumes on a warm worker.
# Bounded, and negative results are cached too - a city that does not resolve will not
# resolve on the next resume either, and re-asking just burns quota.
_CACHE: OrderedDict[tuple[str, str, str], city_api.CityMatch | None] = OrderedDict()
_CACHE_MAX = 2048


def _cache_get(key: tuple[str, str, str]) -> city_api.CityMatch | None | object:
    """Return the cached value, or ``_MISS`` when the key has never been looked up.

    A cached *negative* (None) is a real answer, so it must be distinguishable from a
    cache miss - hence the sentinel rather than a bare None.
    """
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return _MISS


_MISS = object()


def _cache_put(key: tuple[str, str, str], value: city_api.CityMatch | None) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _reset_cache() -> None:
    """Test hook - the cache is process-global and would leak between tests."""
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

    # A role that NAMES a city but whose state never resolved offline (the résumé put
    # the state only in the candidate's header, or nowhere) would otherwise be skipped
    # and come back city_id=null. Infer one state for the résumé - the candidate's home
    # state, else the single state every geo-resolved role shares - and scope such a
    # role's lookup to it. This never fabricates: the id is stamped only on a >=0.9
    # match, and the state/country are then backfilled from that authoritative match
    # (which carries its own stateId/countryId), not from the guess. A wrong guess just
    # fails to match - exactly the null it already was.
    inferred = _infer_geo(parsed)

    # Gather the roles that can be resolved. Group by lookup key so identical triples
    # cost one call. `backfill_keys` marks lookups built from an inferred state, whose
    # match must also stamp the role's state/country.
    pending: dict[tuple[str, str, str], list] = {}
    backfill_keys: set[tuple[str, str, str]] = set()
    for exp in parsed.experience:
        if exp.city_id is not None:
            continue  # already resolved (e.g. a DynamoDB reload)
        city = (exp.city or "").strip()
        if not city:
            continue
        if exp.country_id and exp.state_id:
            key = (exp.country_id, exp.state_id, _match_key(city))
        elif inferred is not None:
            key = (inferred[0], inferred[1], _match_key(city))
            backfill_keys.add(key)
        else:
            continue
        pending.setdefault(key, []).append(exp)
    if not pending:
        return 0

    def _apply(key: tuple[str, str, str], hit: city_api.CityMatch | None) -> None:
        if hit is None:
            return
        needs_backfill = key in backfill_keys
        for exp in pending[key]:
            exp.city_id = hit.id
            exp.city_confidence = hit.score
            if needs_backfill:
                _backfill_geography(exp, hit)

    # Serve what we can from the cross-resume cache before spending any quota.
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

        _cache_put(key, best)
        _apply(key, best)
        stats["matched"] += 1

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_resolve(client, k) for k in to_fetch))

    log.info("city_api_tier", lookups=len(to_fetch), cached=cached, **stats)
    return len(to_fetch)


# -- State inference for city-only roles ---------------------------------------

# The state/zip tail of an address line: "..., NY 14075", "..., Texas 77095",
# "..., MI". Captures the state token(s) after the last comma, minus a trailing ZIP.
_STATE_TAIL = re.compile(r",\s*([A-Za-z][A-Za-z. ]{0,24}?)\s*(?:\d{5}(?:-\d{4})?)?\s*$")


def _infer_geo(parsed: ParsedResumeAI) -> tuple[str, str] | None:
    """Pick one (country_id, state_id) to scope a city-only role's lookup, or None.

    Preference order, most reliable first:
      1. the candidate's OWN home state, parsed from ``personal_info.location``;
      2. else the single (country_id, state_id) that every geo-resolved role shares -
         used only when it is unambiguous (exactly one distinct state on the résumé).
    Returns None when neither yields a confident, single state - in which case a
    city-only role stays unresolved, exactly as before.
    """
    home = _home_geo(parsed.personal_info.location if parsed.personal_info else None)
    if home is not None:
        return home
    seen = {
        (e.country_id, e.state_id)
        for e in parsed.experience
        if e.country_id and e.state_id
    }
    if len(seen) == 1:
        cid, sid = next(iter(seen))
        return (cid, sid)  # both are truthy by the comprehension's guard
    return None


def _home_geo(location: str | None) -> tuple[str, str] | None:
    """Resolve (country_id, state_id) from a candidate's home address line, or None."""
    if not location:
        return None
    line = location.strip().splitlines()[-1]      # city/state/zip is on the last line
    m = _STATE_TAIL.search(line)
    if not m:
        return None
    match = geography_matcher.resolve_state(m.group(1).strip())
    if match.matched and match.id and match.country_id:
        return (match.country_id, match.id)
    return None


def _backfill_geography(exp, match: city_api.CityMatch) -> None:
    """Stamp the state/country a matched city belongs to onto a role that lacked them.

    The value comes from the cities match itself (its stateId/countryId are
    authoritative for the city we just confirmed at >=0.9), never from the inference
    guess - so this cannot fabricate a state. Never overrides a value already present.
    """
    if match.state_id and not exp.state_id:
        exp.state_id = match.state_id
        exp.state_confidence = match.score
        exp.state = exp.state or match.state
    if match.country_id and not exp.country_id:
        exp.country_id = match.country_id
        exp.country_confidence = match.score
        exp.country = exp.country or _country_name(match.country_id)


def _country_name(country_id: str | None) -> str | None:
    if not country_id:
        return None
    for c in geography_catalog.get_catalog().countries:
        if c.id == country_id:
            return c.name
    return None
