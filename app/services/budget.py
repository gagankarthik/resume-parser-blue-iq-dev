"""
The time budget for one resume parse.

Every constant in this module was born from a production incident: a 504, a
silently dropped work history, an OCR pass that ate the whole gateway ceiling.
They are correct. They were also in the wrong place - they used to live in
`pipeline.py`, interleaved line by line with the parse orchestration, so `run()`
was simultaneously orchestrating a parse AND hand-solving a deadline-arithmetic
problem. There was no seam. When the next timeout bug arrived, the only move
available was to add one more constant and one more branch, which is exactly how
this file's contents grew to eleven tuned numbers.

This module is that seam. Each deadline rule is a named method carrying the
incident that produced it, and each is unit-testable without running a parse.
The next timeout fix goes *here*, which is now a place that exists.

The SYNC and ASYNC ladders deliberately differ - see `ParseBudget.for_sync`.
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

# -- Wall-clock budgets --------------------------------------------------------

# Overall soft budget for one resume. Every AI step is capped by the time left
# under this budget. Sized to let a slow orchestrator degrade AND still leave a
# full single-shot fallback, comfortably inside the 300s Lambda timeout.
TOTAL_BUDGET = 200

# Wall-clock budget for a SYNCHRONOUS request. Sized for the ceiling of the gateway
# a DIRECT API caller sits behind: our CloudFront origin read timeout, 60s (see
# infrastructure/terraform/cloudfront_api.tf). The parse must degrade and answer
# before that, or the caller gets a bare 504 with no data at all.
#
# This budget CANNOT protect a caller behind a tighter gateway. The UAT console, for
# one, reaches this API through a Next.js route handler on AWS Amplify Hosting,
# whose SSR compute has a HARD 30s request timeout - not configurable, no quota to
# raise, and Next's `maxDuration` is not honored there. A measured single-shot parse
# of even a *typical* two-role resume takes ~20s, so a complete synchronous parse
# does not fit 30s once extraction, normalization and transfer are counted: there is
# no budget value that makes it work. Such callers must not block on a parse at all
# - they pass `async_only` and poll instead (see app/api/v1/endpoints/resume.py).
SYNC_WALL_BUDGET = 50

# -- Reserves (time held back from one step for the step that must follow) ------

# Held back from the orchestrator for the single-shot fallback + scoring - large
# enough for a full TIMEOUT_AI_PARSE fallback attempt after a degrade.
FALLBACK_RESERVE = 100
# Below this much *usable* window (i.e. on top of FALLBACK_RESERVE) the orchestrator
# cannot do anything worthwhile, so go straight to the cheaper single-shot.
MIN_ORCHESTRATOR_WINDOW = 15
# Hard asyncio net above the orchestrator's own self-bounded budget.
ORCHESTRATOR_HARD_NET = 10
# Floor for the async single-shot. The async path has room to try even when the
# budget is nominally spent, so it never opens a zero-length window.
MIN_ASYNC_AI_TIMEOUT = 15.0

# Time held back from the SYNC single-shot parse for the section-only "enrich"
# pass that runs when it times out. The single-shot is capped this many seconds
# BELOW the wall budget so a resume that would run long is cut early, leaving room
# to recover the semantic sections (headline, secondary phone, education
# locations, skills, certs) with fast section agents instead of dropping to the
# contact-only floor. Only used by a sync caller that RETURNS the partial.
SYNC_ENRICH_RESERVE = 14
# A PROBE caller promotes any partial to the async worker (full budget, complete
# parse), so the enrich output would be thrown away. Hold back almost nothing and
# hand the time to the single-shot instead - a bigger cap means more resumes
# finish synchronously.
PROBE_ENRICH_RESERVE = 3.0
# Smallest AI-parse window worth opening on the sync path. If less than this is
# left after extraction, don't start a call we know cannot land - degrade straight
# to the floor so the caller promotes to async while it still has budget to do so.
MIN_SYNC_AI_TIMEOUT = 8

# Time the sync path holds back from EXTRACTION for the AI parse + scoring that
# must follow it. Extraction used to run entirely outside the sync budget, on its
# own 60s/90s caps, so one slow step could blow the gateway ceiling before the AI
# parse even began - an independent source of 504s that this budget never saw.
SYNC_EXTRACT_RESERVE = 20
# Never hand an extraction step less than this; below it the step is pointless.
MIN_EXTRACT_TIMEOUT = 5

# Enrich: don't open the pass at all below this, and hold back a little for the
# agents' own wind-down (budget) and the asyncio net above them (hard net).
MIN_ENRICH_WINDOW = 9
ENRICH_AGENT_RESERVE = 3
ENRICH_HARD_NET = 1

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
    """Owns every deadline decision in one parse.

    Construct via `for_sync()` / `for_async()` - the ladder differs by caller and
    the constructor is what picks the wall budget.
    """

    total:   float
    sync:    bool
    probe:   bool
    started: float

    # -- construction ----------------------------------------------------------

    @classmethod
    def for_async(cls) -> "ParseBudget":
        """The async worker: full budget, orchestrator-first ladder."""
        return cls(total=TOTAL_BUDGET, sync=False, probe=False, started=time.monotonic())

    @classmethod
    def for_sync(cls, *, probe: bool = False) -> "ParseBudget":
        """A caller blocking on the HTTP response: gateway ceiling, single-shot-first.

        The full orchestrator was tried on this path and silently dropped ALL work
        history - the per-role fan-out got cancelled under the tight budget. That is
        why sync runs single-shot as PRIMARY and async does not. Do not unify them.

        `probe=True` means the caller will re-dispatch any partial to the async worker
        rather than return it, so the enrich pass would be wasted work.
        """
        return cls(total=SYNC_WALL_BUDGET, sync=True, probe=probe, started=time.monotonic())

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
        """Cap one extraction step by the time actually left.

        The async worker keeps the full per-step cap. A sync request cannot: it must
        leave room for the AI parse that follows and still answer inside the gateway
        ceiling, so the step is clamped to what the budget can really afford.
        """
        if not self.sync:
            return cap
        return max(MIN_EXTRACT_TIMEOUT, min(cap, self.remaining() - SYNC_EXTRACT_RESERVE))

    # -- AI parse: async ladder ------------------------------------------------

    def can_afford_orchestrator(self) -> bool:
        """False when extraction (e.g. a slow OCR pass) already ate the budget - go
        straight to the cheaper single-shot rather than start a fan-out that will be
        cancelled halfway."""
        return self.remaining() > FALLBACK_RESERVE + MIN_ORCHESTRATOR_WINDOW

    def for_orchestrator(self) -> Window:
        budget = min(TIMEOUT_ORCHESTRATOR, self.remaining() - FALLBACK_RESERVE)
        return Window(budget=budget, timeout=budget + ORCHESTRATOR_HARD_NET)

    def for_async_ai_parse(self) -> float:
        return min(TIMEOUT_AI_PARSE, max(MIN_ASYNC_AI_TIMEOUT, self.remaining()))

    # -- AI parse: sync ladder -------------------------------------------------

    def enrich_reserve(self) -> float:
        return PROBE_ENRICH_RESERVE if self.probe else SYNC_ENRICH_RESERVE

    def for_sync_ai_parse(self) -> float:
        """The single-shot window on the sync path.

        Clamped to what is genuinely left. An earlier `max(15.0, ...)` floor here meant
        a slow extraction could still hand the AI a 15s window the budget could not
        afford, overshooting the gateway ceiling - the very 504 this budget exists to
        prevent. The result may be too small to use; check `is_viable_sync_window`.
        """
        return min(TIMEOUT_AI_PARSE, self.remaining() - self.enrich_reserve())

    @staticmethod
    def is_viable_sync_window(window: float) -> bool:
        """Is this window worth opening a call into at all? If not, degrade NOW so the
        caller can promote to async while it still has budget to dispatch."""
        return window >= MIN_SYNC_AI_TIMEOUT

    # -- Sync enrich (section-only recovery after a single-shot timeout) --------

    def can_afford_enrich(self) -> bool:
        """Probe callers never enrich: they promote the partial to async instead, so
        the work would be discarded."""
        return not self.probe and self.remaining() > MIN_ENRICH_WINDOW

    def for_enrich(self) -> Window:
        left = self.remaining()
        return Window(budget=left - ENRICH_AGENT_RESERVE, timeout=left - ENRICH_HARD_NET)

    # -- Post-parse enrichment (both ladders) ----------------------------------

    def for_specialty_ai(self) -> float:
        """Tier-4 specialty resolution. Non-positive means skip it - it is optional and
        must never be the reason a parse fails."""
        return self.remaining() - SPECIALTY_SCORING_RESERVE

    def can_afford_city(self) -> bool:
        return self.remaining() > MIN_CITY_WINDOW

    def for_city(self) -> float:
        return self.remaining() - CITY_HARD_NET
