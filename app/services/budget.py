"""
The time budget for one resume parse.

Every constant in this module was born from a production incident: a 504, a
silently dropped work history, an OCR pass that ate the whole budget. They used to
live in `pipeline.py`, interleaved line by line with the parse orchestration, so
`run()` was simultaneously orchestrating a parse AND hand-solving a deadline-
arithmetic problem.

This module is that seam. Each deadline rule is a named method carrying the incident
that produced it, and each is unit-testable without running a parse.

There is one budget: every parse runs on the async worker (nothing parses on the
HTTP request path anymore), so the worker's full wall-clock budget is the only
ladder. The AI step self-bounds against the time actually left under it.
"""

import time
from dataclasses import dataclass

# -- Per-step caps -------------------------------------------------------------

# Text extraction from a digital file (PDF/DOCX/RTF).
TIMEOUT_EXTRACTION = 60
# Textract on multi-page scans - bounded so OCR alone can't eat the whole
# per-resume budget.
TIMEOUT_OCR = 90

# The orchestrator self-bounds each stage (see orchestrator._STAGE2_TIMEOUT et al.)
# and returns a partial result rather than being cancelled, so this is only a hard
# safety net set just above its internal budget. The single-shot fallback is one
# LLM call, so it gets a tighter cap - together they bound the worst case instead
# of stacking two full 2-minute timeouts when the orchestrator degrades.
TIMEOUT_ORCHESTRATOR = 130
# Single-shot cap. Measured single-shot parse time for a dense multi-role resume
# is 39-55s (e.g. a 12-role radiology resume: 39.1s -> 12 roles fully extracted),
# so a 45s cap sat right on the cliff and normal OpenAI latency variance tipped
# it into a contact-only "partial". Give it real headroom - the Lambda function
# timeout is 300s, so the binding constraint is the total budget below, not this.
TIMEOUT_AI_PARSE = 90

# -- Wall-clock budget ---------------------------------------------------------

# Overall soft budget for one resume. Every AI step is capped by the time left
# under this budget. Sized to let a slow orchestrator degrade AND still leave a
# full single-shot fallback, comfortably inside the 300s Lambda timeout.
TOTAL_BUDGET = 200

# -- Reserves (time held back from one step for the step that must follow) ------

# Held back from the orchestrator for the single-shot fallback + scoring - large
# enough for a full TIMEOUT_AI_PARSE fallback attempt after a degrade.
FALLBACK_RESERVE = 100
# Below this much *usable* window (i.e. on top of FALLBACK_RESERVE) the orchestrator
# cannot do anything worthwhile, so go straight to the cheaper single-shot.
MIN_ORCHESTRATOR_WINDOW = 15
# Hard asyncio net above the orchestrator's own self-bounded budget.
ORCHESTRATOR_HARD_NET = 10
# Floor for the single-shot AI parse: there is room to try even when the budget is
# nominally spent, so it never opens a zero-length window.
MIN_AI_TIMEOUT = 15.0

# Tier-4 specialty resolution keeps this much headroom for confidence scoring.
SPECIALTY_SCORING_RESERVE = 5
# City enrichment: skip below this, and keep a hard net above the lookup.
MIN_CITY_WINDOW = 3
CITY_HARD_NET = 1


@dataclass(frozen=True)
class Window:
    """A time window for one step.

    `budget` is what the step is *told* it has and self-bounds against; `timeout`
    is the hard asyncio net that sits above it, so a step that ignores its own
    budget still cannot overrun. Keeping the pair together is what guarantees
    `timeout > budget` - computing them from two separate `remaining()` reads (as
    the old inline code did) let the gap silently shrink.
    """

    budget:  float
    timeout: float


@dataclass(frozen=True)
class ParseBudget:
    """Owns every deadline decision in one parse. Construct via `for_async()`."""

    total:   float
    started: float

    # -- construction ----------------------------------------------------------

    @classmethod
    def for_async(cls) -> "ParseBudget":
        """The worker: full budget, orchestrator-first ladder."""
        return cls(total=TOTAL_BUDGET, started=time.monotonic())

    # -- core ------------------------------------------------------------------

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def elapsed_ms(self) -> int:
        return int(self.elapsed() * 1000)

    def remaining(self) -> float:
        """Seconds left under the wall budget. Goes negative once it is blown."""
        return self.total - self.elapsed()

    # -- extraction ------------------------------------------------------------

    def for_extraction(self, cap: float) -> float:
        """Cap one extraction step. The worker keeps the full per-step cap - the
        wall budget is generous enough that extraction never needs clamping."""
        return cap

    # -- AI parse: orchestrator ------------------------------------------------

    def can_afford_orchestrator(self) -> bool:
        """False when extraction (e.g. a slow OCR pass) already ate the budget - go
        straight to the cheaper single-shot rather than start a fan-out that will be
        cancelled halfway."""
        return self.remaining() > FALLBACK_RESERVE + MIN_ORCHESTRATOR_WINDOW

    def for_orchestrator(self) -> Window:
        budget = min(TIMEOUT_ORCHESTRATOR, self.remaining() - FALLBACK_RESERVE)
        return Window(budget=budget, timeout=budget + ORCHESTRATOR_HARD_NET)

    # -- AI parse: single-shot fallback ----------------------------------------

    def for_async_ai_parse(self) -> float:
        return min(TIMEOUT_AI_PARSE, max(MIN_AI_TIMEOUT, self.remaining()))

    # -- Post-parse enrichment -------------------------------------------------

    def for_specialty_ai(self) -> float:
        """Tier-4 specialty resolution. Non-positive means skip it - it is optional and
        must never be the reason a parse fails."""
        return self.remaining() - SPECIALTY_SCORING_RESERVE

    def can_afford_city(self) -> bool:
        return self.remaining() > MIN_CITY_WINDOW

    def for_city(self) -> float:
        return self.remaining() - CITY_HARD_NET
