# Resume Parsing Backend - Enterprise Architecture

**Status:** Production - **Version:** 1.0 - **Last updated:** 2026-06-02

---

## 1. Executive Summary

The Resume Parsing Service is a production-grade HTTP API that converts **PDF, DOCX, and image
resumes** into clean, schema-validated JSON suitable for auto-filling candidate forms and
populating downstream systems.

It combines deterministic rule-based extraction with **OpenAI gpt-4.1-mini structured-output parsing**,
then validates, normalizes, and confidence-scores every result. The platform is built on a
**fully serverless AWS stack** (Lambda + DynamoDB + S3) and is engineered around three
non-negotiable principles:

| Principle | What it means |
|---|---|
| **Privacy first** | Resume *files* are **never stored** - deleted immediately after processing. Audit metadata is content-free. **One exception:** the opt-in feedback endpoint persists submitted original + corrected JSON (candidate PII) for 90 days. See [Data retention](#data-retention). |
| **Cost-aware accuracy** | OCR and AI are invoked only when needed; cheap deterministic paths run first. |
| **Operational simplicity** | No servers to manage - fully managed AWS services (Lambda, SQS, DynamoDB, S3) that scale to zero and back automatically. The one queue (SQS + DLQ) is serverless too: no broker to run. |

---

## 2. Design Principles

1. **Zero resume-data retention.** Raw files live in S3 only for the duration of processing and
   are removed in a `finally` block - deletion happens even when parsing fails. No resume text or
   PII is ever persisted.
2. **Right tool per document type.** Digital PDFs and DOCX are parsed directly; OCR is reserved for
   scanned/image documents, and Textract is used only as a fallback when local OCR confidence is low.
3. **Deterministic before probabilistic.** Regex anchors (email, phone, URLs) are extracted before
   the AI runs and passed in as ground truth - eliminating hallucination on contact details.
4. **Sync where possible, async where necessary.** Fast paths return inline; slow OCR paths run on a
   dedicated worker and notify via webhook + polling.
5. **Serverless and stateless.** All shared state lives in DynamoDB and S3, so compute scales
   horizontally with zero coordination.

---

## 3. High-Level Architecture

```text
+------------------------------------------------------------------+
|                          CLIENT APPLICATION                        |
|   Uploads resume (PDF / DOCX / image) - Renders structured fields  |
+------------------------------------------------------------------+
                                  |  HTTPS  (X-API-Key)
                                  v
+------------------------------------------------------------------+
|                  API LAMBDA   (FastAPI via Mangum)                 |
|                  Lambda Function URL - AuthType = NONE             |
|                                                                    |
|   * API-key authentication      (DynamoDB lookup, SHA-256)         |
|   * Sliding-window rate limiting (DynamoDB, TTL)                   |
|   * File validation             (extension + magic bytes + size)  |
|   * Document classification     (sync vs. async routing)          |
+------------------------------------------------------------------+
            |                                          |
   digital PDF / DOCX                          scanned PDF / image
   (synchronous)                               (asynchronous)
            |                                          |
            v                                          v
+--------------------------+         +------------------------------+
|   PARSING PIPELINE        |         |  S3 (temp) + SQS worker queue |
|   (runs inline in the     |         |  upload -> SendMessage          |
|    API Lambda)            |         +------------------------------+
|                           |                         |  SQS event-source mapping
|  returns JSON immediately |                         v   (DLQ after 3 retries)
+--------------------------+         +------------------------------+
            |                          |   WORKER LAMBDA               |
            |                          |   (async OCR + full pipeline) |
            |                          |                               |
            |                          |  S3 get -> Tesseract -> Textract|
            |                          |  -> parse -> store result (1h)  |
            |                          |  -> webhook -> delete S3 file   |
            |                          +------------------------------+
            |                                          |
            +------------------+-----------------------+
                               v
        +-----------------------------------------------+
        |   DynamoDB (state)         OpenAI gpt-4.1-mini   |
        |   api_keys - rate_limits   Amazon Textract (OCR)|
        |   jobs - batches                                |
        |   webhooks - audit_logs                         |
        +-----------------------------------------------+
```

---

## 4. Compute Model

The service runs as **two container-image Lambda functions** sharing one codebase and image:

| Function | Handler | Trigger | Responsibility |
|---|---|---|---|
| **API Lambda** | `app.handlers.lambda_handler.handler` (Mangum -> FastAPI) | Lambda **Function URL** (public HTTPS) | All synchronous request handling, auth, rate limiting, sync parsing, enqueuing async jobs onto SQS |
| **Worker Lambda** | `app.handlers.worker_lambda.handler` | SQS **event-source mapping** (drains the worker queue) | OCR-heavy parsing for scanned PDFs / images; reserved concurrency + the mapping's `maximum_concurrency` cap parallel OCR + AI calls |

**Why Lambda + SQS (not ECS/Kubernetes):**

- Scales to zero between requests - no idle cost for a single-tenant workload.
- SQS decouples the thin request path from the heavy batch path: the API answers in well under a
  second while the Worker scales elastically on queue depth.
- The queue's visibility timeout (above the ~130s orchestrator ceiling) stops a running job being
  redelivered; transient failures retry for free; queue depth is an alarm-able backpressure metric;
  and a **DLQ** turns a poison message into a visible event after `maxReceiveCount` (3) deliveries.
- Worker `reserved_concurrent_executions` and the event-source mapping's `maximum_concurrency`
  bound parallel OpenAI / Textract calls during a batch burst.

> **Local-dev fallback:** when no worker queue is configured (`use_queue_worker = false`), async
> work runs via FastAPI `BackgroundTasks` in-process, so the same code path works on a laptop or in
> Docker Compose with LocalStack.

---

## 5. Processing Pipeline

A single orchestrator (`app/services/pipeline.py`) runs every stage with per-step timeouts.

```text
 1. Classify          File type + strategy (PDF / DOCX / OCR), sync vs async
        |
 2. Extract           PyMuPDF (digital PDF) - python-docx (DOCX)
        |             Tesseract -> Textract fallback (scanned / image)
        |             - sync extractors run in an executor (non-blocking)
        v
 3. Clean             Unicode-safe: strips control chars, preserves
        |             international names (é, ñ, Arabic, CJK), fixes ligatures
        v
 4. Rule anchors      Regex: email, phone, LinkedIn / GitHub / portfolio URLs
        |             - passed to AI as ground truth (anti-hallucination)
        v
 5. Section detect    Header-based segmentation -> cuts AI token usage
        |
        v
 6. AI parse          OpenAI gpt-4.1-mini structured output - temperature 0
        |             schema-guaranteed JSON - 1 automatic retry
        v
 7. Validate          Pydantic v2 - type coercion + schema enforcement
        |
 8. Normalize         Skills, degrees, dates, healthcare specialties
        |             (e.g. "Sr Dev" -> "Senior Developer", "MSc" -> "Master of Science")
        v
 8b.Specialty match   Per-role specialties -> catalog id + confidence (tiered:
        |             name / full-name / keyword, then a batched AI shortlist pick;
        |             unmatched kept with id=null for review)
        v
 9. Confidence score  Per-field 0.0-1.0 scores for human-review triage
```

**Per-step timeouts:** extraction 60 s - OCR 180 s - AI parse 120 s. A timeout raises a typed
domain error rather than hanging the invocation.

---

## 6. Extraction Strategy

| Document type | Method | Rationale |
|---|---|---|
| Digital PDF | PyMuPDF | Fast, layout-aware, handles multi-column |
| DOCX | python-docx | Native text + table extraction |
| Scanned PDF / image | Tesseract -> **Textract fallback** | Local OCR is free; Textract is invoked only when Tesseract confidence is low |

This tiering keeps the common case free/cheap and reserves paid OCR for documents that genuinely
need it.

---

## 7. AI Parsing Strategy

A **hybrid rule-based + AI** approach:

- **Deterministic (regex):** contact details - email, phone, social/portfolio URLs.
- **AI (gpt-4.1-mini structured outputs):** semantic content - experience, education, skills, roles,
  date associations, projects, certifications.

| Technique | Purpose |
|---|---|
| Structured outputs / schema enforcement | Guaranteed valid JSON, no post-hoc repair |
| Anchor injection | Contact facts come from regex, not the model - eliminates hallucination |
| Section-scoped prompting | Lower token count, better locality, lower cost |
| `temperature = 0` | Deterministic, reproducible parses |
| Single automatic retry | Recovers transient failures without user impact |
| Confidence scoring | Surfaces uncertain fields for human review |

---

## 8. Data Layer (DynamoDB)

No relational database, no Redis. All state is in six on-demand (`PAY_PER_REQUEST`) DynamoDB tables;
TTL handles cleanup automatically.

| Table | Key | Purpose | Retention |
|---|---|---|---|
| `resume-parser-api-keys` | `key_hash` | API-key auth (SHA-256 hashes only) + per-key limits | Permanent |
| `resume-parser-rate-limits` | `window_key` | Sliding-window request counters | TTL (auto-expire) |
| `resume-parser-jobs` | `job_id` | Async job status + result | **TTL 1 hour** |
| `resume-parser-batches` | `batch_id` | Batch-upload tracking | TTL |
| `resume-parser-webhooks` | `company_id` + `webhook_id` | Webhook registrations + HMAC secrets | Permanent |
| `resume-parser-audit-logs` | `job_id` + `timestamp` | Content-free processing metadata | **TTL 90 days** |

Point-in-time recovery is enabled on the durable tables (`api_keys`, `jobs`, `webhooks`, `audit_logs`).

---

## 9. Security Architecture

```text
+-----------------------------------------------------------+
|                        SECURITY LAYERS                      |
+-----------------------------------------------------------+
| Transport   | HTTPS only (Function URL TLS)                 |
| AuthN       | API key  rp_live_*  - SHA-256 hashed at rest  |
| AuthZ       | Key scoped to company_id; revocable status    |
| Abuse       | Per-key sliding-window rate limits (min + day)|
| Storage     | S3 SSE-AES256; files deleted post-processing  |
| IAM         | Least-privilege Lambda execution role         |
| Webhooks    | HMAC-SHA256 signatures + timestamp anti-replay|
| Audit       | Content-free audit trail in DynamoDB          |
| PII         | No resume content persisted, anywhere         |
+-----------------------------------------------------------+
```

- **API keys** follow the format `rp_live_{random}`; only the SHA-256 hash is stored. A revoked key
  returns `403`; an unknown/missing key returns `401`.
- **Rate limiting** uses two independent DynamoDB windows (per-minute and per-day), configurable per
  key. Counters are atomic and correct across concurrent Lambda instances.
- **Webhooks** are signed with `X-Signature: sha256=...` over `{timestamp}.{body}`; consumers reject
  deliveries older than 5 minutes. In production, only HTTPS webhook URLs are accepted.

---

## 10. Privacy & Compliance

### Data retention

| Data | Where | Lifetime |
|---|---|---|
| Raw resume file | S3 `temp/{job_id}/{filename}`, SSE-AES256 | Deleted in `finally` block immediately after processing |
| Parsed result (async only) | DynamoDB `jobs` | 1 hour (TTL), then auto-deleted |
| Parsed result (sync) | Returned in response only | Not retained |
| Audit log | DynamoDB `audit_logs` | 90 days - **metadata only** (`job_id`, `company_id`, `file_type`, `file_size_bytes`, `status`, `duration_ms`, `ocr_used`, `ai_tokens_used`, `error_code`). **No content, no PII.** |
| **Feedback (original + corrected parse)** | DynamoDB `feedback` | **90 days (`feedback_retention_days`) - CONTAINS CANDIDATE PII.** Written only when a caller submits corrections to `POST /resume/{job_id}/feedback`. This is the only store of parsed resume content in the system. |

> **Third-party note:** resume text is transmitted to the OpenAI API for parsing. The OpenAI data
> processing terms govern that transit; no copy is retained by this service.

---

## 11. Scalability & Reliability

- **Horizontal by default.** Stateless Lambdas scale per-request; DynamoDB and S3 absorb concurrency.
- **Backpressure built-in.** Worker `reserved_concurrent_executions` caps simultaneous OCR + AI
  calls, protecting downstream rate limits without a broker.
- **Graceful degradation.** Per-step timeouts, a single AI retry, and Tesseract->Textract fallback
  keep partial failures contained.
- **Self-healing deploy.** The deployment pipeline ensures the Function URL and its public-invoke
  permission exist before traffic is admitted, and the smoke test retries to absorb propagation lag.

**Error-recovery flow**

```text
Parse failure
     |
     v  typed domain error (Extraction / AIParsing / ...)
Automatic retry (AI: 1x) - OCR fallback (Tesseract -> Textract)
     |
     v
Job marked  failed  ->  parse.failed webhook  +  status via poll endpoint
     |
     v
Client retry endpoint  (POST /resume/{job_id}/retry, up to MAX_RETRY_COUNT)
```

---

## 12. API Surface

Base path: `/api/v1` - all endpoints require `X-API-Key` (except health).

| Method | Path | Description |
|---|---|---|
| `POST` | `/resume/parse` | Parse one resume. Digital -> sync result; scanned/image -> async `job_id` |
| `GET` | `/resume/job/{job_id}` | Poll async job status / result |
| `POST` | `/resume/{job_id}/retry` | Re-parse a resume (new linked job, retry-limited) |
| `POST` | `/batch` | Submit a batch of resumes |
| `POST` | `/webhooks` | Register a webhook (returns one-time HMAC secret) |
| `GET` | `/webhooks` | List webhooks |
| `DELETE` | `/webhooks/{webhook_id}` | Remove a webhook |
| `GET` | `/health` | Liveness check (no auth) |

**Parse - synchronous response**

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": { "personal_info": {}, "experience": [], "education": [],
            "skills": [], "certifications": [], "projects": [], "languages": [] },
  "confidence": { "overall": 0.87, "personal_info": 0.92, "experience": 0.85,
                  "education": 0.90, "skills": 1.0 },
  "poll_url": null
}
```

**Parse - asynchronous response**

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "processing",
  "data": null,
  "confidence": null,
  "poll_url": "/api/v1/resume/job/01J3K5M2N4P6Q8R0S2T4U6V8W0"
}
```

