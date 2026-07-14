"""
Direct tests for ParseBudget - the deadline arithmetic of one parse.

Before this module existed, every one of these rules was reachable only by running
a full pipeline: to assert "the sync path won't open an AI call it can't afford"
you had to stub four extractors, an AI parser and an orchestrator, then infer the
rule from which stub did or did not get called. That is why the rules were only
ever changed by adding another constant - nobody could see them.

They are ordinary functions now. Each one gets a test that names the incident it
came from.
"""

import time

import pytest

from app.services import budget as bud
from app.services.budget import ParseBudget


def _spent(b: ParseBudget, seconds: float) -> ParseBudget:
    """Return the same budget with its clock wound back, i.e. `seconds` already spent.

    Beats sleeping: the rules are pure arithmetic over `remaining()`, so moving the
    start time is exactly equivalent and costs no wall-clock.
    """
    return ParseBudget(total=b.total, sync=b.sync, probe=b.probe, started=b.started - seconds)


# -- Construction: the two ladders --------------------------------------------

def test_async_gets_the_full_budget():
    b = ParseBudget.for_async()
    assert b.total == bud.TOTAL_BUDGET
    assert b.sync is False
    assert b.probe is False


def test_sync_gets_the_gateway_ceiling_not_the_full_budget():
    """The whole point of the sync ladder: a caller blocking on the response must
    answer inside its gateway, not run to the 200s worker budget and get severed."""
    b = ParseBudget.for_sync()
    assert b.total == bud.SYNC_WALL_BUDGET
    assert b.total < bud.TOTAL_BUDGET
    assert b.sync is True


def test_probe_is_a_sync_budget_that_knows_it_will_be_thrown_away():
    assert ParseBudget.for_sync(probe=True).probe is True
    assert ParseBudget.for_sync().probe is False


def test_remaining_counts_down_and_goes_negative_when_blown():
    b = ParseBudget.for_async()
    assert b.remaining() == pytest.approx(bud.TOTAL_BUDGET, abs=1)
    assert _spent(b, 190).remaining() == pytest.approx(10, abs=1)
    # Must go NEGATIVE rather than clamp at zero - the callers below subtract
    # reserves from it, and a clamp would silently manufacture time that is gone.
    assert _spent(b, 250).remaining() < 0


def test_elapsed_ms_is_the_reported_duration():
    b = _spent(ParseBudget.for_async(), 1.5)
    assert 1400 <= b.elapsed_ms() <= 1700


# -- Extraction ----------------------------------------------------------------

def test_async_extraction_keeps_its_full_per_step_cap():
    b = ParseBudget.for_async()
    assert b.for_extraction(bud.TIMEOUT_EXTRACTION) == bud.TIMEOUT_EXTRACTION
    assert b.for_extraction(bud.TIMEOUT_OCR) == bud.TIMEOUT_OCR


def test_sync_extraction_is_clamped_to_what_the_budget_can_afford():
    """THE INCIDENT: extraction used to run outside the wall budget on its own 60s/90s
    caps, so one slow step could blow the gateway ceiling before the AI parse even
    began - an independent source of 504s the budget never saw."""
    b = ParseBudget.for_sync()  # 50s total, nothing spent
    window = b.for_extraction(bud.TIMEOUT_EXTRACTION)
    # Never the raw 60s cap: it exceeds the whole 50s sync budget.
    assert window < bud.TIMEOUT_EXTRACTION
    # It is "everything left, minus what the AI parse that follows will need".
    assert window == pytest.approx(bud.SYNC_WALL_BUDGET - bud.SYNC_EXTRACT_RESERVE, abs=1)


def test_sync_extraction_shrinks_as_the_budget_is_spent():
    b = ParseBudget.for_sync()
    early = b.for_extraction(bud.TIMEOUT_EXTRACTION)
    late  = _spent(b, 20).for_extraction(bud.TIMEOUT_EXTRACTION)
    assert late < early


def test_sync_extraction_never_drops_below_the_floor():
    """Below MIN_EXTRACT_TIMEOUT the step is pointless - but hand it a NEGATIVE
    timeout and asyncio.wait_for raises instantly, turning a degradable parse into a
    hard ExtractionError."""
    b = _spent(ParseBudget.for_sync(), 999)  # budget massively blown
    assert b.remaining() < 0
    assert b.for_extraction(bud.TIMEOUT_EXTRACTION) == bud.MIN_EXTRACT_TIMEOUT


# -- Async ladder: orchestrator ------------------------------------------------

def test_orchestrator_is_affordable_on_a_fresh_async_budget():
    assert ParseBudget.for_async().can_afford_orchestrator() is True


def test_orchestrator_is_skipped_once_extraction_has_eaten_the_budget():
    """A slow OCR pass must not leave the orchestrator a window too small to finish -
    it would fan out per-role, get cancelled, and lose the work history. Go straight
    to the cheaper single-shot instead."""
    b = ParseBudget.for_async()
    # Leave exactly the fallback reserve + the minimum useful window: still affordable.
    assert _spent(b, bud.TOTAL_BUDGET - bud.FALLBACK_RESERVE - bud.MIN_ORCHESTRATOR_WINDOW - 1).can_afford_orchestrator() is True
    # One second less and it is not.
    assert _spent(b, bud.TOTAL_BUDGET - bud.FALLBACK_RESERVE - bud.MIN_ORCHESTRATOR_WINDOW + 1).can_afford_orchestrator() is False


