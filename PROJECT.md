# PROJECT.md

The reference document for what this system is, why it exists, and the rules that keep it
from degrading. If you are about to change something, read the section that owns it first.

---

## 1. Mission

**Eliminate manual re-keying of healthcare resumes into placement systems.**

Staffing coordinators read a nurse's resume and hand-type it into a candidate profile - slowly,
inconsistently, and with errors that surface later as a bad placement. This service removes that
step: upload a resume, receive a complete candidate record that maps directly onto the
placement platform's own forms and IDs.

## 2. Vision

The parser is not an OCR tool with a schema bolted on. It is a **healthcare-domain expert
encoded as software**. It knows a certification is not a state licence. It knows `RN, BSN, CCRN`
is three credentials and not a surname. It knows a travel nurse's "agency" is not their
workplace, and decomposes the assignment into the facilities where the work actually happened.
It resolves "ICU" to a *different* catalog ID for an RN than for a CNA.

That domain knowledge - not the code around it - is the product.

## 3. Scope

**Client:** BlueIQ. Downstream consumer is the **GigHealth** staffing platform; its
specialty / facility / geography / city IDs are the vocabulary we normalize into.

**In scope**
- Digital PDF, DOCX, RTF (synchronous path)
- Scanned PDF, PNG / JPG / TIFF / WEBP via OCR (asynchronous path)
- Per-field confidence scores, so low-quality records can be routed to human review
- Credential and state-licence capture with post-nominals preserved
- 350+ clinical specialties resolved to canonical, profession-scoped catalog IDs
- Batch submission, webhooks, and a correction-feedback loop

**Explicitly out of scope**
- Candidate ranking, scoring, or matching against jobs - we structure data, we do not judge it
- Resume storage. Bytes touch S3 transiently and are deleted in a `finally`. **Exception:** the
  `feedback` table intentionally persists original + corrected JSON for 90 days. See §8.
- Any non-healthcare vertical

---

## 4. The non-negotiable invariants

These are the promises the system makes. Every one of them was learned from a production
failure. **A change that breaks one of these is a regression, no matter what the tests say.**

1. **Never return nothing.** A hard resume degrades through a ladder - multi-agent -> single-shot
   -> deterministic floor - but the caller always receives a structured record. The floor
   (`heuristic_parser`) cannot time out and cannot be empty.
2. **Never return a silent partial.** If the parse degraded, `partial=True` and a human-readable
   warning says so. A degraded record that *looks* complete is worse than an honest failure.
3. **Never fabricate.** Regex-extracted contact anchors are passed to the LLM as ground truth.
   Unmatched specialties come back with `specialty_id=None` - never a guessed ID. LLM-returned
   catalog IDs are validated against the shortlist and dropped if hallucinated.
4. **Never drop a role.** The `StructureAgent` maps every role and its exact bullet count. If a
   per-role extraction fails, the role is *stubbed from the boundary*, not omitted. Positional
   1:1 with the structure map is what the `ValidatorAgent` checks.
5. **Never blow the gateway ceiling.** A synchronous caller sits behind a timeout we do not
   control. The parse must degrade and answer *before* it, or the caller gets a bodyless 504
   with no data at all. See §6 - this is the system's hardest constraint.
6. **Never log PII.** Resume filenames embed candidate names. Log the extension and the length.

---

## 5. Architecture

Two AWS Lambdas (one container image, two entry points, `us-east-2`). The **API Lambda** serves
the HTTP API and, for async work, pushes a job onto an **SQS queue** and returns. A separate
**Worker Lambda** drains that queue and runs the heavy OCR / multi-agent pipeline, sized for that
load independently of the thin request path. No API Gateway - a Lambda Function URL, optionally
fronted by CloudFront for the custom domain. State is in DynamoDB (7 tables). Resume bytes pass
through S3 transiently.

The queue is what makes the async path operable: a visibility timeout above the ~130s
orchestrator ceiling stops a still-running job being redelivered; SQS retries transient failures
for free; queue depth is a backpressure metric we alarm on; and a **dead-letter queue** turns a
poison message into a visible, alertable event after `maxReceiveCount` (3) failed deliveries,
instead of a job that silently never finishes. See `infrastructure/terraform/sqs.tf`.

```
Client --HTTPS--> CloudFront (60s origin read timeout) --> API Lambda (Function URL)
                                                              |
                                                        Mangum -> FastAPI
                                                              |
                            +---------------------------------+--------------+
                            |              app/services/pipeline.py           |
                            |                                                 |
                            |  classify -> extract -> clean -> anchors ->        |
                            |  sections -> PARSE -> validate -> normalize ->     |
                            |  catalog-match -> score                          |
                            +---------------------------------+--------------+
                                                              | partial? (or scanned)
                                                     SendMessage -> SQS worker queue
                                                              |         \
                                                     (maxReceiveCount)   -> DLQ (alarm)
                                                              v
                                                   Worker Lambda, full budget
                                                   -> DynamoDB job -> webhook
```

### The pipeline stages

