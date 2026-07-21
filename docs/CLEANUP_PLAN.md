# Cleanup Plan

> **Status (2026-07-20): the budget/parse-flow items here are RESOLVED.** The time-budget seam this
> plan called for was extracted into `app/services/budget.py`, and the API then moved to **one
> uniform asynchronous flow** for every file (submit → worker parse → poll). The synchronous parse
> path, the sync time-budget ladder (`SYNC_WALL_BUDGET` and its reserves), the `sync`/`sync_probe`
> pipeline branches, and the `run_parse`/`enrich`/promote machinery were **removed** - so the
> "eleven timeout constants" and the "sync vs async prompt drift" concerns below no longer apply
> (there is a single async `ParseBudget` and a single parse ladder). Non-flow items (e.g. committing
> `poetry.lock`) may still be open. Kept as a historical record of the analysis.

**Verdict: do not rewrite.** Baseline is 560 tests passing at 78% coverage, above the 70% CI
gate. The package boundaries are sound. The domain modeling is good. A greenfield rebuild would
discard the catalogs, the tuned prompts, and 560 tests' worth of hard-won resume edge cases, and
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

### B1. Extract the time budget out of `pipeline.py` - ✅ DONE

**This was the single highest-value change in the codebase.**

Shipped as `app/services/budget.py`: `ParseBudget` owns every deadline decision, and
`pipeline.py` reads as a pipeline again (584 -> 453 lines). The rules are unit-tested
in isolation for the first time - `tests/unit/test_budget.py`, 34 tests, `budget.py` at
100% coverage. Same constants, same numbers, same behavior: the whole pre-existing
degradation suite passed unchanged.

Two things the extraction surfaced, which is the point of having a seam at all:

- **`TIMEOUT_ORCHESTRATOR` (130) is inert.** The orchestrator window is
  `min(130, remaining - FALLBACK_RESERVE)` = `min(130, 100)` = 100, so its own cap can
  never bind at the current numbers. Anyone "giving the orchestrator more time" by
  raising it would change nothing. Left as-is (a dormant ceiling, correct if the totals
  move) but now pinned by a test that says so.
- **The enrich window read the clock twice** - once for the agents' budget, once for the
  asyncio net above them - so the gap between them was silently smaller than the
  constants implied. `Window` now computes the pair from one reading.

The original diagnosis, kept for the record:

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

- **D1. Delete `pipeline._fallback_from_anchors()`** - ✅ DONE. Was dead (zero production
  callers, superseded by `heuristic_parser.parse()`); removed along with the two
  `test_pipeline_degradation.py` tests that only existed to exercise it.
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

## E. Production is not under Terraform's control - ⚠️ NEW, discovered 2026-07-14

**Nothing in `infrastructure/terraform/` has ever been applied.**

The S3 backend it declares - `resume-parser-tfstate` - **does not exist** (`NoSuchBucket`), and
`main.tf:14` still carries its own unfinished instruction: `# create this bucket manually first`.
There is a `make tf-bootstrap` target to create the bucket + lock table; it has evidently never
been run.

So the running Lambda, all 7 DynamoDB tables, the S3 bucket, the Function URL and both IAM roles
were created **outside Terraform**, and Terraform holds **no state for any of them**.

**Why this matters more than it looks.** The config *reads* as the source of truth, so anyone
debugging live config will trust it and be wrong.

That is not hypothetical - it is what happened here, and the failure is worth recording precisely
because the wrong conclusion was so easy to reach. `lambda.tf:29` wires `GIG_SPECIALTIES_API_KEY`
from a variable that `terraform.tfvars` supplies with a real 64-character key. Reading only the
repo, the state of the deployed function looks fully determined. It is not determined at all - the
file has never run - so **the config told us nothing about production either way.** The conclusion
drawn from it ("the key must be missing, apply Terraform to fix it") was wrong on both halves: the
key was already on the function, set by hand under the platform's misspelling
(`GIG_SPECIAILITIES_API_KEY`, which `config.py` accepts via `AliasChoices`), and `terraform apply`
would not have fixed it but tried to recreate the stack. The README shipped that advice; it has
been corrected.

The lesson is not "read Terraform more carefully". It is that **an unapplied Terraform config is
evidence about nothing**, and reasoning from it is worse than having no config at all, because it
produces confident wrong answers instead of an "I don't know" that would have sent us to the logs.

**`terraform apply` is currently DANGEROUS, not just useless.** With empty state it does not
reconcile the 19 existing resources - it tries to **create** them. Every one already exists.

**The fix is adoption, not application:**

1. `make tf-bootstrap` - create the state bucket + lock table (the target already exists).
2. `terraform import` each of the 19 resources, one at a time.
3. `terraform plan` until it reports **no changes**. That empty plan is the whole goal: it is the
   proof that the config finally describes reality.
4. Only then is `terraform apply` a safe way to change anything - and only then is
   "Terraform owns Lambda env" a true statement rather than an aspiration.

Until step 3 passes, treat `infrastructure/terraform/` as documentation and change live config on
the resource itself.

**Do not skip step 3 by importing and applying in one go.** A plan that is not empty after import
means the config and reality disagree, and an apply would resolve that disagreement in favor of
the config - on live infrastructure, silently.

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