def test_orchestrator_window_holds_back_the_single_shot_fallback():
    """The reserve is what guarantees a FULL single-shot attempt still fits after the
    orchestrator degrades. Without it a slow orchestrator eats the fallback too and
    the parse drops all the way to the contact-only floor."""
    w = ParseBudget.for_async().for_orchestrator()
    assert w.budget <= bud.TIMEOUT_ORCHESTRATOR
    assert w.budget == pytest.approx(bud.TOTAL_BUDGET - bud.FALLBACK_RESERVE, abs=1)
    # Enough left afterwards for a real fallback.
    assert bud.TOTAL_BUDGET - w.budget >= bud.TIMEOUT_AI_PARSE


def test_orchestrator_hard_net_sits_above_its_own_budget():
    """The orchestrator self-bounds its stages and returns a PARTIAL rather than being
    cancelled. The asyncio timeout must therefore sit ABOVE that budget - if it fired
    first it would cancel the fan-out and destroy the partial the design depends on."""
    w = ParseBudget.for_async().for_orchestrator()
    assert w.timeout > w.budget
    assert w.timeout == w.budget + bud.ORCHESTRATOR_HARD_NET


def test_orchestrator_window_is_capped_even_with_a_huge_budget(monkeypatch):
    monkeypatch.setattr(bud, "TOTAL_BUDGET", 10_000)
    assert ParseBudget.for_async().for_orchestrator().budget == bud.TIMEOUT_ORCHESTRATOR


# -- Async ladder: single-shot -------------------------------------------------

def test_async_ai_parse_is_capped_by_the_single_shot_ceiling():
    assert ParseBudget.for_async().for_async_ai_parse() == bud.TIMEOUT_AI_PARSE


def test_async_ai_parse_keeps_a_floor_even_on_a_spent_budget():
    """The async worker has the Lambda's 300s to play with, so even a nominally spent
    budget gets one real attempt rather than a zero-length window that cannot land."""
    b = _spent(ParseBudget.for_async(), bud.TOTAL_BUDGET + 50)
    assert b.remaining() < 0
    assert b.for_async_ai_parse() == bud.MIN_ASYNC_AI_TIMEOUT


def test_async_ai_parse_tracks_the_remaining_budget_in_between():
    b = _spent(ParseBudget.for_async(), 160)  # ~40s left: under the cap, over the floor
    assert b.for_async_ai_parse() == pytest.approx(40, abs=1)


# -- Sync ladder: single-shot --------------------------------------------------

def test_sync_ai_parse_holds_back_the_enrich_reserve():
    """The single-shot is capped BELOW the wall budget on purpose: a resume that would
    run long is cut early, leaving room to recover the semantic sections instead of
    dropping to the bare contact-only floor."""
    w = ParseBudget.for_sync().for_sync_ai_parse()
    assert w == pytest.approx(bud.SYNC_WALL_BUDGET - bud.SYNC_ENRICH_RESERVE, abs=1)
    assert w < bud.SYNC_WALL_BUDGET


def test_a_probe_hands_the_enrich_reserve_to_the_single_shot():
    """A probe caller RE-DISPATCHES any partial to the async worker, so an enrich pass
    would be thrown away. Give that time to the single-shot instead - a bigger cap
    means more resumes finish synchronously and never need the async round trip."""
    plain = ParseBudget.for_sync()
    probe = ParseBudget.for_sync(probe=True)
    assert probe.enrich_reserve() < plain.enrich_reserve()
    assert probe.for_sync_ai_parse() > plain.for_sync_ai_parse()


def test_sync_ai_parse_has_no_floor_it_cannot_afford():
    """THE INCIDENT: an earlier `max(15.0, ...)` floor here meant a slow extraction
    could still hand the AI a 15s window the budget could not afford, overshooting the
    gateway ceiling - the very 504 this budget exists to prevent. The window must be
    allowed to go small (and unviable) rather than be inflated to a lie."""
    b = _spent(ParseBudget.for_sync(), 45)  # 5s left, minus a 14s reserve => negative
    assert b.for_sync_ai_parse() < 0


def test_an_unviable_sync_window_is_rejected_before_the_call_is_made():
    assert ParseBudget.is_viable_sync_window(bud.MIN_SYNC_AI_TIMEOUT) is True
    assert ParseBudget.is_viable_sync_window(bud.MIN_SYNC_AI_TIMEOUT - 0.1) is False
    assert ParseBudget.is_viable_sync_window(-5) is False


def test_a_fresh_sync_budget_can_always_afford_an_ai_call():
    """If this ever went false, EVERY sync parse would degrade on arrival."""
    w = ParseBudget.for_sync().for_sync_ai_parse()
    assert ParseBudget.is_viable_sync_window(w)