| Stage | Module | Job |
|---|---|---|
| Classify | `extraction/classifier.py` | File type -> extraction strategy |
| Extract | `extraction/{pdf,docx,rtf,ocr}_extractor.py` | Bytes -> text. PyMuPDF is layout-aware; a garbled CID text layer falls back to OCR |
| Clean | `pipeline._clean_text` | Unicode-safe scrub. Preserves international names |
| Anchors | `parsing/rule_parser.py` | Regex email/phone/URLs -> fed to the LLM as ground truth |
| Sections | `parsing/section_detector.py` | Header segmentation |
| **Parse** | `parsing/orchestrator.py` - `ai_parser.py` - `heuristic_parser.py` | The three-tier ladder. See below |
| Validate | `models/schemas/resume.py` | Pydantic v2 enforcement |
| Normalize | `normalization/normalizer.py` | Degrees, dates, credentials, geography/facility IDs, compliance |
| Catalog match | `normalization/specialty_matcher.py` - `city_resolver.py` | Free text -> GigHealth IDs |
| Score | `scoring/confidence_scorer.py` | Per-section + overall 0-1 confidence |

### The parse ladder

**Async (full 200s budget):** multi-agent orchestrator -> single-shot -> deterministic floor.

**Sync (tight budget):** single-shot is *primary* -> on timeout, deterministic floor + a
section-only "enrich" pass, merged by `_backfill_from_floor`.

> The full orchestrator was tried on the sync path and **silently dropped all work history** -
> the per-role fan-out got cancelled under the tight budget. This is why sync and async use
> different ladders. Do not "simplify" them back together.

### The shared LLM executor (`services/llm/`)

Every LLM call - single-shot, all the orchestrator agents, and the specialty-AI tier - goes
through **one** function, `llm.client.structured_parse`, so the resilience policy is defined
once instead of drifting across call sites. It wraps a provider call with:

- **Transient retry** on 429 / 5xx / timeout / connection with exponential backoff + jitter,
  honoring a `Retry-After` header when present. (A 4xx like 400 is a content error - not retried.)
- **A per-process circuit breaker.** After N consecutive infra failures it opens and calls
  fast-fail, so during an OpenAI outage jobs degrade to the deterministic floor **immediately**
  (invariant 1) instead of every one hanging through full backoff. It half-opens to probe recovery
  and is process-global so a warm container remembers the outage across invocations.
- **An optional Azure OpenAI same-model fallback.** Off unless configured (today: OpenAI /
  `gpt-4.1-mini` only). Same model + params → no accuracy shift, so it needs no separate
  benchmark; a cross-*vendor* fallback would and is intentionally not wired. A content error
  (repetition-loop, empty output, 400) does **not** fall over - the same model would fail identically.
- **A token-bucket RPM limiter** ahead of the fan-out (per process, opt-in). The cross-instance
  backpressure lever in production is the SQS event-source mapping's `maximum_concurrency` +
  Lambda reserved concurrency (see §5 diagram), which a per-process bucket cannot replace.

The concurrency semaphore that caps the per-role fan-out still lives in `agents/base.py`.

### The agents (`services/parsing/agents/`)

All inherit `BaseAgent`: OpenAI structured outputs, `temperature=0` + fixed seed, retry with
jitter, and a **per-event-loop client + semaphore** - rebuilt when the loop changes, which is
essential for warm Lambda reuse.

| Agent | Stage | Job |
|---|---|---|
| `StructureAgent` | 1, sequential | Map every role + exact bullet count; decompose travel/agency umbrellas per facility |
| `PersonalInfoAgent` | 2, parallel | Name, post-nominals, headline, address, phones, summary |
| `WorkExperienceAgent` | 2 | **One LLM call per role**, told the expected bullet count |
| `EducationAgent` | 2 | Degrees, institutions, in-progress degrees |
| `CredentialsAgent` | 2 | Skills / certifications / **state licences** / associations - one call, so classification sees all three |
| `SupplementalAgent` | 2 | References, awards, publications, languages |
| `ValidatorAgent` | 3, sequential | Re-extract any role whose bullet count ≠ the map |
| `SpecialtyMatchAgent` | post | Tier-4 batched specialty -> catalog ID |

### The catalogs (`services/normalization/`)

| Catalog | Source | Live API at parse time? |
|---|---|---|
| Specialty (282 KB) | `app/data/specialty_catalog.json` | No - tiers 1-3.5 offline; tier 4 calls the **LLM**, not GigHealth |
| Facility (1.4 MB) | `app/data/facility_catalog.json` | No |
| Geography (7.7 KB) | `app/data/geography_catalog.json` | No |
| **City** | *cannot be snapshotted* | **Yes** - live GigHealth `/cities` fuzzy search |

Snapshots are refreshed out-of-band by `scripts/refresh_*_catalog.py`. **All four catalogs are
optional by design:** a missing or garbled file yields an empty catalog and a `null` ID. Parsing
is never broken by a bad catalog.

