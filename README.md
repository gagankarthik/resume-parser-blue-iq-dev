# Resume Parser API

**Enterprise resume parsing for healthcare staffing.** A privacy-first HTTP API that converts
nursing and allied-health résumés — PDF, DOCX, or scanned images — into structured, validated,
taxonomy-normalized JSON that maps directly onto candidate profile and Work History forms.

Built for a single enterprise client (BlueIQ) and delivered as a managed API: upload a résumé,
receive a complete candidate record with per-field confidence scores, credential/licence capture,
and 350+ clinical specialties resolved to canonical names.

---

## Why it exists

Healthcare staffing teams re-key candidate data from résumés into placement systems by hand —
slow, error-prone, and inconsistent. This service automates that step with healthcare-aware
extraction: it understands the difference between a certification and a state licence, preserves
credential post-nominals, keeps travel-assignment history intact, and normalizes free-text
specialties to a controlled vocabulary the placement platform can match on.

---

## Capabilities at a glance

| Area | What you get |
|---|---|
| **Formats** | Digital PDF, DOCX, and scanned PDF / PNG / JPG / TIFF / WEBP (OCR) |
| **Accuracy** | Multi-agent extraction with per-role work history and bullet-count verification |
| **Healthcare domain** | 350+ specialty taxonomy, credential & state-licence capture, Work History field mapping |
| **Resilience** | Three-tier graceful degradation — never returns "nothing" on a hard résumé |
| **Confidence** | Per-section 0–1 confidence scores to route low-quality records to human review |
| **Integration** | Synchronous parse, async (OCR) jobs, batch, webhooks, and a correction-feedback loop |
| **Security** | API-key + session auth, magic-byte validation, SSRF-guarded webhooks, zero content storage |
| **Observability** | Structured request logs, content-free audit trail, and per-company token/usage accounting |

---

## System architecture

A single AWS Lambda serves the HTTP API and, via asynchronous self-invocation, also runs the
OCR worker. State lives in DynamoDB; résumé bytes touch S3 only transiently and are deleted
immediately after processing.

```
                              ┌─────────────────────────────────────────────┐
                              │                 Clients                       │
                              │  Staffing platform · UAT tool · Batch jobs    │
                              └───────────────┬──────────────────────────────┘
                                              │  HTTPS · X-API-Key / Bearer
                                              ▼
              ┌──────────────────────────────────────────────────────────────┐
              │                    Resume Parser API (AWS Lambda)             │
              │                                                                │
              │   Auth ─ Validation ─ Pipeline ─ Normalization ─ Scoring      │
              │     │         │           │            │            │          │
              └─────┼─────────┼───────────┼────────────┼────────────┼─────────┘
                    │         │           │            │            │
        ┌───────────┘    ┌────┘      ┌────┴─────┐  ┌───┘       ┌────┘
        ▼                ▼           ▼          ▼   ▼           ▼
   ┌─────────┐    ┌────────────┐ ┌──────┐ ┌─────────┐  ┌──────────────┐
   │DynamoDB │    │  S3 (temp) │ │OpenAI│ │ Textract │  │  Webhooks    │
   │ 7 tables│    │ auto-purge │ │GPT-4o│ │  (OCR)   │  │ (HMAC-signed)│
   └─────────┘    └────────────┘ └──────┘ └─────────┘  └──────────────┘
```

**DynamoDB tables:** API keys · jobs · batches · webhooks · audit logs · companies · feedback.
No résumé content is ever persisted to any of them.

---

## Request lifecycle

The API picks a processing mode from the file type. Text-bearing documents return results in the
same HTTP response; scanned documents that need OCR are processed asynchronously and delivered by
webhook and polling.