---

## 13. Confidence Scoring

Each field is scored `0.0-1.0` so clients can route low-confidence records to human review.

| Score | Meaning |
|---|---|
| 0.90 - 1.00 | High - all expected fields present and valid |
| 0.70 - 0.89 | Good - minor gaps |
| 0.50 - 0.69 | Partial - key fields missing or unverifiable |
| < 0.50 | Low - recommend human review |

**Overall weights:** Personal info 35% - Experience 35% - Education 20% - Skills 10%.

---

## 14. Deployment & CI/CD

```text
+--------------------------------------------------------------+
|                          AWS (us-east-2)                       |
+--------------------------------------------------------------+
|  ECR  (container image: Dockerfile.lambda)                     |
|    |                                                           |
|    +--►  API Lambda      -- Function URL (HTTPS, AuthType NONE)|
|    |         |                + public-invoke permission        |
|    |         +- async invoke -►  Worker Lambda                  |
|    |                                                           |
|    +--►  DynamoDB  (6 tables, on-demand, TTL)                  |
|    +--►  S3        (temp bucket, SSE-AES256, auto-delete)      |
|    +--►  Amazon Textract  (OCR fallback)                      |
|    +--►  SSM Parameter Store  (OpenAI key at runtime)         |
+--------------------------------------------------------------+
```