**Specialty matching is 5-tier and profession-scoped** (RN-ICU ≠ CNA-ICU):
name (1.00) -> full_name (0.95) -> keywords (0.80) -> deterministic fuzzy (<=0.94) -> batched LLM
(capped 0.70 unless deterministically verifiable).

---

## 6. The time-budget problem - read this before touching `pipeline.py`

This is the hardest constraint in the system and the source of most of its churn.

A synchronous caller sits behind a gateway we do not control:

| Caller | Ceiling | Source |
|---|---|---|
| Direct API | **60s** | CloudFront `origin_read_timeout` |
| UAT console | **30s, hard** | Amplify SSR compute. Not configurable. `maxDuration` is ignored |
| Lambda itself | 300s | Function timeout |

A single-shot parse of even a *typical* two-role resume takes ~20s; a dense 12-role radiology
resume takes 39-55s. **There is no budget value that makes a complete synchronous parse fit
30s.** Callers behind a tight gateway must not block on a parse at all - they pass `async_only`
and poll. This is why `/resume/parse` runs a fast *probe* and promotes to the async worker
rather than returning a degraded record.

`pipeline.py` currently carries **eleven** tuned constants implementing this. Each one was added
by a production incident. **They are correct, and they are in the wrong place** - deadline
arithmetic is interleaved with parse orchestration, so there is no seam to put the next fix in.
Extracting a budget object is the single highest-value refactor available. See `CLEANUP_PLAN.md`.

---

## 7. How to change this system without degrading it

The failure mode this document exists to prevent: *a fix has no natural home, so it is bolted on
as one more special case, and the code gets worse with every PR.*

1. **A bug fix that adds a constant is a design signal.** If the fix is "add a new reserve /
   threshold / flag," the abstraction is missing. Add the seam, then the fix.
2. **A fix belongs at the layer that owns the concept.** Timeouts belong to a budget. Catalog
   IDs belong to a matcher. Degradation belongs to the ladder. Not to `run()`.
3. **Pin the behavior before you change it.** Every one of the invariants in §4 has tests. If
   you are changing behavior, the test that proves the old behavior must fail *first*.
4. **Never simplify away a comment that names an incident.** `pipeline.py:262-264` explains why
   sync and async use different ladders. That comment is load-bearing.
5. **A rewrite is not a refactor.** The catalogs, the prompts, and the 560 tests are the
   product. The code shape around them is replaceable - freely, incrementally, behind the suite.

---

## 8. Known truths the docs used to get wrong

Recorded here because stale docs cost more than no docs.

- **Resume content IS stored, in one place.** The `feedback` table persists original + corrected
  parsed JSON - full resume PII - for 90 days. Terraform says so; the marketing copy did not.
  Any privacy statement to the client must disclose this.
- **Two Lambdas, decoupled by SQS.** The API Lambda enqueues async jobs onto an SQS queue; a
  separate Worker Lambda drains it (with a DLQ for poison messages). This replaced the old
  single-function self-invoke (`InvocationType="Event"`) design - if a doc still describes the API
  self-invoking, it is stale. See §5 and `infrastructure/terraform/sqs.tf`.
- **Secrets are plain Lambda env vars,** not SSM Parameter Store.
- **There is no rate-limit DynamoDB table.** `core/rate_limit.py` is an in-process fixed-window
  counter, **disabled by default**. It does not survive a cold start and does not coordinate
  across concurrent Lambdas.
- **7 DynamoDB tables**, not 6. Default model is `gpt-4.1-mini`, not GPT-4o.
- **Region is `us-east-2`.** The `Makefile` says `us-east-1` and targets Lambda function names
  that do not exist. Do not trust it.
- **Terraform does not manage production.** `infrastructure/terraform/` has **never been
  applied** - its state bucket does not exist. Every live resource was created outside it, and
  Terraform holds no state for any of them. The config *describes* the stack; it does not
  *control* it, and `terraform apply` would try to create a second copy of all 19 resources
  rather than update the running ones. This document said "Terraform owns env/sizing" until
  2026-07-14; that was aspiration read as fact, and it sent a `city_id` investigation down the
  wrong path. Adoption plan: `CLEANUP_PLAN.md` §E.

---

## 9. Operational facts

- **Deploy:** push to `main` -> GitHub Actions builds `Dockerfile.lambda`, pushes to ECR,
  `update-function-code`, then a retrying health smoke test. **CI owns the image, and only the
  image** - it never touches env vars or sizing.
- **Env vars and sizing are set on the function itself,** by hand. Nothing manages them: see the
  Terraform note in §8. A redeploy will not pick up a new secret, and neither will an apply.
- **Rollback:** `rollback.yml` (`workflow_dispatch`) -> verify tag in ECR -> update -> smoke test.
  Shares a concurrency group with deploy so the two cannot race.
- **Quality gate:** ruff + mypy + `pytest --cov-fail-under=70`. Current: **560 passing, 78%**.
- **Local:** `docker-compose up` (LocalStack: S3 + DynamoDB). Note `Dockerfile` is dev-only -
  `Dockerfile.lambda` is what ships.
