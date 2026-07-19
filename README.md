# Resume Parser API

**Enterprise resume parsing for healthcare staffing.** A privacy-first HTTP API that converts
nursing and allied-health resumes - PDF, DOCX, RTF, or scanned images - into structured,
validated, taxonomy-normalized JSON that maps directly onto candidate profile and Work History
forms.

Built for a single enterprise client (BlueIQ) and delivered as a managed API: upload a resume,
receive a complete candidate record with per-field confidence scores, credential and licence
capture, and 350+ clinical specialties resolved to the placement platform's own IDs.

> **New here?** Read [`PROJECT.md`](PROJECT.md) first. It holds the mission, the architecture,
> and the six invariants this system must never break. [`CLEANUP_PLAN.md`](CLEANUP_PLAN.md)
> tracks the known debt.

---

## Why it exists

Healthcare staffing teams re-key candidate data from resumes into placement systems by hand -
slow, error-prone, inconsistent. This service automates that step with healthcare-aware
extraction: it knows a certification is not a state licence, it preserves credential
post-nominals, it keeps travel-assignment history intact per facility, and it normalizes
free-text specialties to a controlled vocabulary the placement platform can match on.

## Capabilities

| Area | What you get |
|---|---|
| **Formats** | Digital PDF, DOCX, RTF, and scanned PDF / PNG / JPG / TIFF / WEBP (OCR) |
| **Accuracy** | Multi-agent extraction with per-role work history and bullet-count verification |
| **Healthcare domain** | 350+ specialty taxonomy, credential and state-licence capture, Work History field mapping |
| **Platform IDs** | Specialty, facility, profession, country, state and city resolved to GigHealth IDs |
| **Resilience** | Three-tier graceful degradation - never returns "nothing" on a hard resume |
| **Confidence** | Per-section 0-1 scores to route low-quality records to human review |
| **Integration** | Synchronous parse, async (OCR) jobs, batch, webhooks, and a correction-feedback loop |
| **Security** | API-key + session auth, magic-byte validation, SSRF-guarded webhooks, no file storage |

---

## Quick start

Send a resume, get a structured candidate record back in one authenticated call:

```bash
curl -X POST https://<your-api-host>/api/v1/resume/parse \
  -H "X-API-Key: rp_live_..." \
  -F "file=@nurse_resume.pdf"
```

