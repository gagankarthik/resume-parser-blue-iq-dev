"""
Direct tests for ParseBudget - the deadline arithmetic of one parse.

There is one budget now: every parse runs on the async worker (nothing parses on the
HTTP request path), so these cover the single orchestrator -> single-shot -> floor
ladder. Each rule gets a test that names the incident it came from.
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
    return ParseBudget(total=b.total, started=b.started - seconds)


# -- Construction --------------------------------------------------------------

def test_async_gets_the_full_budget():
    b = ParseBudget.for_async()
    assert b.total == bud.TOTAL_BUDGET


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

def test_extraction_keeps_its_full_per_step_cap():
    b = ParseBudget.for_async()
    assert b.for_extraction(bud.TIMEOUT_EXTRACTION) == bud.TIMEOUT_EXTRACTION
    assert b.for_extraction(bud.TIMEOUT_OCR) == bud.TIMEOUT_OCR


# -- Orchestrator --------------------------------------------------------------

def test_orchestrator_is_affordable_on_a_fresh_budget():
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


# -- Single-shot fallback ------------------------------------------------------

def test_ai_parse_is_capped_by_the_single_shot_ceiling():
    assert ParseBudget.for_async().for_async_ai_parse() == bud.TIMEOUT_AI_PARSE


def test_ai_parse_keeps_a_floor_even_on_a_spent_budget():
    """The worker has the Lambda's 300s to play with, so even a nominally spent budget
    gets one real attempt rather than a zero-length window that cannot land."""
    b = _spent(ParseBudget.for_async(), bud.TOTAL_BUDGET + 50)
    assert b.remaining() < 0
    assert b.for_async_ai_parse() == bud.MIN_AI_TIMEOUT


def test_ai_parse_tracks_the_remaining_budget_in_between():
    b = _spent(ParseBudget.for_async(), 160)  # ~40s left: under the cap, over the floor
    assert b.for_async_ai_parse() == pytest.approx(40, abs=1)


# -- Post-parse enrichment -----------------------------------------------------

def test_specialty_tier_keeps_headroom_for_scoring():
    b = ParseBudget.for_async()
    assert b.for_specialty_ai() == pytest.approx(bud.TOTAL_BUDGET - bud.SPECIALTY_SCORING_RESERVE, abs=1)


def test_specialty_tier_goes_non_positive_when_it_should_be_skipped():
    """Tier 4 is optional and must never be the reason a parse fails: on a spent budget
    the window goes <= 0 and the caller skips it."""
    b = _spent(ParseBudget.for_async(), bud.TOTAL_BUDGET - 2)
    assert b.for_specialty_ai() <= 0


def test_city_lookup_is_skipped_on_a_spent_budget():
    b = ParseBudget.for_async()
    assert b.can_afford_city() is True
    assert _spent(b, bud.TOTAL_BUDGET).can_afford_city() is False


def test_city_window_never_outlives_the_budget():
    b = ParseBudget.for_async()
    assert b.for_city() < b.remaining()


# -- The invariants that keep the numbers coherent -----------------------------

def test_the_budget_leaves_room_for_a_full_single_shot_fallback():
    """The reserve exists so that an orchestrator degrade still leaves a COMPLETE
    single-shot attempt. If FALLBACK_RESERVE ever drops below the single-shot cap,
    a degrade quietly becomes a drop to the floor."""
    assert bud.FALLBACK_RESERVE >= bud.TIMEOUT_AI_PARSE


def test_the_orchestrator_window_never_eats_the_fallback_reserve():
    """The binding constraint on the orchestrator is the RESERVE, not its own cap.
    TIMEOUT_ORCHESTRATOR is 130, but the window is min(130, remaining - FALLBACK_RESERVE)
    = min(130, 100) = 100 at the current numbers, so raising it alone does nothing -
    a trap worth a failing test rather than a comment."""
    window = ParseBudget.for_async().for_orchestrator().budget
    assert window <= bud.TOTAL_BUDGET - bud.FALLBACK_RESERVE


def test_the_clock_is_monotonic_not_wall_clock():
    """A parse must not be lengthened or shortened by an NTP step mid-run."""
    b = ParseBudget.for_async()
    assert b.started <= time.monotonic()