- **Infrastructure as Code:** Terraform (`infrastructure/terraform/`) defines Lambda, Function URL +
  invoke permission, DynamoDB, S3, IAM, ECR, and SSM.
- **CI/CD:** GitHub Actions (`.github/workflows/deploy.yml`) - lint + type-check + unit/integration
  tests -> build & push image to ECR -> update both Lambdas -> ensure Function URL + permission ->
  retrying health-check smoke test.
- **Secrets:** the OpenAI API key is loaded from SSM at runtime, never baked into the image or set as
  a plaintext env var.

---

## 15. Technology Stack

| Layer | Technology |
|---|---|
| API framework | FastAPI + Python 3.12 (Mangum adapter) |
| Compute | AWS Lambda (container image) - API + Worker |
| Public ingress | Lambda Function URL (HTTPS) |
| AI parsing | OpenAI gpt-4.1-mini (structured outputs), via the shared resilient executor |
| PDF extraction | PyMuPDF |
| DOCX extraction | python-docx |
| OCR | Tesseract -> Amazon Textract (fallback) |
| Validation | Pydantic v2 |
| State / auth / rate limiting | DynamoDB (on-demand, TTL) |
| Temp file storage | Amazon S3 (SSE-AES256, auto-deleted) |
| Webhooks | HTTPX async + HMAC-SHA256 |
| Secrets | AWS SSM Parameter Store |
| Packaging | Docker |
| IaC / CI-CD | Terraform + GitHub Actions |
| Local dev | Docker Compose + LocalStack |
| Logging | Structured logs (structlog) -> CloudWatch |

---

## 16. Business Value

- **Faster hiring workflow** - eliminates manual candidate data entry; structured JSON auto-fills
  forms and databases.
- **High accuracy** - hybrid rule + AI parsing with confidence scores that flag what needs review.
- **Privacy by design** - zero resume-data retention is a strong differentiator for regulated
  industries (e.g. healthcare staffing, which this service's specialty-normalization targets).
- **Low total cost of ownership** - serverless scales to zero; OCR/AI invoked only when necessary.
- **Extensible foundation** - the structured output is a clean base for future resume scoring,
  candidate-job matching, ATS integrations, and skill intelligence.

---

## 17. Future Extensions

- Candidate ↔ job-description matching and ranking.
- Resume quality / completeness scoring.
- Self-service API-key provisioning and a usage dashboard.
- Native ATS connectors (Greenhouse, Lever, Workday).
- Expanded domain taxonomies beyond healthcare specialties.
