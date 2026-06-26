"""
Tiered specialty → catalog-id matcher.

Each résumé specialty string is resolved to a `SpecialtyMatch` carrying a catalog
`specialty_id` (when found), a `confidence`, the `group`, and which tier fired.
Tiers, highest-confidence first:

  1. name      — the specialty itself (canonicalised through the taxonomy) matches
                 a catalog specialty name.                              conf 1.00
  2. full_name — it matches a catalog specialty's fuller name.          conf 0.95
  3. keywords  — it matches one of a catalog specialty's keywords.      conf 0.80
  4. ai        — (async, batched) the LLM picks the best catalog id from a filtered
                 shortlist for everything tiers 1–3 missed.        conf ≤ 0.70

A specialty that matches none of these is returned with `specialty_id=None` and
`matched=False` — never dropped — so an admin can review it. When no catalog is
loaded, tiers 1–3 still clean the NAME via the built-in taxonomy (high name
confidence) but leave `specialty_id=None`; the platform's ids light up the moment
the catalog file is supplied.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI, SpecialtyMatch
from app.services.normalization.healthcare_taxonomy import (
    _match_key,
    get_specialty_group,
    resolve_specialty,
)
from app.services.normalization.specialty_catalog import SpecialtyRecord, get_catalog

log = get_logger(__name__)

# Per-tier confidence. Tunable in one place.
CONF_NAME      = 1.0
CONF_FULL_NAME = 0.95
CONF_KEYWORD   = 0.80
CONF_AI_MAX    = 0.70   # the AI tier's confidence is capped to this
CONF_UNMATCHED = 0.0


def match(raw: str) -> SpecialtyMatch:
    """Resolve one raw specialty string through the deterministic tiers (1–3)."""
    text = (raw or "").strip()
    if not text:
        return SpecialtyMatch(name="Unknown", raw=raw or None,
                              confidence=CONF_UNMATCHED, matched=False)

    canonical = resolve_specialty(text)        # taxonomy canonical name, or None
    name = canonical or text
    catalog = get_catalog()

    raw_key = _match_key(text)
    name_key = _match_key(name)

    # Tier 1 — specialty name (try the canonical name first, then the raw spelling).
    rec = catalog.by_name_key.get(name_key) or catalog.by_name_key.get(raw_key)
    if rec is not None:
        return _matched(rec, raw, CONF_NAME, "name")

    # Tier 2 — fuller specialty name.
    rec = catalog.by_full_key.get(raw_key) or catalog.by_full_key.get(name_key)
    if rec is not None:
        return _matched(rec, raw, CONF_FULL_NAME, "full_name")

    # Tier 3 — keyword.
    rec = catalog.by_keyword_key.get(raw_key) or catalog.by_keyword_key.get(name_key)
    if rec is not None:
        return _matched(rec, raw, CONF_KEYWORD, "keywords")

    # No catalog id. If the taxonomy still recognised the NAME, the specialty is
    # clean (high name confidence) but awaits an id — surfaced for review.
    if canonical is not None:
        return SpecialtyMatch(
            name=canonical, raw=raw, specialty_id=None,
            group=get_specialty_group(canonical),
            confidence=CONF_NAME, matched=False, match_tier="name",
        )

    return SpecialtyMatch(
        name=name, raw=raw, specialty_id=None, group=None,
        confidence=CONF_UNMATCHED, matched=False, match_tier=None,
    )


def match_batch(specialties: list[str]) -> list[SpecialtyMatch]:
    """Resolve a list of raw specialty strings, dedup-by-canonical-name, in order."""
    seen: set[str] = set()
    out: list[SpecialtyMatch] = []
    for raw in specialties:
        m = match(raw)
        dedup_key = _match_key(m.name)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(m)
    return out


def _matched(rec: SpecialtyRecord, raw: str, confidence: float, tier: str) -> SpecialtyMatch:
    return SpecialtyMatch(
        name=rec.name,
        raw=raw,
        specialty_id=rec.id,
        group=rec.group or get_specialty_group(rec.name),
        confidence=confidence,
        matched=True,
        match_tier=tier,
    )


# ── Tier 4: batched AI shortlist resolution ───────────────────────────────────


async def resolve_unmatched_with_ai(parsed: ParsedResumeAI, *, budget: float) -> int:
    """Resolve still-unmatched per-role specialties with one batched LLM call.

    Mutates `parsed.experience[*].specialties` in place, stamping an `ai`-tier id +
    confidence onto any entry the model confidently maps to a catalog candidate.
    Returns tokens used (0 when skipped). Best-effort: any failure leaves the
    deterministic result untouched and is swallowed by the caller's warning path.
    """
    settings = get_settings()
    if not settings.enable_ai_specialty_match or budget <= 0:
        return 0

    catalog = get_catalog()
    if catalog.is_empty:
        return 0

    # Gather the distinct unmatched phrases across every role (by original text).
    pending: dict[str, list[SpecialtyMatch]] = {}
    for exp in parsed.experience:
        for sm in exp.specialties:
            if sm.matched or sm.specialty_id is not None:
                continue
            phrase = (sm.raw or sm.name or "").strip()
            if phrase:
                pending.setdefault(phrase, []).append(sm)
    if not pending:
        return 0

    shortlist, allowed_ids = _build_shortlist(
        list(pending), catalog.records, settings.specialty_ai_shortlist_max
    )
    if not shortlist:
        return 0

    # Imported lazily so the normalization layer doesn't import the parsing/agents
    # stack (and its OpenAI client) at module load.
    from app.services.parsing.agents.base import TokenMeter
    from app.services.parsing.agents.specialty import SpecialtyMatchAgent

    meter = TokenMeter()
    try:
        matches = await SpecialtyMatchAgent().run(list(pending), shortlist, meter)
    except Exception as exc:  # noqa: BLE001 — tier 4 is best-effort
        log.warning("specialty_ai_tier_failed", error=str(exc))
        return meter.total

    by_id = {rec.id: rec for rec in catalog.records}
    applied = 0
    for m in matches:
        if not m.specialty_id or m.specialty_id not in allowed_ids:
            continue  # drop a hallucinated / off-shortlist id
        rec = by_id.get(m.specialty_id)
        if rec is None:
            continue
        for sm in pending.get(m.raw.strip(), ()):
            sm.name = rec.name
            sm.specialty_id = rec.id
            sm.group = rec.group or get_specialty_group(rec.name)
            sm.confidence = min(m.confidence, CONF_AI_MAX)
            sm.matched = True
            sm.match_tier = "ai"
            applied += 1

    log.info("specialty_ai_tier", unmatched=len(pending), applied=applied, tokens=meter.total)
    return meter.total


def _build_shortlist(
    phrases: list[str], records: list[SpecialtyRecord], cap: int
) -> tuple[list[str], set[str]]:
    """Pick the catalog candidates most likely to cover `phrases`.

    Candidates sharing a word with any unmatched phrase come first (most relevant);
    the rest fill the list up to `cap`. Returns the formatted "<id> | <name> |
    <full>" lines plus the set of ids offered (for validating the model's reply).
    """
    phrase_tokens: set[str] = set()
    for p in phrases:
        phrase_tokens.update(_match_key(p).split())

    def shares_word(rec: SpecialtyRecord) -> bool:
        words = set(_match_key(rec.name).split())
        if rec.full_name:
            words.update(_match_key(rec.full_name).split())
        for kw in rec.keywords:
            words.update(_match_key(kw).split())
        return bool(words & phrase_tokens)

    relevant = [r for r in records if shares_word(r)]
    rest = [r for r in records if r not in relevant]
    chosen = (relevant + rest)[:cap]

    lines = [
        f"{r.id} | {r.name}" + (f" | {r.full_name}" if r.full_name else "")
        for r in chosen
    ]
    return lines, {r.id for r in chosen}
