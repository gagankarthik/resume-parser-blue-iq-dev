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
                 shortlist for everything tiers 1–3 missed. The pick is then graded
                 against the chosen record: if the résumé phrase actually contains
                 that record's name/full-name/keyword it earns that tier's
                 confidence (1.00 / 0.95 / 0.80); a genuine semantic match keeps the
                 model's own certainty, capped at 0.70.

Tiers 1–3 do not require a bare, exact spelling: each phrase is probed through
several candidate forms (its canonical name, any parenthetical acronym, the phrase
with parentheticals removed, and each slash-separated part), so a specialty written
as "Surgical Intensive Care Unit (SICU)" or embedded in a longer line still resolves
deterministically at full confidence instead of falling through to the AI tier.

A specialty that matches none of these is returned with `specialty_id=None` and
`matched=False` — never dropped — so an admin can review it. When no catalog is
loaded, tiers 1–3 still clean the NAME via the built-in taxonomy (high name
confidence) but leave `specialty_id=None`; the platform's ids light up the moment
the catalog file is supplied.
"""

from __future__ import annotations

import difflib
import re

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import ParsedResumeAI, SpecialtyMatch
from app.services.normalization.healthcare_taxonomy import (
    _match_key,
    get_specialty_group,
    resolve_specialty,
)
from app.services.normalization.specialty_catalog import (
    SpecialtyCatalog,
    SpecialtyRecord,
    get_catalog,
    profession_keys,
)

log = get_logger(__name__)

# Per-tier confidence. Tunable in one place.
CONF_NAME      = 1.0
CONF_FULL_NAME = 0.95
CONF_KEYWORD   = 0.80
CONF_FUZZY_MAX = 0.94   # a near-miss/typo fuzzy match; graded by similarity but
                        # never allowed to claim an exact tier's certainty
CONF_AI_MAX    = 0.70   # the AI tier's confidence is capped to this when it cannot
                        # be verified deterministically against the chosen record
CONF_UNMATCHED = 0.0

# Conservative similarity floor for the deterministic fuzzy tier (tier 3.5): only a
# near-identical spelling/typo auto-matches; anything less certain falls through to
# the AI tier or stays unmatched for review.
FUZZY_THRESHOLD    = 0.90
_FUZZY_MAX_WORDS   = 6     # only fuzzy-match short, specialty-like candidates
# Minimum model certainty for an UNVERIFIED AI pick to be trusted; below it the
# phrase is left unmatched for review rather than risk a hallucinated id.
CONF_AI_ACCEPT_MIN = 0.55

# Tier ordering used both to run the deterministic lookup and to grade an AI pick.
# Each entry: (flat index attr, profession-scoped index attr, confidence, tier tag).
_TIERS = (
    ("by_name_key",    "by_prof_name_key",    CONF_NAME,      "name"),
    ("by_full_key",    "by_prof_full_key",    CONF_FULL_NAME, "full_name"),
    ("by_keyword_key", "by_prof_keyword_key", CONF_KEYWORD,   "keywords"),
)

# Generic words that carry no discriminating power when sanity-checking an AI pick
# for lexical plausibility (so "Cardiac Nurse" still corroborates "Cardiology" via
# its stem, but a shared "care"/"unit" alone never rubber-stamps a wrong id).
_PLAUSIBILITY_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "for", "in", "to", "with",
    "care", "unit", "nurse", "nursing", "registered", "level", "acute",
    "clinical", "services", "service", "general", "staff", "certified",
})


def _candidate_keys(text: str, canonical: str | None) -> list[str]:
    """Ordered, de-duplicated match keys to try for one résumé specialty phrase.

    A résumé rarely writes a specialty as a bare catalog token — it embeds it in a
    heading or a sentence ("Surgical Intensive Care Unit (SICU)", "Critical Care
    Unit/Cardiac Care Unit", or a whole garbled duty line). We therefore probe, in
    priority order: the taxonomy-canonical name, the phrase as written, any
    parenthetical acronym/expansion, the phrase with parentheticals removed, and
    each slash-separated part. The first candidate that hits a catalog index wins,
    so a clean exact spelling always outranks a fragment pulled from a longer line.
    """
    keys: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        key = _match_key(value)
        if key and key not in keys:
            keys.append(key)

    add(canonical)
    add(text)
    for inner in re.findall(r"\(([^)]+)\)", text):     # "(SICU)" → "SICU"
        add(inner)
    stripped = re.sub(r"\([^)]*\)", " ", text)          # drop the parentheticals
    add(stripped)
    for part in re.split(r"\s*/\s*", stripped):         # "CCU/Cardiac Care Unit"
        add(part)
    # Leading-token prefixes catch a specialty that opens a longer descriptor
    # ("NICU Level III and IV" → "NICU", "ICU Stepdown" → "ICU"). Tried LAST so an
    # exact whole/paren/slash match always wins first (e.g. "ICU Float" resolves to
    # itself, not to "ICU").
    words = stripped.split()
    for n in (2, 1):
        if len(words) > n:
            add(" ".join(words[:n]))
    return keys


def _lookup(
    catalog: SpecialtyCatalog, keys: list[str], prof_keys: list[str]
) -> tuple[SpecialtyRecord, float, str] | None:
    """Run the deterministic tiers over the candidate keys.

    Outer loop is the tier (name > full_name > keyword) so a high-confidence tier
    on ANY candidate beats a lower tier on the exact spelling; inner loop is the
    candidate priority from `_candidate_keys`.
    """
    for flat_attr, prof_attr, conf, tier in _TIERS:
        flat = getattr(catalog, flat_attr)
        scoped = getattr(catalog, prof_attr)
        for key in keys:
            rec = catalog.find(flat, scoped, key, prof_keys)
            if rec is not None:
                return rec, conf, tier
    return None


def _in_scope(rec: SpecialtyRecord, prof_set: set[str]) -> bool:
    """A record is in scope when no profession is known, or their keys overlap."""
    return not prof_set or bool(set(profession_keys(rec.profession)) & prof_set)


def _fuzzy_lookup(
    catalog: SpecialtyCatalog, keys: list[str], prof_keys: list[str],
    *, threshold: float = FUZZY_THRESHOLD,
) -> tuple[SpecialtyRecord, float, str] | None:
    """Tier 3.5 — resolve a near-miss spelling/typo by string similarity.

    Compares each short, specialty-like candidate against every in-scope catalog
    record's name and full name, returning the closest above `threshold`. Uses
    difflib's cheap ratio pre-filters so the scan stays fast, and is scoped to the
    role's profession so a typo never leaks across professions. Confidence is the
    similarity itself, capped below the exact tiers (`CONF_FUZZY_MAX`).
    """
    if catalog.is_empty:
        return None
    cands = [k for k in keys if 0 < len(k.split()) <= _FUZZY_MAX_WORDS]
    if not cands:
        return None
    prof_set = set(prof_keys)

    best: tuple[float, SpecialtyRecord, str] | None = None
    for rec in catalog.records:
        if not _in_scope(rec, prof_set):
            continue
        targets = [(_match_key(rec.name), "name")]
        if rec.full_name:
            targets.append((_match_key(rec.full_name), "full_name"))
        for tkey, tier in targets:
            for cand in cands:
                sm = difflib.SequenceMatcher(None, cand, tkey)
                if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                    continue
                ratio = sm.ratio()
                if ratio >= threshold and (best is None or ratio > best[0]):
                    best = (ratio, rec, tier)
    if best is None:
        return None
    ratio, rec, field_tier = best
    # A ratio of 1.0 means the candidate is IDENTICAL (after normalization) to the
    # record's name/full-name — an exact match, scored 1.0 and tagged with the real
    # tier, never "fuzzy" and never sent to the AI fallback. Genuine near-misses are
    # graded by their similarity, capped below the exact tiers.
    if ratio >= 1.0:
        return rec, CONF_NAME, field_tier
    return rec, min(round(ratio, 2), CONF_FUZZY_MAX), "fuzzy"


def _plausibility_tokens(text: str | None) -> set[str]:
    """Discriminating tokens of a phrase (stopwords + short words dropped)."""
    if not text:
        return set()
    words = _match_key(text).replace("/", " ").replace("(", " ").replace(")", " ").split()
    return {w for w in words if len(w) > 2 and w not in _PLAUSIBILITY_STOPWORDS}


def _ai_pick_plausible(phrase: str, rec: SpecialtyRecord) -> bool:
    """Lexical sanity check on an unverified AI pick — a guard against hallucination.

    Accept only when the phrase and the chosen record share a discriminating token,
    a 4-char token stem (so "Cardiac" corroborates "Cardiology"), or are fuzzily
    close. A pick with no lexical footing at all is rejected and left unmatched.
    """
    p = _plausibility_tokens(phrase)
    if not p:
        return False
    target = _plausibility_tokens(rec.name) | _plausibility_tokens(rec.full_name)
    for kw in rec.keywords:
        target |= _plausibility_tokens(kw)
    if p & target:
        return True
    if {t[:4] for t in p} & {t[:4] for t in target}:
        return True
    return difflib.SequenceMatcher(
        None, _match_key(phrase), _match_key(rec.name)
    ).ratio() >= 0.60


def _dedupe_matches(matches: list[SpecialtyMatch]) -> list[SpecialtyMatch]:
    """Collapse duplicate specialties within a role, keeping the strongest.

    Two phrases can resolve to the same catalog id (e.g. "CCU" and "Critical Care
    Unit") or the same canonical name; keep one entry per id (or per name when
    unmatched), preferring a matched, higher-confidence result. Order preserved.
    """
    best: dict[str, SpecialtyMatch] = {}
    order: list[str] = []
    for m in matches:
        key = f"id:{m.specialty_id}" if m.specialty_id else f"nm:{_match_key(m.name)}"
        cur = best.get(key)
        if cur is None:
            best[key] = m
            order.append(key)
        elif (m.matched, m.confidence) > (cur.matched, cur.confidence):
            best[key] = m
    return [best[k] for k in order]


def _score_against_record(text: str, rec: SpecialtyRecord) -> tuple[float, str] | None:
    """Grade an AI-chosen record against the résumé phrase, deterministically.

    Returns (confidence, tier) when a candidate spelling of `text` equals the
    record's name / full name / a keyword — i.e. the AI merely un-hid a match the
    phrasing obscured — else None (a genuine semantic match to grade as `ai`).
    """
    keys = set(_candidate_keys(text, resolve_specialty(text)))
    if not keys:
        return None
    if _match_key(rec.name) in keys:
        return CONF_NAME, "name"
    if rec.full_name and _match_key(rec.full_name) in keys:
        return CONF_FULL_NAME, "full_name"
    if any(_match_key(kw) in keys for kw in rec.keywords):
        return CONF_KEYWORD, "keywords"
    return None


def match(raw: str, profession: str | None = None) -> SpecialtyMatch:
    """Resolve one raw specialty string through the deterministic tiers (1–3).

    `profession` is the role's credential (e.g. "RN", "LPN"); when supplied it
    scopes the lookup so a name that exists under several professions resolves to
    that profession's id (RN "ICU"=56 vs CNA "ICU"=757), falling back to the flat
    index when the profession has no such specialty.
    """
    text = (raw or "").strip()
    if not text:
        return SpecialtyMatch(name="Unknown", raw=raw or None,
                              confidence=CONF_UNMATCHED, matched=False)

    canonical = resolve_specialty(text)        # taxonomy canonical name, or None
    name = canonical or text
    catalog = get_catalog()
    prof_keys = profession_keys(profession)
    cand_keys = _candidate_keys(text, canonical)

    # Tiers 1–3: exact name / full name / keyword over the candidate spellings.
    hit = _lookup(catalog, cand_keys, prof_keys)
    if hit is None:
        # Tier 3.5: conservative fuzzy match for a near-miss spelling/typo.
        hit = _fuzzy_lookup(catalog, cand_keys, prof_keys)
    if hit is not None:
        rec, conf, tier = hit
        return _matched(rec, raw, conf, tier)

    # No catalog id. If the taxonomy still recognised the NAME, the specialty is
    # clean (high name confidence) but awaits an id — surfaced for review.
    if canonical is not None:
        return SpecialtyMatch(
            name=canonical, raw=_tidy_raw(raw), specialty_id=None,
            group=get_specialty_group(canonical),
            confidence=CONF_NAME, matched=False, match_tier="name",
        )

    return SpecialtyMatch(
        name=_tidy_raw(name), raw=_tidy_raw(raw), specialty_id=None, group=None,
        confidence=CONF_UNMATCHED, matched=False, match_tier=None,
    )


def match_batch(specialties: list[str], profession: str | None = None) -> list[SpecialtyMatch]:
    """Resolve a list of raw specialty strings and de-duplicate them, in order.

    `profession` scopes every lookup to the role's credential (see `match`). The
    result carries one entry per distinct catalog id / canonical name, keeping the
    strongest match (see `_dedupe_matches`).
    """
    return _dedupe_matches([match(raw, profession) for raw in specialties])


def _matched(rec: SpecialtyRecord, raw: str, confidence: float, tier: str) -> SpecialtyMatch:
    return SpecialtyMatch(
        name=rec.name,
        raw=_tidy_raw(raw),
        specialty_id=rec.id,
        group=rec.group or get_specialty_group(rec.name),
        confidence=confidence,
        matched=True,
        match_tier=tier,
    )


# A specialty rarely needs more than a handful of words; anything longer is a duty
# line the extractor mis-filed. We keep `raw` for audit but collapse the obvious
# noise so it reads cleanly instead of echoing a repeated, run-on sentence.
_MAX_RAW_WORDS = 8


def _tidy_raw(raw: str | None) -> str | None:
    """Clean an extractor-emitted raw specialty phrase for legible auditing.

    The extractor occasionally doubles a specialty string or runs it on
    ("Neonatal Intensive Care Unit (NICU) Level III and Level IV Neonatal Care Unit
    (NICU) Level III and Level IV including …"). This:
      1. collapses whitespace,
      2. removes an ADJACENT duplicated run of 1–6 words,
      3. drops a later EXACT repeat of a 2–6 word phrase (non-adjacent), and
      4. trims a very long trailing tail,
    so the preserved `raw` stays legible — without inventing or reordering content.
    """
    if not raw:
        return raw
    text = re.sub(r"\s+", " ", raw).strip()

    # (2) Adjacent duplicated run of up to 6 words, repeatedly.
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\b(\w[\w /()&-]{0,80}?)\s+\1\b", r"\1", text, flags=re.I)

    # (3) Non-adjacent exact phrase repeat: keep the first occurrence, drop later ones.
    text = _drop_repeated_phrases(text)

    # (4) Bound the length.
    words = text.split()
    if len(words) > _MAX_RAW_WORDS:
        text = " ".join(words[:_MAX_RAW_WORDS]).rstrip(" ,;:-") + "…"
    return text


def _drop_repeated_phrases(text: str, *, min_words: int = 2, max_words: int = 6) -> str:
    """Remove a later exact repeat of any `min_words`–`max_words` word phrase.

    Scans left→right; once a phrase (by lowercase key) has been seen, a later
    identical run of the same length is skipped. Longer runs are preferred so a
    whole repeated clause is dropped in one piece. Order and first occurrences are
    preserved.
    """
    words = text.split()
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    while i < len(words):
        dropped = False
        for n in range(min(max_words, len(words) - i), min_words - 1, -1):
            key = " ".join(words[i:i + n]).lower()
            if key in seen:
                i += n            # skip this repeated run
                dropped = True
                break
        if dropped:
            continue
        # Register every run starting here so a later copy is recognised.
        for n in range(min_words, min(max_words, len(words) - i) + 1):
            seen.add(" ".join(words[i:i + n]).lower())
        out.append(words[i])
        i += 1
    return " ".join(out)


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

    # Gather the distinct unmatched phrases across every role, remembering each
    # occurrence's role profession so an id is only applied to a compatible role.
    pending: dict[str, list[tuple[SpecialtyMatch, str | None]]] = {}
    prof_scope: set[str] = set()
    for exp in parsed.experience:
        for sm in exp.specialties:
            if sm.matched or sm.specialty_id is not None:
                continue
            phrase = (sm.raw or sm.name or "").strip()
            if phrase:
                pending.setdefault(phrase, []).append((sm, exp.profession))
                prof_scope.update(profession_keys(exp.profession))
    if not pending:
        return 0

    shortlist, by_id = _build_shortlist(
        list(pending), catalog.records, settings.specialty_ai_shortlist_max, prof_scope
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

    applied = 0
    for m in matches:
        rec = by_id.get(m.specialty_id) if m.specialty_id else None
        if rec is None:
            continue  # drop a hallucinated / off-shortlist id
        rec_prof = set(profession_keys(rec.profession))
        for sm, role_prof in pending.get(m.raw.strip(), ()):
            # Only stamp the id when the candidate's profession is compatible with
            # the role's (either side unknown, or their keys overlap) — never leak
            # e.g. a CNA id onto an RN role.
            role_keys = set(profession_keys(role_prof))
            if rec_prof and role_keys and not (rec_prof & role_keys):
                continue
            phrase = sm.raw or sm.name or m.raw
            # Grade the pick. If the résumé phrase actually contains this record's
            # name/full-name/keyword it is a deterministic match the phrasing hid —
            # award that tier's confidence. Otherwise it is a semantic call: trust it
            # only when the model is sufficiently sure AND the pick is lexically
            # plausible; an unconvincing pick is dropped (left unmatched for review)
            # rather than risk a hallucinated id.
            verified = _score_against_record(phrase, rec)
            if verified is not None:
                conf, tier = verified
            elif m.confidence >= CONF_AI_ACCEPT_MIN and _ai_pick_plausible(phrase, rec):
                conf, tier = min(m.confidence, CONF_AI_MAX), "ai"
            else:
                continue
            sm.name = rec.name
            sm.specialty_id = rec.id
            sm.group = rec.group or get_specialty_group(rec.name)
            sm.confidence = conf
            sm.match_tier = tier
            sm.matched = True
            applied += 1

    # Two phrases in one role can now resolve to the same id — collapse duplicates.
    for exp in parsed.experience:
        if len(exp.specialties) > 1:
            exp.specialties = _dedupe_matches(exp.specialties)

    log.info("specialty_ai_tier", unmatched=len(pending), applied=applied, tokens=meter.total)
    return meter.total


def _build_shortlist(
    phrases: list[str],
    records: list[SpecialtyRecord],
    cap: int,
    prof_scope: set[str],
) -> tuple[list[str], dict[str, SpecialtyRecord]]:
    """Pick the catalog candidates most likely to cover `phrases`.

    Ranked: candidates that both share a word with an unmatched phrase AND belong
    to a profession in scope come first, then any word-sharing candidate, then the
    rest — trimmed to `cap`. Returns the formatted "<id> | <name> | <full> |
    <profession>" lines plus an id→record map (for validating the model's reply and
    applying the chosen record's profession).
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

    def in_scope(rec: SpecialtyRecord) -> bool:
        return not prof_scope or bool(set(profession_keys(rec.profession)) & prof_scope)

    scoped_relevant = [r for r in records if shares_word(r) and in_scope(r)]
    other_relevant = [r for r in records if shares_word(r) and not in_scope(r)]
    rest = [r for r in records if not shares_word(r)]
    chosen = (scoped_relevant + other_relevant + rest)[:cap]

    lines = [
        f"{r.id} | {r.name}"
        + (f" | {r.full_name}" if r.full_name else " |")
        + (f" | {r.profession}" if r.profession else "")
        for r in chosen
    ]
    return lines, {r.id: r for r in chosen}