```
 Upload ─► Authenticate ─► Size + magic-byte validation ─► Classify file
                                                               │
                          ┌────────────────────────────────────┴───────────────────────┐
                          ▼                                                              ▼
                Digital PDF / DOCX                                          Scanned PDF / image
                  (synchronous)                                                (asynchronous)
                          │                                                              │
                          │                                              store in S3 ─► return job_id
                          │                                                              │
                          ▼                                                              ▼
                 ┌──────────────────────────── Parsing pipeline ───────────────────────────┐
                 │  Extract text ─► Clean ─► Detect sections ─► Parse ─► Normalize ─► Score   │
                 └──────────────────────────────────────────────────────────────────────────┘
                          │                                                              │
                          ▼                                                              ▼
                 Structured JSON in response                       Webhook  +  GET /resume/job/{id}
```

### Tiered text extraction

The classifier inspects each file and routes it to the cheapest extractor that will produce
clean text, falling back automatically when quality is poor:

```
 PDF ─► digital text layer?  ── yes ─► layout-aware extract (multi-column reading order)
   │                            │
   │                            └──► low-quality / garbled?  ── yes ─► OCR fallback
   │
 Image / scanned PDF ─────────────────────────────────────────► OCR
                                                                  │
                            preprocess (deskew · denoise · contrast · binarise)
                                                                  │
                                  Tesseract  ── confident? ── no ─► AWS Textract
                                       │                              │
                                       └──────────── text ───────────┘
```

Callers can force Textract per request (`force_textract`) for maximum accuracy on hard scans.

### Three-tier accuracy & graceful degradation

A résumé that defeats one stage falls through to the next, so the API **never returns an empty
or error-only response** when any usable data can be recovered. Records that degraded are flagged
`partial: true` with human-readable `warnings`.

```
 Tier 1  Multi-agent orchestrator        — highest accuracy (long / complex résumés)
            │  on failure ▼
 Tier 2  Single-shot structured parse     — fast, robust default
            │  on failure ▼
 Tier 3  Anchor-only partial record       — contact details recovered, flagged for review
```

---

## Multi-agent extraction

For long and complex résumés (e.g. multi-employer travel-nurse CVs), the parser runs a
coordinated multi-agent pipeline that extracts each role independently and cross-checks the
result, rather than asking one model call to do everything at once. Short résumés take the fast
single-shot path; the orchestrator is reserved for documents that benefit from it.

```
 Stage 1   Structure Agent      Map every role + exact responsibility-bullet counts;
   (sequential)                 decompose travel/agency assignments into per-facility roles

 Stage 2   Parallel section agents
   (concurrent)     ┌─ Personal      name · post-nominal credentials · contact · summary
                    ├─ Work          one focused extraction per mapped role
                    ├─ Education     degrees · institutions · in-progress study
                    ├─ Credentials   skills · certifications · STATE LICENSES (kept separate)
                    └─ Supplemental  references · awards · publications · languages

 Stage 3   Validator Agent      Reconcile extracted bullet counts against the structure map;
   (sequential)                 re-extract any role that lost detail
```

**Why this is more accurate**

- **No dropped employers.** A structure map pins the role count up front, so a 10-employer
  résumé yields 10 records — not a truncated subset.
- **Travel assignments stay intact.** Each facility under a travel/agency umbrella becomes its
  own entry that inherits the profession and agency — never a stray "Unknown" role.
- **Verifiable completeness.** Responsibility bullets are counted, extracted, and re-checked;
  mismatches are re-extracted and any residual gap is surfaced as a warning.
- **Per-section isolation.** One section failing degrades only that section — the rest of the
  record still returns.

Every model call uses **structured outputs** (schema-enforced JSON), so malformed responses can't
corrupt a record regardless of which path produced it.

---

## Healthcare normalization & mapping

After extraction, every record is normalized so downstream forms receive consistent, matchable
values:

- **Specialty taxonomy** — 350+ clinical specialties and abbreviations resolved to canonical
  names (e.g. *Med Surg / Tele*, *Intensive Care Unit*), punctuation- and synonym-tolerant.
- **Credential expansion** — role credentials expanded in titles (RN → Registered Nurse) while
  raw abbreviations are preserved where they belong.
- **Post-nominal capture** — credentials trailing a name (RN, BSN, MPH, CCRN) are lifted into a
  dedicated field instead of being discarded.
- **Licences vs certifications** — state licences (with number, state, and status) are captured
  separately from time-limited certifications.