**Every section is always present** (empty when the resume doesn't mention it), each role and
credential resolved to the placement platform's own IDs, with a per-section confidence score you
can use to route weak records to human review:

```jsonc
{
  "job_id": "01K...",
  "status": "completed",
  "data": {
    "personal_info": { "full_name": "Jane Smith", "email": "jane@example.com",
                       "phone": "865-541-1111", "credentials": ["RN", "BSN"], "location": "..." },
    "experience": [
      { "company": "Fort Sanders Regional Medical Center", "role": "RN - Med Surg/Tele",
        "start_date": "01/2022", "end_date": "Present", "city": "Knoxville", "state": "TN",
        "state_id": "42", "city_id": "1234", "profession": "RN", "profession_id": "1",
        "specialties": [{ "name": "Med Surg/Tele", "specialty_id": "88", "confidence": 1.0 }],
        "description": ["Charge nurse on a 30-bed telemetry unit", "..."] }
    ],
    "education":      [{ "institution": "University of Tennessee", "degree": "BSN", "graduation_year": 2021 }],
    "skills":         ["Telemetry", "ACLS", "IV Placement"],
    "certifications": [{ "name": "BLS", "issued_date": "01/2024", "expiry_date": "01/2026" }],
    "licenses":       [{ "license_type": "RN", "state": "TN", "is_compact": true }],
    "languages": [], "references": [], "awards": [], "publications": [],
    "projects": [], "professional_associations": [], "clinical_rotations": [],
    "compliance": { "compliance_risk": false },
    "extraction_notes": []
  },
  "confidence": { "overall": 0.9, "personal_info": 1.0, "experience": 1.0,
                  "education": 1.0, "skills": 1.0, "catalog_mapping": 0.8 },
  "skills_validation": { "total": 12, "recognized_count": 9, "recognized_ratio": 0.75 },
  "partial": false,
  "warnings": []
}
```

A hard resume (a scan, or an unusually dense CV) may instead return `status: "processing"` with a
`job_id` to **poll** at `GET /api/v1/resume/job/{job_id}`. A degraded parse comes back with
`partial: true` and human-readable `warnings[]` - **never an empty record, never a silent partial.**

---

## Architecture

**One AWS Lambda** (container image, `us-east-2`) serves the HTTP API *and*, by self-invoking
with `InvocationType="Event"`, runs the async OCR worker. There is no API Gateway - a Lambda
Function URL, optionally fronted by CloudFront for the custom domain. State lives in DynamoDB
(7 tables). Resume bytes pass through S3 transiently and are deleted in a `finally` block.

```
Client --HTTPS--> CloudFront (60s origin read timeout) --> Lambda Function URL
                                                              |
                                                        Mangum -> FastAPI
                                                              |
                            +---------------------------------+--------------+
                            |            app/services/pipeline.py            |
                            |                                               |
                            |  classify -> extract -> clean -> anchors ->   |
                            |  sections -> PARSE -> validate -> normalize   |
                            |  -> catalog-match -> score                    |
                            +---------------------------------+--------------+
                                                              | partial?
                                                     self-invoke (Event)
                                                              v
                                                   async worker, full budget
                                                   -> DynamoDB job -> webhook
```

### The parse ladder

The system **never returns nothing**, and **never returns a silent partial**.

- **Async** (full 200s budget): multi-agent orchestrator -> single-shot -> deterministic floor.
- **Sync** (tight gateway budget): single-shot is *primary* -> on timeout, deterministic floor
  plus a section-only "enrich" pass.

If a parse degrades, `partial: true` and a human-readable `warnings[]` entry says so.

> The full orchestrator was tried on the sync path and **silently dropped all work history** -
> the per-role fan-out got cancelled under the tight budget. That is why the two paths use
> different ladders. Do not "simplify" them back together.

### The agents (`app/services/parsing/agents/`)

| Agent | Job |
|---|---|
| `StructureAgent` | Maps every role + its exact bullet count; splits travel/agency umbrellas per facility |
| `WorkExperienceAgent` | **One LLM call per role**, told the expected bullet count |
| `PersonalInfoAgent` | Name, post-nominals, headline, address, phones, summary |
| `EducationAgent` | Degrees, institutions, in-progress degrees |
| `CredentialsAgent` | Skills / certifications / **state licences** / associations, in one call |
| `SupplementalAgent` | References, awards, publications, languages |
| `ValidatorAgent` | Re-extracts any role whose bullet count differs from the structure map |

A role whose extraction fails is **stubbed from the structure map, never dropped**.

---

## Platform ID mapping

Free text becomes GigHealth IDs. **An unresolved ID comes back `null` - never a guess.**

| Catalog | Source | Live API call at parse time? |
|---|---|---|
| Specialty (350+) | bundled snapshot | No (tiers 1-3.5 offline; tier 4 calls the **LLM**) |
| Facility (8k+) | bundled snapshot | No |
| Geography (country/state) | bundled snapshot | No |
| **City** | *cannot be snapshotted* | **Yes** - live fuzzy search |

Snapshots live in `app/data/` and are refreshed out-of-band:

```bash
python -m scripts.refresh_specialty_catalog
python -m scripts.refresh_facility_catalog
python -m scripts.refresh_geography_catalog
```

All four catalogs are **optional by design**: a missing file yields a null ID and a logged
warning. A bad catalog never breaks a parse.

### Debugging `city_id: null`

City is the only mapping that calls the partner API *during a parse*, so it is the only one that
can fail at runtime. **Read the logs before theorising** - every failure mode now emits a
distinct line, and they tell you which one you are in:

| Log line | Meaning | Fix |
|---|---|---|
| `city_api_disabled` | `ENABLE_CITY_API_MATCH` is false | It defaults to **true**; something set it off |
| `city_api_no_key` | No API key resolved from the environment | Set it on the function (below) |
| `city_api_lookup_failed kind=auth\|forbidden` | Key present but rejected | Wrong key, or no permission on the endpoint |
| `city_api_lookup_failed kind=rate_limited` | Partner quota (per-second or monthly) | Backs off and retries; persistent = quota exhausted |
| `city_api_below_threshold` | Matched, but under `CITY_ACCEPT_MIN` (0.9) | Working as designed - see below |
| `city_api_tier lookups=N matched=N` | **Working** | - |

None of this existed before 2026-07-14: every one of these failures used to be swallowed into an
empty result with **no log line at all**, so a bad key, an exhausted quota and "this city genuinely
has no match" were indistinguishable. That silence - not any one of the failures - was the real
defect, and it is why an earlier `city_id: null` report was misdiagnosed as a missing key when the
key was in fact present.

**Two traps worth knowing:**

- **The key's name is misspelled on purpose.** The platform calls it
  `GIG_SPECIAILITIES_API_KEY`, and `config.py` accepts that spelling via `AliasChoices` alongside
  the correct `GIG_SPECIALTIES_API_KEY`. Seeing only the misspelled one in the Lambda's
  environment does **not** mean the key is missing. (It also authenticates facilities,
  geographies and cities - not just specialties.)
- **A missing key is set on the function itself,** not in this repo and not via Terraform - CI
  deploys only update the function's code, never its environment, and Terraform does not manage
  this infrastructure at all (see [Deployment](#deployment)).

---

## API surface

All paths are under `/api/v1`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Liveness + dependency probe |
| `POST` | `/resume/parse` | API key | Parse one resume. Fast probe; promotes to async if it cannot finish clean |
| `POST` | `/resume/upload-url` | API key | Presigned S3 POST for files above the ~6 MB Function URL cap |
| `POST` | `/resume/parse-uploaded` | API key | Parse a presigned upload |
| `GET` | `/resume/job/{job_id}` | API key | Poll an async job |
| `POST` | `/resume/{job_id}/retry` | API key | Re-run the pipeline |
| `POST` | `/resume/{job_id}/feedback` | API key | Submit corrections (**stores PII - see below**) |
| `POST` | `/resume/batch` | API key | Batch submit (<= 200 files), 202 + per-file job IDs |
| `GET` | `/resume/batch/{batch_id}` | API key | Batch progress |
| `POST`/`GET`/`DELETE` | `/webhooks` | API key | HMAC-signed delivery registration |
| `POST` | `/auth/signup`, `/auth/login` | none | Self-serve accounts |
| `GET`/`POST` | `/account/keys`, `/account/usage` | Bearer | Manage your own keys and usage |
| `*` | `/admin/*` | Admin token | Company onboarding, keys, usage, stats |

**Callers behind a tight gateway must not block on a parse.** A single-shot parse of even a
typical two-role resume takes ~20s, and a dense 12-role one takes 39-55s. There is no budget
that makes a complete synchronous parse fit inside a 30s ceiling (Amplify SSR, for example).
Pass `async_only` and poll.

---

## Security and data retention

| Control | Implementation |
|---|---|
| **Authentication** | Per-company API keys (`X-API-Key`, SHA-256 at rest), session tokens (Bearer), separate admin token |
| **File validation** | Magic-byte checks reject type-spoofed uploads before processing; size caps on every entry path |
| **SSRF protection** | Webhook URLs validated against private/loopback/metadata ranges at registration **and** at delivery (defeats DNS rebinding) |
| **Tenant isolation** | Every job, webhook and feedback record is scoped to a `company_id` |
| **Encryption** | SSE on S3; encryption at rest on DynamoDB |
| **Logging** | Structured, correlatable by `X-Request-ID` - and **never any resume content** (filenames embed candidate names, so only extension and length are logged) |

### Data retention

| Data | Where | Retained |
|---|---|---|
| Resume file (bytes) | S3 `temp/{job_id}/` | Deleted in a `finally` block. A lifecycle rule expires any leak after 1 day |
| Parsed result - sync | Response body only | Not retained |
| Parsed result - async | DynamoDB `jobs` | 1 hour (TTL) |
| Audit log | DynamoDB `audit-logs` | 90 days - **metadata only**, no content, no PII |
| **Feedback - original + corrected JSON** | DynamoDB `feedback` | **90 days - contains candidate PII** |

**The feedback table is the one place parsed resume content is stored.** It is written *only*
when a caller explicitly submits corrections to `POST /resume/{job_id}/feedback`, so the parser
can be improved. The submitted JSON includes name, email, phone, address, work history and
licences. Callers whose data policy forbids that should simply not call the endpoint - nothing
else in the integration depends on it.

---

## Development

```bash
poetry install          # deps (poetry.lock IS committed - builds are reproducible)
make dev                # docker-compose + LocalStack (S3 + DynamoDB)
make test               # full suite
make lint               # ruff
make typecheck          # mypy
```

**Quality gate:** ruff + mypy + `pytest --cov-fail-under=70`. Currently **560 tests, 78%**.

Note `Dockerfile` is **dev-only**; `Dockerfile.lambda` is what ships.

### A note on non-ASCII

Source, docs and everything the API emits are **ASCII**. Three things are deliberately not:

- Platform specialty names carry en dashes (`"Progressive Care - Oncology"` in the catalog) and
  must match GigHealth byte for byte.
- The bullet and dash character classes in the parsers - those glyphs are what real resumes use.
- Candidate names. The parser **preserves** international names (Jose, Garcia, and so on); that
  is a feature, and there is a test pinning it.

## Deployment

- **Deploy:** push to `main` -> GitHub Actions builds `Dockerfile.lambda`, pushes to ECR,
  updates the function, then runs a retrying health smoke test. **CI owns the image.**
- **Environment:** CI never touches Lambda env vars or sizing. A redeploy will **not** pick up
  a new secret - it must be set on the function itself.
- **Rollback:** `rollback.yml` (`workflow_dispatch`) - verifies the tag in ECR, updates, smoke
  tests. Shares a concurrency group with deploy so the two cannot race.

> ### Terraform does not manage this infrastructure
>
> `infrastructure/terraform/` **describes** the stack, but it has never been applied: the state
> bucket it points at (`resume-parser-tfstate`) does not exist, and `main.tf` still carries the
> "create this bucket manually first" note. The running Lambda, the 7 DynamoDB tables, the S3
> bucket and the IAM roles were all created **outside** Terraform.
>
> **So `terraform apply` is not a safe way to change anything here.** With empty state it would
> not update the running resources - it would try to *create* all 19 of them, every one of which
> already exists. Until the stack is adopted (`terraform import`, one resource at a time - **not**
> an apply), treat these files as a description, not as the source of truth. Change live config on
> the resource itself.
>
> Tracked in [`CLEANUP_PLAN.md`](CLEANUP_PLAN.md) §E.

## Limits

- Function URL caps a request body at ~6 MB. Larger files use the presigned-upload flow.
- Batch: 200 files per request.
- Rate limiting (`core/rate_limit.py`) is in-process and **disabled by default**. It does not
  survive a cold start and does not coordinate across concurrent Lambdas - front the API with a
  usage plan for a strict global cap.