# -- Sync ladder: enrich -------------------------------------------------------

def test_probe_never_enriches():
    """It promotes the partial to async instead, so the enrich output is discarded."""
    assert ParseBudget.for_sync(probe=True).can_afford_enrich() is False


def test_enrich_runs_only_while_there_is_real_time_left():
    b = ParseBudget.for_sync()
    assert _spent(b, bud.SYNC_WALL_BUDGET - bud.MIN_ENRICH_WINDOW - 1).can_afford_enrich() is True
    assert _spent(b, bud.SYNC_WALL_BUDGET - bud.MIN_ENRICH_WINDOW + 1).can_afford_enrich() is False


def test_enrich_hard_net_sits_above_the_agents_own_budget():
    """Same contract as the orchestrator: the agents self-bound and return what they
    have, so the asyncio net must fire strictly later or it cancels them and the
    recovered sections are lost.

    This pair is computed from ONE reading of the clock. The old inline code read
    `_remaining()` twice - once for the budget, once for the timeout - so the gap
    between them was silently smaller than the constants implied.
    """
    w = ParseBudget.for_sync().for_enrich()
    assert w.timeout > w.budget
    assert w.timeout - w.budget == pytest.approx(bud.ENRICH_AGENT_RESERVE - bud.ENRICH_HARD_NET)


# -- Post-parse enrichment (both ladders) --------------------------------------

def test_specialty_tier_keeps_headroom_for_scoring():
    b = ParseBudget.for_async()
    assert b.for_specialty_ai() == pytest.approx(bud.TOTAL_BUDGET - bud.SPECIALTY_SCORING_RESERVE, abs=1)


def test_specialty_tier_goes_non_positive_when_it_should_be_skipped():
    """Tier 4 is optional and must never be the reason a parse fails: on a spent budget
    the window goes <= 0 and the caller skips it."""
    b = _spent(ParseBudget.for_async(), bud.TOTAL_BUDGET - 2)
    assert b.for_specialty_ai() <= 0


def test_city_lookup_is_skipped_on_a_spent_budget():
    b = ParseBudget.for_sync()
    assert b.can_afford_city() is True
    assert _spent(b, bud.SYNC_WALL_BUDGET).can_afford_city() is False


def test_city_window_never_outlives_the_budget():
    b = ParseBudget.for_sync()
    assert b.for_city() < b.remaining()


# -- The invariants that keep the numbers coherent -----------------------------
#
# These are the ones that break when someone "just bumps a constant" to fix the next
# timeout. They are cheap, and they are the guard rail this refactor exists to give.

def test_the_sync_reserves_fit_inside_the_sync_budget():
    assert bud.SYNC_EXTRACT_RESERVE < bud.SYNC_WALL_BUDGET
    assert bud.SYNC_ENRICH_RESERVE < bud.SYNC_WALL_BUDGET
    assert bud.SYNC_WALL_BUDGET - bud.SYNC_ENRICH_RESERVE >= bud.MIN_SYNC_AI_TIMEOUT


def test_the_async_budget_leaves_room_for_a_full_single_shot_fallback():
    """The reserve exists so that an orchestrator degrade still leaves a COMPLETE
    single-shot attempt. If FALLBACK_RESERVE ever drops below the single-shot cap,
    a degrade quietly becomes a drop to the floor."""
    assert bud.FALLBACK_RESERVE >= bud.TIMEOUT_AI_PARSE


def test_the_orchestrator_window_never_eats_the_fallback_reserve():
    """The binding constraint on the orchestrator is the RESERVE, not its own cap.

    Worth stating plainly, because the two look interchangeable and are not:
    TIMEOUT_ORCHESTRATOR is 130, but the window is min(130, remaining - FALLBACK_RESERVE)
    = min(130, 100) = 100. **The 130 cap cannot bind at the current numbers** - it is
    a dormant ceiling that only wakes up if TOTAL_BUDGET grows or FALLBACK_RESERVE
    shrinks (test_orchestrator_window_is_capped_even_with_a_huge_budget covers that).

    So the invariant that actually protects the fallback is this one, and raising
    TIMEOUT_ORCHESTRATOR to "give the orchestrator more time" would do nothing at all -
    a trap worth a failing test rather than a comment.
    """
    window = ParseBudget.for_async().for_orchestrator().budget
    assert window <= bud.TOTAL_BUDGET - bud.FALLBACK_RESERVE


def test_the_enrich_reserve_can_actually_pay_for_an_enrich():
    """SYNC_ENRICH_RESERVE is carved out of the single-shot's window specifically to
    fund the enrich pass. If it were smaller than the pass's own minimum, the reserve
    would be pure waste: time taken from the AI parse and then never spent."""
    assert bud.SYNC_ENRICH_RESERVE > bud.MIN_ENRICH_WINDOW


def test_the_clock_is_monotonic_not_wall_clock():
    """A parse must not be lengthened or shortened by an NTP step mid-run."""
    b = ParseBudget.for_async()
    assert b.started <= time.monotonic()
