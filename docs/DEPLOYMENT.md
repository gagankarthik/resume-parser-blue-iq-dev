# Deployment & Operations

How the Resume Parser API is built, shipped, and run in production. Read this before changing
anything that touches infrastructure.

- **Region:** `us-east-2`
- **Runtime:** two container-image AWS Lambda functions from one image (API + async Worker)
- **Deploy trigger:** push to `main` → GitHub Actions
- **IaC status:** Terraform *describes* the stack but does **not** control it — see
  [Infrastructure reality](#infrastructure-reality) before running any `terraform` command.

---

## 1. CI/CD pipeline

Three GitHub Actions workflows in `.github/workflows/`:

| Workflow | Trigger | Does |
|---|---|---|
| `pr-check.yml` | every PR | The authoritative quality gate: `ruff` + `mypy` + `pytest --cov-fail-under=70`. |
| `deploy.yml` | push to `main` | Build `Dockerfile.lambda` → push to ECR (SHA tag) → `update-function-code` on **both** functions → wait → retrying health smoke test. |
| `rollback.yml` | `workflow_dispatch` | Verify a tag exists in ECR → `update-function-code` on both functions → smoke test. Shares a concurrency group with `deploy` so the two cannot race. |

**CI owns the image, and only the image.** It never touches environment variables, memory, or
timeout. A redeploy will **not** pick up a new secret or config value — those are set on the
function itself (see §4).

Both Lambdas run the **same image**, differing only in the entry-point command
(`app.handlers.lambda_handler.handler` for the API, `app.handlers.worker_lambda.handler` for the
Worker), so every deploy and rollback updates both to keep them on one code version.

---

## 2. AWS services

| Service | Role |
|---|---|
| **Lambda** (API) | Serves the HTTP API via a Function URL; enqueues async jobs. |
| **Lambda** (Worker) | Drains the SQS queue and runs the OCR / multi-agent pipeline. Sized independently for the heavy path. |
| **Lambda Function URL** | Public HTTPS entry point (no API Gateway). |
| **SQS** (+ DLQ) | Carries async parse jobs from the API to the Worker; a dead-letter queue captures poison messages after 3 failed deliveries. |
| **DynamoDB** (7 tables) | `api-keys`, `jobs` (1 h TTL), `batches` (24 h TTL), `webhooks`, `companies`, `audit-logs` (90 d, content-free), `feedback` (90 d — the only PII store). |
| **S3** | Transient resume bytes under `temp/{job_id}/`, deleted after parsing; a lifecycle rule expires any leak after 1 day. |
| **Textract** | OCR for scanned PDFs / images (tiered behind Tesseract). |
| **ECR** | Container image registry; CI pushes SHA-tagged images. |
| **CloudFront + ACM** | Optional branded domain in front of the Function URL — see [`custom-api-domain.md`](./custom-api-domain.md). |
| **CloudWatch** | Logs (30-day retention) and alarms on DLQ depth, queue backlog, and Worker errors. |
| **IAM** | Least-privilege execution roles — the API can only *produce* to the queue, the Worker only *consume*. |

---

## 3. OpenAI

Every model call — the single-shot parser, all orchestrator agents, and the specialty-AI tier —
runs through one shared executor, `app/services/llm/client.py::structured_parse`.

- **Model:** `gpt-4.1-mini` (structured outputs), deterministic decoding (`temperature=0`, fixed
  seed) so identical resumes parse consistently. Pin a dated model snapshot in production to hold
  the fingerprint stable.
- **Resilience (built in):** retry with backoff + jitter on `429 / 5xx / timeout / connection`
  (honoring `Retry-After`); a per-process **circuit breaker** that fast-fails to the deterministic
  rule floor during an outage; an optional **same-model Azure OpenAI fallback** (off unless
  configured — same model, so no accuracy shift).
- **Backpressure:** the Worker's reserved concurrency and the SQS event-source mapping's
  `maximum_concurrency` bound concurrent OpenAI calls during a batch burst; an opt-in per-process
  token-bucket (`LLM_RATE_LIMIT_RPM`) smooths the per-role fan-out.

All OpenAI/resilience settings are documented in [`.env.example`](../.env.example).

---

## 4. Configuration & secrets

Environment variables and function sizing are **set on the Lambda functions by hand** — nothing in
this repo or CI manages them. Secrets are plain Lambda env vars (not SSM). The critical ones:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI credential |
| `GIG_SPECIALTIES_API_KEY` | GigHealth Partner key (also spelled `GIG_SPECIAILITIES_API_KEY` — both accepted); enables the live city lookup |
| `AUTH_SECRET` | Signs self-serve session tokens; the app refuses to boot in production on the dev default |
| `ADMIN_API_TOKEN` | Gates `/admin/*` |
| `WORKER_QUEUE_URL` | SQS queue the API enqueues onto — **required in production** (see §5) |

To change runtime config, update the value **on the function** and (for reproducibility) mirror it
into [`.env.example`](../.env.example). A code redeploy will not apply it.

---

## 5. The async worker (SQS) — provisioning reality

The code enqueues async jobs onto SQS and expects a separate Worker Lambda to drain the queue. The
API gates this on `WORKER_QUEUE_URL`:

- **Set** → jobs go to SQS and the Worker Lambda processes them (production path).
- **Unset** → the API falls back to in-process `BackgroundTasks`. This is the **local-dev** path
  only; it is not a reliable production worker.

> **Before shipping the SQS code to production, the following must exist and be wired up:** the SQS
> queue + DLQ, the Worker Lambda (same image, `worker_lambda.handler` command, its own
> memory/timeout/reserved concurrency), the SQS → Worker event-source mapping, the Worker's IAM
> consume permissions and the API's IAM produce permission, and `WORKER_QUEUE_URL` set on the API
> function. Deploying the enqueue code *before* this is provisioned will silently degrade async
> parsing (every parse job now runs on this path) to the in-process fallback.

The Terraform in `infrastructure/terraform/` (`sqs.tf`, `lambda.tf`, `iam.tf`) is the authoritative
*description* of this stack — but see the next section on how it must actually be applied.

---

## 6. Infrastructure reality

`infrastructure/terraform/` **describes** the production stack but has **never been applied**: the
state bucket it points at (`resume-parser-tfstate`) does not exist, and Terraform holds no state
for any live resource. Every running resource — the Lambda, the 7 DynamoDB tables, the S3 bucket,
the IAM roles — was created **outside** Terraform.

**Consequences:**

- `terraform apply` is **not** a safe way to change anything. With empty state it would try to
  *create* a second copy of every resource, not update the running ones.
- New infrastructure (like the SQS worker in §5) must be created either **by hand** (console / AWS
  CLI) or by **adopting** the stack into Terraform one resource at a time via `terraform import`
  (**not** a blind apply).

The adoption plan is tracked in [`CLEANUP_PLAN.md`](./CLEANUP_PLAN.md) §E. Until then, treat the
Terraform files as documentation and change live config on the resource itself.

---

## 7. Runbook

**Deploy**

1. Merge to `main`. `deploy.yml` builds, pushes, updates both functions, and smoke-tests `/health`.
2. If the deploy introduces or changes any env var / sizing, apply that change on the functions by
   hand — CI will not.

**Rollback**

1. Run `rollback.yml` (`workflow_dispatch`) with the target image tag (a past commit SHA).
2. It verifies the tag in ECR, updates both functions, and smoke-tests.

**Health**

- `GET /api/v1/health` — liveness + dependency probe. The deploy/rollback smoke tests retry it.
- CloudWatch alarms fire on DLQ depth (a job that failed past all retries), queue backlog
  (backpressure), and Worker errors.

**Local development**

```bash
poetry install          # deps (poetry.lock is committed — reproducible builds)
docker-compose up        # API + LocalStack (S3 + DynamoDB); Dockerfile is dev-only
poetry run pytest        # full suite
```
