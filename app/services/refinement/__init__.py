"""Feedback-driven instruction refinement (the self-improvement loop).

Reviewer corrections submitted to `POST /resume/{job_id}/feedback` are stored as
(original JSON, corrected JSON, changed_fields). This package closes the loop:

  1. `field_map`   - map each corrected leaf path to the agent that produced it.
  2. `aggregator`  - turn a batch of feedback into per-agent correction examples.
  3. `refiner`     - an LLM "refiner" proposes concise, imperative rules per agent
                     grounded in those examples (agent-based instruction refinement).
  4. `store`       - the hot-path applicator: agents append the ACTIVE learned rules
                     for their name to their system prompt (in-process TTL cache).

Generation (1-3) is admin-triggered and runs OFF the parse hot path
(`/api/v1/admin/refinement/*`); only step 4 touches parsing, and it degrades to a
no-op if the store is empty or unreachable, so behaviour is unchanged until a
proposal is reviewed and approved.
"""