- **Date fidelity** — dates are normalized to a single format while preserving the stated
  precision; a missing day or month is never invented.
- **Work History field mapping** — facility location, profession, specialties, shift, charting
  system, ratios, and facility flags map straight onto the platform's Work History form.

---

## Output record

A parsed record is a single JSON document. High-level shape:

```
personal_information   name · credentials[] · email · phone · full address · summary
work_experience[]      per-role: facility · title · dates · location · profession ·
                       specialties[] · shift · charting system · ratios · facility flags ·
                       responsibilities[] · achievements[] · agency
education[]            institution · degree · field · years
skills[]               clinical specialties & competencies (taxonomy-normalized)
certifications[]       BLS · ACLS · CCRN … (issuer + dates)
licenses[]             state licences with number, state, status, compact flag
references[] · awards[] · publications[] · projects[] · languages[]
─────────────────────────────────────────────────────────────────────────────
confidence             per-section + overall 0–1 scores
skills_validation      taxonomy match ratio + recognized / unrecognized split
partial · warnings     degradation flag + reviewer notes
```

---

## Security & compliance

| Control | Implementation |
|---|---|
| **Privacy by design** | Résumé content is never stored. S3 holds bytes only transiently and deletes them in a guaranteed cleanup step after processing. |
| **Authentication** | Per-company API keys (`X-API-Key`, SHA-256 hashed at rest), self-serve session tokens (Bearer), and a separate admin token for management endpoints. |
| **File validation** | Magic-byte signature checks reject type-spoofed or corrupted uploads before any processing; size caps enforced on every entry path. |
| **SSRF protection** | Webhook URLs are validated against private/loopback/metadata ranges at registration **and** re-checked at delivery to defeat DNS rebinding. |
| **Transport & headers** | HTTPS-only in production, HSTS, `X-Content-Type-Options`, `X-Frame-Options`, strict referrer policy. CORS denies cross-origin by default unless explicitly allow-listed. |
| **Tenant isolation** | Every job, webhook, and feedback record is scoped to a `company_id`; cross-tenant access is rejected. |
| **Encryption** | Server-side encryption on S3; encryption at rest on DynamoDB. |
| **Surface reduction** | Interactive API docs are disabled in production. |

---

## Observability

### Structured request logging
Every request emits a structured log line with method, path, status, duration, and a
correlatable `X-Request-ID` (returned on every response) — **never any résumé content**.

### Content-free audit trail
Each parse writes an audit record to DynamoDB capturing the operational facts needed for billing,
debugging, and capacity planning — and nothing sensitive:

```
job_id · company_id · file_type · file_size · status · duration_ms ·
ocr_used · ai_tokens_used · error_code · timestamp
```

### Token & usage accounting
LLM token consumption is metered across **every** model call — including each agent in the
multi-agent path — aggregated per job, and recorded on the audit log. Per-company usage and token
totals are queryable through the usage endpoints, giving a precise, tenant-level cost view.

### Correction feedback loop
After a reviewer edits a parsed record, the original and corrected JSON can be submitted back to
the API. Feedback is stored per company with a computed field-level diff and a retention TTL,
forming a labeled dataset for measuring and improving accuracy over time.

---

## API surface

All endpoints are under `/api/v1`. Parsing endpoints authenticate with `X-API-Key`.

### Parsing
| Method & path | Purpose |
|---|---|
| `POST /resume/parse` | Parse one résumé. Digital files return JSON synchronously; scans return a `job_id`. |
| `POST /resume/upload-url` | Get a presigned S3 URL for large files (bypasses the request-size cap). |
| `POST /resume/parse-uploaded` | Parse a file previously uploaded via the presigned URL. |
| `GET /resume/job/{job_id}` | Poll an asynchronous (OCR) job for status and results. |
| `POST /resume/{job_id}/retry` | Re-run the full pipeline on a résumé whose result was unsatisfactory. |
| `POST /resume/{job_id}/feedback` | Submit original + corrected JSON after human review. |

