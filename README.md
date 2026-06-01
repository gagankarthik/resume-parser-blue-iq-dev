# Resume Parser API

Enterprise-grade resume parsing service that converts PDF, DOCX, and image resumes into structured JSON. Built for a single-tenant client API with zero resume data retention.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [API Reference](#api-reference)
- [Authentication](#authentication)
- [Rate Limiting](#rate-limiting)
- [Webhooks](#webhooks)
- [Processing Pipeline](#processing-pipeline)
- [Data Privacy](#data-privacy)
- [Local Development](#local-development)
- [Environment Variables](#environment-variables)
- [Deployment](#deployment)
- [Running Tests](#running-tests)
- [DynamoDB Tables](#dynamodb-tables)
- [Output Schema](#output-schema)
- [Confidence Scores](#confidence-scores)
- [Error Handling](#error-handling)

---

## Overview

The Resume Parser API accepts resume files via HTTP, extracts and semantically parses the content using a hybrid rule-based + AI pipeline, and returns clean structured JSON ready to auto-fill candidate forms or populate a database.

**Key design decisions:**

- **Zero data retention** — resume content is never stored. Raw files are deleted from S3 immediately after processing (in a `finally` block, even on failure). Only audit logs (metadata, no content) are kept.
- **No Redis / no Celery** — FastAPI BackgroundTasks handles async processing. DynamoDB handles rate limiting and job tracking.
- **Single tenant** — built for one company. Auth is API key scoped to `company_id`.
- **Sync/async split** — digital PDFs and DOCX files are processed synchronously and return results immediately. Scanned PDFs and images run asynchronously (OCR is slow) and deliver results via webhook + polling.

---

## Architecture

```
Client
  │
  ▼
POST /api/v1/resume/parse
  │
  ├── API Key auth      (DynamoDB lookup)
  ├── Rate limit check  (DynamoDB sliding window)
  ├── File validation   (type, size)
  │
  ├── [Digital PDF / DOCX] ────────────────────────────────────────────┐
  │     Synchronous:                                                    │
  │     Extract → Clean → Detect Sections → Rule Parse → AI Parse      │
  │     → Validate → Normalize → Score → Return JSON                   │
  │     → Write audit log → (no file stored)                           │
  │                                                                     │
  └── [Scanned PDF / Image] ───────────────────────────────────────────┤
        Returns job_id immediately                                      │
        BackgroundTask:                                                  │
        Upload to S3 → Tesseract → Textract → Parse → Score            │
        → Store result in DynamoDB (TTL 1h) → Fire webhook             │
        → Write audit log → Delete S3 file                             │
                                                                        ▼
                                                              Structured JSON
```

### Scalability

The app is stateless — S3 and DynamoDB handle all shared state. Scale horizontally by running multiple FastAPI containers behind a load balancer. DynamoDB rate limiting counters are atomic and work correctly across multiple instances.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Python 3.12 |
| AI Parsing | OpenAI GPT-4o (structured outputs) |
| PDF Extraction | PyMuPDF |
| DOCX Extraction | python-docx |
| OCR | Tesseract (primary) → Amazon Textract (fallback) |
| Validation | Pydantic v2 |
| Auth & Rate Limiting | API Keys + DynamoDB |
| Job Tracking | DynamoDB (TTL 1 hour) |
| File Storage | Amazon S3 (temp, auto-deleted) |
| Webhook Delivery | HTTPX async + HMAC-SHA256 |
| Audit Logs | DynamoDB (no resume content) |
| Containerization | Docker + Docker Compose |
| Local Dev | LocalStack (S3 + DynamoDB) |

---

## Project Structure

```
resume-parser/
├── app/
│   ├── main.py                          # FastAPI app factory + lifespan
│   ├── api/
│   │   ├── dependencies.py              # Auth + rate limit DI
│   │   └── v1/
│   │       ├── router.py
│   │       └── endpoints/
│   │           ├── resume.py            # POST /parse, GET /job/{id}
│   │           ├── webhooks.py          # Webhook CRUD
│   │           └── health.py            # GET /health
│   ├── core/
│   │   ├── config.py                    # Pydantic settings (env-based)
│   │   ├── security.py                  # API key hashing, HMAC signing
│   │   ├── logging.py                   # Structured logging (structlog)
│   │   ├── rate_limiter.py              # DynamoDB sliding window
│   │   └── exceptions.py               # Domain exceptions + HTTP helpers
│   ├── db/
│   │   └── dynamodb.py                  # All DynamoDB operations
│   ├── storage/
│   │   └── s3_client.py                 # Temp upload + guaranteed delete
│   ├── services/
│   │   ├── pipeline.py                  # Full pipeline orchestrator
│   │   ├── extraction/
│   │   │   ├── classifier.py            # File type + strategy detection
│   │   │   ├── pdf_extractor.py         # PyMuPDF (digital PDFs)
│   │   │   ├── docx_extractor.py        # python-docx
│   │   │   └── ocr_extractor.py         # Tesseract → Textract
│   │   ├── parsing/
│   │   │   ├── rule_parser.py           # Regex: email, phone, URLs
│   │   │   ├── section_detector.py      # Header-based section segmentation
│   │   │   └── ai_parser.py             # GPT-4o structured output
│   │   ├── normalization/
│   │   │   └── normalizer.py            # Skills, dates, degrees
│   │   └── scoring/
│   │       └── confidence_scorer.py     # Per-field 0.0–1.0 confidence
│   ├── workers/
│   │   ├── background.py                # FastAPI BackgroundTasks handler
│   │   └── webhook_sender.py            # HMAC-signed delivery + retry
│   └── models/
│       └── schemas.py                   # All Pydantic models
├── tests/
│   ├── unit/                            # rule_parser, normalizer, classifier, scorer
│   └── integration/                     # health, auth rejection tests
├── scripts/
│   └── localstack_init.sh               # Creates tables + S3 bucket in LocalStack
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## API Reference

Base URL: `https://your-domain.com/api/v1`

All endpoints require `X-API-Key` header.

---

### Parse a Resume

```http
POST /api/v1/resume/parse
```

**Headers**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | Your API key (`rp_live_...`) |
| `Content-Type` | Yes | `multipart/form-data` |

**Body**

| Field | Type | Description |
|---|---|---|
| `file` | File | Resume file (PDF, DOCX, PNG, JPG, TIFF) |

**Supported file types:** `.pdf`, `.docx`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.webp`

**Max file size:** 10 MB (configurable via `MAX_FILE_SIZE_MB`)

**Response — Synchronous (digital PDF / DOCX)**

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": {
    "personal_info": { ... },
    "experience": [ ... ],
    "education": [ ... ],
    "skills": [ ... ],
    "certifications": [ ... ],
    "projects": [ ... ],
    "languages": [ ... ]
  },
  "confidence": {
    "overall": 0.87,
    "personal_info": 0.92,
    "experience": 0.85,
    "education": 0.90,
    "skills": 1.0
  },
  "poll_url": null
}
```

**Response — Asynchronous (scanned PDF / image)**

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

### Poll Async Job Status

```http
GET /api/v1/resume/job/{job_id}
```

**Path Parameters**

| Parameter | Description |
|---|---|
| `job_id` | Job ID returned from `/parse` |

**Response**

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": { ... },
  "confidence": { ... },
  "error": null
}
```

**Job statuses:** `pending` → `processing` → `completed` | `failed`

Job results are retained in DynamoDB for **1 hour** then auto-deleted.

---

### Register a Webhook

```http
POST /api/v1/webhooks
```

**Body**

```json
{
  "url": "https://your-server.com/hooks/resume",
  "events": ["parse.completed", "parse.failed"]
}
```

**Available events:** `parse.completed`, `parse.failed`

**Response** (`201 Created`)

```json
{
  "webhook_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "url": "https://your-server.com/hooks/resume",
  "events": ["parse.completed", "parse.failed"],
  "hmac_secret": "a3f8d2e1c9b7...",
  "status": "active",
  "created_at": "2026-06-01T10:00:00"
}
```

> **Important:** The `hmac_secret` is only returned on creation. Store it securely — it cannot be retrieved again.

---

### List Webhooks

```http
GET /api/v1/webhooks
```

**Response**

```json
[
  {
    "webhook_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
    "url": "https://your-server.com/hooks/resume",
    "events": ["parse.completed"],
    "status": "active",
    "created_at": "2026-06-01T10:00:00"
  }
]
```

---

### Delete a Webhook

```http
DELETE /api/v1/webhooks/{webhook_id}
```

**Response:** `204 No Content`

---

### Health Check

```http
GET /api/v1/health
```

No authentication required.

**Response**

```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "production"
}
```

---

## Authentication

API keys follow the format `rp_live_{random_44_chars}`.

Include the key in every request via the `X-API-Key` header:

```
X-API-Key: rp_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd
```

**How it works:**
1. The raw key is never stored — only a SHA-256 hash is kept in DynamoDB.
2. On each request, the incoming key is hashed and looked up in DynamoDB.
3. If the key is not found or has status `revoked`, a `401` or `403` is returned.

**Provisioning keys** (currently manual — add to DynamoDB `api_keys` table):

```bash
# Generate a key hash
echo -n "rp_live_your_key_here" | sha256sum

# Insert via AWS CLI
aws dynamodb put-item \
  --table-name resume-parser-api-keys \
  --item '{
    "key_hash": {"S": "<hash>"},
    "key_prefix": {"S": "rp_live_abc…"},
    "company_id": {"S": "your-company"},
    "status": {"S": "active"},
    "rate_limit_per_minute": {"N": "30"},
    "rate_limit_per_day": {"N": "1000"},
    "created_at": {"S": "2026-06-01T00:00:00Z"}
  }'
```

---

## Rate Limiting

Rate limiting uses a **DynamoDB sliding window** with two independent windows:

| Window | Default | Header on exceed |
|---|---|---|
| Per minute | 30 requests | `429 Too Many Requests` |
| Per day | 1000 requests | `429 Too Many Requests` |

Limits are **per API key** and configurable individually in the `api_keys` table via `rate_limit_per_minute` and `rate_limit_per_day` fields.

DynamoDB TTL auto-expires the counter records — no cleanup needed.

**Response on rate limit exceeded:**

```json
{
  "detail": "Rate limit: 30 requests/minute exceeded"
}
```

---

## Webhooks

### Delivery

When a job completes or fails, the API fires a POST request to all registered webhook URLs subscribed to that event.

**Payload — `parse.completed`**

```json
{
  "event": "parse.completed",
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "data": {
    "personal_info": { ... },
    "experience": [ ... ],
    "education": [ ... ],
    "skills": [ ... ]
  }
}
```

**Payload — `parse.failed`**

```json
{
  "event": "parse.failed",
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "error": "Textract failed: ..."
}
```

### Signature Verification

Every webhook delivery includes two headers:

```
X-Signature: sha256=<hex_digest>
X-Timestamp:  <unix_timestamp>
```

Verify on your server:

```python
import hmac, hashlib, time

def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    # Reject requests older than 5 minutes
    if abs(time.time() - int(timestamp)) > 300:
        return False
    message = f"{timestamp}.".encode() + body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Retry Policy

Failed deliveries (5xx response or connection error) are retried **3 times** with delays of 2s, 5s, and 10s. 4xx responses are not retried.

---

## Processing Pipeline

```
File Upload
    │
    ▼
Classifier
    Detects: PDF (digital) / PDF (scanned) / DOCX / Image
    Decides: sync or async path
    │
    ▼
Extractor
    PDF (digital)  → PyMuPDF       (preserves layout, handles multi-column)
    DOCX           → python-docx   (paragraphs + tables)
    Scanned/Image  → Tesseract     (local OCR, free)
                   → Textract      (AWS, fallback when Tesseract confidence < 60%)
    │
    ▼
Text Cleaning
    Remove non-printable chars, normalize whitespace, collapse blank lines
    │
    ▼
Rule Parser (runs first, before AI)
    Extracts: email, phone, LinkedIn URL, GitHub URL, portfolio URL
    These are passed as anchors to the AI — avoids hallucination on contact info
    │
    ▼
Section Detector
    Identifies: summary, experience, education, skills, projects,
                certifications, achievements, languages
    Segments text into labeled sections → reduces AI token count
    │
    ▼
AI Parser (OpenAI GPT-4o)
    Structured output — guaranteed schema-valid JSON
    Temperature: 0 (deterministic)
    Retry: 1 automatic retry on failure
    │
    ▼
Pydantic Validation
    Schema enforcement, type coercion
    │
    ▼
Normalizer
    Skills:  nodejs → Node.js, JS → JavaScript, postgres → PostgreSQL
    Degrees: MSc → Master of Science, BTech → Bachelor of Technology
    Dates:   Jan 2023 / 01-2023 / 2023/01 → 2023-01 (ISO YYYY-MM)
    │
    ▼
Confidence Scorer
    Per-field scores (0.0–1.0) based on completeness and validity
    │
    ▼
Structured JSON Response
```

---

## Data Privacy

- **Raw files** — uploaded to S3 as `temp/{job_id}/{filename}` with server-side AES-256 encryption. Deleted immediately after processing in a `finally` block — deletion happens even if parsing fails.
- **Parsed content** — for async jobs only, the result is stored in DynamoDB with a **1-hour TTL**. After TTL, DynamoDB auto-deletes it. For sync jobs, result is returned directly and never stored.
- **Audit logs** — stored permanently in DynamoDB. Contain: `job_id`, `company_id`, `file_type`, `file_size_bytes`, `status`, `duration_ms`, `ocr_used`, `ai_tokens_used`, `error_code`. **No resume content, no PII.**
- **OpenAI** — resume text is sent to the OpenAI API for parsing. Review OpenAI's data processing agreement for your compliance requirements.

---

## Local Development

### Prerequisites

- Docker + Docker Compose
- (Optional) Python 3.12 + Poetry for running tests locally

### Start

```bash
# Clone and configure
cp .env.example .env
# Add your OPENAI_API_KEY to .env

# Start app + LocalStack
docker-compose up
```

The app starts at `http://localhost:8000`.  
LocalStack starts at `http://localhost:4566`.  
Swagger UI: `http://localhost:8000/docs`

On first startup, `scripts/localstack_init.sh` auto-creates all DynamoDB tables, the S3 bucket, and seeds a dev API key:

```
rp_live_devkey00000000000000000000000000000000
```

### Example Request

```bash
curl -X POST http://localhost:8000/api/v1/resume/parse \
  -H "X-API-Key: rp_live_devkey00000000000000000000000000000000" \
  -F "file=@/path/to/resume.pdf"
```

### Register a Webhook (local testing)

Use [webhook.site](https://webhook.site) to get a free test URL:

```bash
curl -X POST http://localhost:8000/api/v1/webhooks \
  -H "X-API-Key: rp_live_devkey00000000000000000000000000000000" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://webhook.site/your-uuid", "events": ["parse.completed", "parse.failed"]}'
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in values.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | No | `development` | `development` or `production` |
| `DEBUG` | No | `false` | Enable debug logging |
| `AWS_REGION` | Yes | `us-east-1` | AWS region |
| `AWS_ACCESS_KEY_ID` | Yes* | — | AWS credentials (*not needed with IAM roles) |
| `AWS_SECRET_ACCESS_KEY` | Yes* | — | AWS credentials |
| `DYNAMODB_ENDPOINT_URL` | No | — | Set to `http://localstack:4566` for local dev |
| `S3_ENDPOINT_URL` | No | — | Set to `http://localstack:4566` for local dev |
| `DYNAMODB_TABLE_API_KEYS` | No | `resume-parser-api-keys` | DynamoDB table name |
| `DYNAMODB_TABLE_RATE_LIMITS` | No | `resume-parser-rate-limits` | DynamoDB table name |
| `DYNAMODB_TABLE_JOBS` | No | `resume-parser-jobs` | DynamoDB table name |
| `DYNAMODB_TABLE_WEBHOOKS` | No | `resume-parser-webhooks` | DynamoDB table name |
| `DYNAMODB_TABLE_AUDIT_LOGS` | No | `resume-parser-audit-logs` | DynamoDB table name |
| `S3_BUCKET_NAME` | No | `resume-parser-temp` | S3 bucket for temp files |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o` | OpenAI model ID |
| `OPENAI_MAX_TOKENS` | No | `4096` | Max tokens for AI parsing |
| `MAX_FILE_SIZE_MB` | No | `10` | Maximum upload size |
| `JOB_RESULT_TTL_SECONDS` | No | `3600` | How long async results live in DynamoDB |
| `DEFAULT_RATE_LIMIT_PER_MINUTE` | No | `30` | Default per-key per-minute limit |
| `DEFAULT_RATE_LIMIT_PER_DAY` | No | `1000` | Default per-key per-day limit |
| `WEBHOOK_TIMEOUT_SECONDS` | No | `10` | Timeout per webhook delivery attempt |
| `WEBHOOK_MAX_RETRIES` | No | `3` | Max retries per webhook |

**Production notes:**
- Use IAM roles instead of `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
- `ENVIRONMENT=production` disables Swagger UI (`/docs`, `/redoc`) and enforces HTTPS on webhook URLs

---

## Deployment

### AWS (Recommended)

```
Route 53
    └── Load Balancer (ALB)
            └── ECS Fargate (FastAPI containers)
                    ├── DynamoDB (5 tables)
                    ├── S3 (resume-parser-temp bucket)
                    └── Amazon Textract (on-demand)
```

**ECS Task Role permissions needed:**

```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
    "dynamodb:DeleteItem", "dynamodb:Query",
    "s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket",
    "textract:DetectDocumentText"
  ],
  "Resource": "*"
}
```

### Docker (standalone)

```bash
docker build -t resume-parser .
docker run -p 8000:8000 --env-file .env resume-parser
```

---

## Running Tests

```bash
# Install dev dependencies
poetry install

# Unit tests only (no AWS needed)
pytest tests/unit/ -v

# Integration tests (no AWS needed — mocked)
pytest tests/integration/ -v

# All tests
pytest -v
```

**Unit test coverage:**

| Test file | What it tests |
|---|---|
| `test_rule_parser.py` | Email, phone, URL regex extraction |
| `test_section_detector.py` | Section header detection + fallback |
| `test_normalizer.py` | Date formats, skill aliases, deduplication |
| `test_classifier.py` | File type detection, unsupported file rejection |
| `test_confidence_scorer.py` | Score calculation for complete/empty resumes |
| `test_health.py` | `/health` endpoint |
| `test_auth.py` | 401 on missing key, 401 on invalid key |

---

## DynamoDB Tables

### `resume-parser-api-keys`

| Attribute | Type | Description |
|---|---|---|
| `key_hash` (PK) | String | SHA-256 of the raw API key |
| `key_prefix` | String | First 12 chars + `…` for display |
| `company_id` | String | Owning company |
| `status` | String | `active` or `revoked` |
| `rate_limit_per_minute` | Number | Per-minute request limit |
| `rate_limit_per_day` | Number | Per-day request limit |
| `created_at` | String | ISO timestamp |

### `resume-parser-rate-limits`

| Attribute | Type | Description |
|---|---|---|
| `window_key` (PK) | String | `{key_hash}#min#{YYYY-MM-DDTHH:MM}` or `#day#{YYYY-MM-DD}` |
| `count` | Number | Request count in this window |
| `ttl` | Number | Unix timestamp — DynamoDB auto-expires |

### `resume-parser-jobs`

| Attribute | Type | Description |
|---|---|---|
| `job_id` (PK) | String | ULID |
| `company_id` | String | Owning company |
| `status` | String | `pending` / `processing` / `completed` / `failed` |
| `result` | Map | Parsed data + confidence (only when completed) |
| `error` | String | Error message (only when failed) |
| `created_at` | String | ISO timestamp |
| `started_at` | String | ISO timestamp |
| `completed_at` | String | ISO timestamp |
| `ttl` | Number | Unix timestamp — auto-deleted after 1 hour |

### `resume-parser-webhooks`

| Attribute | Type | Description |
|---|---|---|
| `company_id` (PK) | String | Owning company |
| `webhook_id` (SK) | String | ULID |
| `url` | String | Delivery URL |
| `hmac_secret` | String | Signing secret (treat as sensitive) |
| `events` | List | Subscribed event names |
| `status` | String | `active` |
| `created_at` | String | ISO timestamp |

### `resume-parser-audit-logs`

| Attribute | Type | Description |
|---|---|---|
| `job_id` (PK) | String | ULID |
| `timestamp` (SK) | String | ISO timestamp |
| `company_id` | String | Owning company |
| `file_type` | String | `pdf` / `docx` / `ocr` |
| `file_size_bytes` | Number | — |
| `status` | String | `completed` / `failed` |
| `duration_ms` | Number | End-to-end processing time |
| `ocr_used` | Boolean | Whether Textract was invoked |
| `ai_tokens_used` | Number | OpenAI tokens consumed |
| `error_code` | String | Exception class name on failure |

---

## Output Schema

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": {
    "personal_info": {
      "full_name": "Jane Smith",
      "email": "jane.smith@email.com",
      "phone": "+1 555 234 5678",
      "location": "San Francisco, CA",
      "linkedin_url": "linkedin.com/in/janesmith",
      "github_url": "github.com/janesmith",
      "portfolio_url": "janesmith.dev",
      "summary": "Senior software engineer with 8 years of experience..."
    },
    "experience": [
      {
        "company": "Acme Corporation",
        "role": "Senior Software Engineer",
        "start_date": "2021-03",
        "end_date": "Present",
        "is_current": true,
        "location": "San Francisco, CA",
        "description": "Led backend architecture for core platform...",
        "achievements": [
          "Reduced API latency by 40% through query optimization",
          "Mentored team of 4 junior engineers"
        ]
      }
    ],
    "education": [
      {
        "institution": "State University",
        "degree": "Bachelor of Science",
        "field_of_study": "Computer Science",
        "start_year": 2012,
        "graduation_year": 2016,
        "gpa": "3.8"
      }
    ],
    "skills": [
      "Python", "FastAPI", "PostgreSQL", "AWS", "Docker",
      "Kubernetes", "React", "TypeScript"
    ],
    "certifications": [
      {
        "name": "AWS Solutions Architect Associate",
        "issuer": "Amazon Web Services",
        "issued_date": "2023-06",
        "expiry_date": "2026-06",
        "credential_id": "ABC123"
      }
    ],
    "projects": [
      {
        "name": "OpenSearch Dashboard",
        "description": "Real-time analytics dashboard for log aggregation",
        "technologies": ["React", "Node.js", "Elasticsearch"],
        "url": "github.com/janesmith/opensearch-dashboard"
      }
    ],
    "languages": ["English", "Spanish"]
  },
  "confidence": {
    "overall": 0.91,
    "personal_info": 0.96,
    "experience": 0.88,
    "education": 0.90,
    "skills": 1.0
  }
}
```

All fields are nullable — missing information is `null`, not omitted.

---

## Confidence Scores

Each score is `0.0` to `1.0`. Use them to surface records that need human review.

| Score | Meaning |
|---|---|
| `0.9 – 1.0` | High confidence — all expected fields present and valid |
| `0.7 – 0.89` | Good — minor gaps (e.g. no LinkedIn URL) |
| `0.5 – 0.69` | Partial — some key fields missing or unverifiable |
| `< 0.5` | Low — significant information missing, recommend human review |

**Scoring weights (overall):**

| Section | Weight |
|---|---|
| Personal info | 35% |
| Experience | 35% |
| Education | 20% |
| Skills | 10% |

---

## Error Handling

| Status | Cause |
|---|---|
| `401 Unauthorized` | Missing or invalid API key |
| `403 Forbidden` | API key revoked |
| `413 Request Entity Too Large` | File exceeds `MAX_FILE_SIZE_MB` |
| `415 Unsupported Media Type` | File extension not supported |
| `422 Unprocessable Entity` | Invalid webhook event name or non-HTTPS URL |
| `429 Too Many Requests` | Rate limit exceeded (per-minute or per-day) |
| `404 Not Found` | Job ID not found or belongs to different company |
| `500 Internal Server Error` | Unhandled pipeline error |

All errors return:

```json
{
  "detail": "Human-readable error description"
}
```

Failed async jobs are surfaced via the job status endpoint and via the `parse.failed` webhook event.
