# Cleanup Plan

**Verdict: do not rewrite.** Baseline is 478 tests passing at 78% coverage, above the 70% CI
gate. The package boundaries are sound. The domain modeling is good. A greenfield rebuild would
discard the catalogs, the tuned prompts, and 478 tests' worth of hard-won resume edge cases, and
would re-derive the same eleven timeout constants the hard way - in front of the client.

The "every PR makes it worse" feeling is real, but it has **one** structural cause (§B1), not a
thousand. Fix the seam and the degradation stops.

Every item below is independently shippable and keeps the suite green.

---

## A. Ship first - safety, correctness, and footguns

Low risk, high value. None of these change parsing behavior.

### A1. Commit `poetry.lock` - **highest priority in this document**
`poetry.lock` is not in the repo. Both Dockerfiles do `COPY pyproject.toml poetry.lock* ./` -
the `*` glob **silently succeeds when the file is absent**. Production images therefore resolve
dependencies *fresh at build time*. Two deploys of byte-identical source can ship different
versions of `openai`, `pydantic`, or `PyMuPDF`, and you would have no way to tell.

- Generate and commit `poetry.lock`.
- Drop the `*` from both Dockerfiles so a missing lockfile **fails the build** instead of
  silently degrading it.
- Replace deprecated `poetry install --no-dev` with `--only main` (both Dockerfiles).

### A2. Fix or delete the `Makefile` deploy targets
`make deploy-lambda` calls `update-function-code` on `resume-parser-api` and
`resume-parser-worker`. **Neither function exists** - the real one is
`resume-parser-production-api`. It also defaults to `AWS_REGION=us-east-1`; you are deployed in
`us-east-2`. Anyone who runs this either gets a confusing failure or, worse, touches the wrong
account. CI owns deploys - delete these targets rather than repair them.

### A3. The privacy claim is false - fix the docs
`README.md:225` and `docs/ARCHITECTURE.md:20` state resume content is never stored. The
**`feedback` table persists original + corrected parsed JSON - full resume PII - for 90 days.**
`dynamodb.tf` says so in a comment; the client-facing docs do not. This is a disclosure problem,
not a code problem. Correct the docs. (Or, if the claim must hold, the feedback loop needs to
store a redacted diff - a product decision, not a cleanup one.)

### A4. Stop running the full test suite twice per PR
`deploy.yml` triggers on `pull_request` **and** `pr-check.yml` triggers on `pull_request`. Every
PR runs lint + mypy + the suite twice, and the two disagree: `pr-check` enforces
`--cov-fail-under=70`, `deploy.yml` enforces no gate at all. Restrict `deploy.yml` to
`push: [main]` and let `pr-check.yml` be the single authoritative gate.

### A5. Resolve the OIDC contradiction - ⚠️ DEFERRED, needs AWS verification

Terraform provisions and outputs a GitHub Actions OIDC role (`iam.tf:127`, `outputs.tf:16-19` -
"paste into GitHub repo secret AWS_ROLE_ARN"), but all three workflows authenticate with
**long-lived static `AWS_ACCESS_KEY_ID` secrets**. The intent was clearly to go keyless; the
wiring was never finished.

**Do not just flip the workflows to `role-to-assume`.** The role's trust policy names an OIDC
provider ARN:

```hcl
identifiers = ["arn:aws:iam::${account_id}:oidc-provider/token.actions.githubusercontent.com"]
```

...but **no `aws_iam_openid_connect_provider` resource exists anywhere in the stack.** The provider
is referenced, never created. So it is either (a) absent, in which case the role cannot be
assumed and switching the workflows breaks every deploy, or (b) present but created out-of-band,
in which case it is unmanaged infrastructure that Terraform does not know about.

**Before wiring this up, verify:**

```bash
aws iam list-open-id-connect-providers          # does the GitHub provider exist?
aws iam get-role --role-name resume-parser-production-github-actions-deploy
gh secret list                                   # is AWS_ROLE_ARN set?
```

Then, in order: add the missing `aws_iam_openid_connect_provider` to Terraform (`terraform
import` it if it already exists - a bare `apply` will fail with `EntityAlreadyExists`), set the
`AWS_ROLE_ARN` secret, add `permissions: id-token: write` to each workflow, swap
`aws-access-key-id`/`aws-secret-access-key` for `role-to-assume`, confirm one green deploy, and
only *then* delete the static keys from repo secrets and IAM.

Left as-is in this pass: switching deploy auth on unverifiable assumptions is exactly how you
lose the ability to ship.

### A6. Declare the package in `pyproject.toml`
`poetry install` fails on the root package under Poetry 2.x (`No file/folder found for package
resume-parser`); CI only passes because it pins 1.8.4. Add
`packages = [{ include = "app" }]`, or set `package-mode = false`.

---

## B. The rot vector - the actual fix for "every PR makes it worse"

### B1. Extract the time budget out of `pipeline.py`

**This is the single highest-value change in the codebase.**

`pipeline.py` carries **eleven** tuned constants - `_TOTAL_BUDGET`, `_SYNC_WALL_BUDGET`,
`_FALLBACK_RESERVE`, `_SYNC_ENRICH_RESERVE`, `_SYNC_EXTRACT_RESERVE`, `_MIN_SYNC_AI_TIMEOUT`,
`_MIN_EXTRACT_TIMEOUT`, `_TIMEOUT_ORCHESTRATOR`, `_TIMEOUT_AI_PARSE`, `_TIMEOUT_EXTRACTION`,
`_TIMEOUT_OCR` - each added by a separate production incident, each carrying a paragraph of
comment explaining the 504 that created it.