### Batch
| Method & path | Purpose |
|---|---|
| `POST /resume/batch` | Submit many résumés in one request; results via webhook + polling. |
| `GET /resume/batch/{batch_id}` | Aggregate batch progress (completed / failed / processing). |

### Webhooks
| Method & path | Purpose |
|---|---|
| `POST /webhooks` | Register an HMAC-signed webhook for `parse.completed` / `parse.failed` / `batch.completed`. |
| `GET /webhooks` | List registered webhooks. |
| `DELETE /webhooks/{webhook_id}` | Remove a webhook. |

### Accounts & administration
| Method & path | Auth | Purpose |
|---|---|---|
| `POST /signup` · `POST /login` · `GET /me` | — / Bearer | Self-serve account creation and session. |
| `GET/POST /keys` · `POST /keys/{hash}/revoke` · `GET /usage` | Bearer | Self-serve key management and usage stats. |
| `POST/GET /companies` · `/companies/{id}/keys` · `/companies/{id}/usage` · `/companies/{id}/webhooks` | Admin token | Company onboarding, key issuance, usage, and webhook management for the product platform. |

### Service
| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness + dependency probe (`ok` / `degraded`). |

---

## File format support

| Format | Path | Notes |
|---|---|---|
| PDF (digital) | Synchronous | Layout-aware, multi-column reading order |
| DOCX | Synchronous | Paragraphs + table cells in document order |
| PDF (scanned) | Asynchronous | Tiered OCR with automatic Textract escalation |
| PNG · JPG · TIFF · WEBP | Asynchronous | Image preprocessing + OCR; multi-page TIFF supported |

> Magic-byte validation runs on every upload regardless of extension.

---

## Asynchronous delivery

For OCR jobs and batches, results are delivered two ways so integrators can choose push or pull:

- **Webhooks** — HMAC-SHA256 signed `POST` to your endpoint on `parse.completed`, `parse.failed`,
  or `batch.completed`, with automatic retries.
- **Polling** — `GET /resume/job/{job_id}` until `completed` or `failed`.

```
 Async parse ─► job: pending ─► processing ─► completed ──► webhook  ─►  your server
                                          └─► failed    ──► (also retrievable via polling)
```

---

## Configuration

Key settings (full list in `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `OPENAI_MODEL` | `gpt-4o` | Extraction model |
| `USE_MULTI_AGENT` | `true` | Enable the high-accuracy multi-agent path |
| `MULTI_AGENT_MIN_CHARS` | `3500` | Length gate — shorter résumés use the fast single-shot path |
| `MULTI_AGENT_MAX_CONCURRENCY` | `4` | Cap on concurrent in-flight model calls |
| `FORCE_TEXTRACT` | `false` | Skip Tesseract; always use Textract for OCR |
| `MAX_FILE_SIZE_MB` | `10` | Upload size limit |
| `JOB_RESULT_TTL_SECONDS` | `3600` | How long async results are retained |
| `FEEDBACK_RETENTION_DAYS` | `90` | Feedback record retention |
| `CORS_ALLOWED_ORIGINS` | _(empty)_ | Allow-listed browser origins (prod denies by default) |

---

## Deployment

- **Compute** — a single AWS Lambda serves HTTP and, via async self-invocation, the OCR worker.
- **Infrastructure as code** — Terraform provisions Lambda, IAM, DynamoDB, S3, and the function URL.
- **CI/CD** — GitHub Actions lints, type-checks, runs the test suite, and deploys on merge.
- **Storage** — DynamoDB (operational state) and S3 (transient résumé bytes, auto-purged).

---

## Quality & testing

The codebase is covered by an automated suite spanning schema validation, healthcare
normalization, taxonomy mapping, confidence scoring, the multi-agent orchestrator, graceful
degradation, and API integration — run on every change alongside static type-checking and linting.

---

## Limits

| Limit | Value |
|---|---|
| Max file size (direct) | ~6 MB request cap → use presigned upload for larger |
| Max file size (presigned) | 10 MB |
| Max batch size | 200 files |
| Async result retention | 1 hour |
| Rate limiting | Not enforced at the API layer; control via deployment concurrency |