They are **correct**. They are also **in the wrong place**: `run()` is simultaneously
orchestrating a parse *and* hand-solving a deadline-arithmetic problem, with the two interleaved
line by line. There is no seam. So when the next timeout bug arrives, the only available move is
to add a twelfth constant and another branch - which is precisely the degradation you have been
feeling.

**The fix:** a `ParseBudget` object that owns all deadline arithmetic.

```python
budget = ParseBudget.for_sync(probe=True)   # or .for_async()
budget.remaining()
budget.for_extraction(cap=_TIMEOUT_EXTRACTION)
budget.for_ai_parse()
budget.can_afford_orchestrator()
```

`run()` then reads as a pipeline again, and the budget rules become **unit-testable in
isolation** - today they are only reachable through a full parse. The next timeout fix goes
*into `ParseBudget`*, which is a place that exists.

- Pure refactor. Same constants, same numbers, same behavior.
- Pin first: `tests/unit/test_pipeline_degradation.py` already covers the ladder. Add direct
  `ParseBudget` tests for each of the eleven rules.
- **Do not** unify the sync and async ladders while doing this. They differ for a reason
  (`pipeline.py:262-264`): the full orchestrator on the sync path silently dropped all work
  history when the per-role fan-out got cancelled.

### B2. Split `normalizer.py` (805 LOC)
One module currently owns degree mapping, date normalization, credential bucketing, employment
-type detection, clinical-rotation routing, geography/facility ID stamping, street-address
refinement, bed-count sanitization, compliance scanning, **and** gap flagging. Split it along the
lines the `normalization/` package already uses for its catalogs. Mechanical, well-tested
(`test_normalizer.py` is 66 tests), low risk.

---

## C. Duplication - two implementations to keep in sync

Each of these is a place where a future fix will get applied to one copy and not the other.

| # | Duplication | Locations |
|---|---|---|
| C1 | `_slug()` + `_public()` byte-for-byte identical; company-creation body near-identical | `admin.py:52,61,72` / `auth.py:58,63,80` |
| C2 | **Webhook CRUD implemented twice** - same validation, same SSRF guard, same secret-once semantics | `webhooks.py` (API-key scoped) / `admin.py:356-414` (admin scoped) |
| C3 | API-key issuance implemented twice | `admin.py:174-191` / `account.py:47-58` |
| C4 | **Extraction rules prompt maintained twice** - the single-shot prompt restates the certification/licence classification rules nearly verbatim against the per-agent versions. `prompts.py:3-4` *explicitly acknowledges the drift risk*; shared `CORE_RULES` covers only part of it | `parsing/ai_parser.py:90-173` / `agents/prompts.py` + `agents/*.py` |

C4 is the dangerous one: prompt drift between the sync path (single-shot) and the async path
(agents) means **the same resume can be classified differently depending on which path parsed
it.** Extract every shared rule into `CORE_RULES`.

---

## D. Hygiene

- **D1. Delete `pipeline._fallback_from_anchors()`** (`pipeline.py:509-523`). Zero production
  callers - superseded by `heuristic_parser.parse()`. Its only references are in
  `test_pipeline_degradation.py:17,23,35`: **tests keeping dead code alive.** Delete both.
- **D2. Make the invisible regex visible** (`pipeline.py:576`). The character class contains
  *literal* `U+E000` and `U+F8FF` codepoints, so it renders everywhere as `[\ud800-\udfff-]` and
  reads like a bug. **It is not a bug** - verified: hyphens in `X-Ray` / `Med-Surg` /
  `Ricafort-Moulds` are preserved. Rewrite as explicit escapes,
  `r"[\ud800-\udfff-]"`. Identical behavior, greppable, and no longer one careless
  editor save from corruption.
- **D3. `docs/ARCHITECTURE.md` is materially wrong** - claims two Lambdas (there is one,
  self-invoking), SSM Parameter Store (secrets are plain env vars), a `rate-limits` DynamoDB
  table (does not exist), 6 tables (there are 7), GPT-4o (default is `gpt-4.1-mini`), and
  `reserved_concurrent_executions` backpressure (never set). Rewrite against `PROJECT.md`.
- **D4. `core/rate_limit.py` is in-process, fixed-window, and disabled by default.** It does not
  survive a cold start and does not coordinate across concurrent Lambdas. Either make it real
  (DynamoDB-backed) or document it as best-effort - but stop implying it is a rate limiter.
- **D5. Config naming.** `gig_specialties_api_key` also authenticates the *facilities*,
  *geographies*, and *cities* endpoints - the name is a lie. It is additionally aliased to the
  misspelled `GIG_SPEICAILITIES_API_KEY`, which is what `README.md:185` documents while
  `lambda.tf:30` sets the correct spelling. Rename to `gig_api_key`, keep both aliases for
  compatibility, fix the README.

---

## Sequencing

```
A1 A2 A3 A4 A5 A6   ->  safety + footguns, no behavior change, ship immediately
        |
        v
      B1            ->  ParseBudget. THE fix. Behavior-preserving, heavily pinned
        |
        v
    C4  ->  C1 C2 C3 ->  kill prompt drift first (it can change parse output), then the CRUD dupes
        |
        v
      B2            ->  split normalizer.py
        |
        v
    D1...D5           ->  hygiene + docs
```

Suite stays green at every step. Nothing deploys without review and explicit sign-off.
