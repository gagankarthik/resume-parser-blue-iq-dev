# Resume Parser API

Enterprise resume parsing for healthcare staffing. The Resume Parser API converts a nurse or
allied-health resume — PDF, Word, RTF, or a photographed scan — into structured, validated JSON
that drops straight into a candidate profile and Work History form. Every specialty, facility,
licence, and location is resolved to the placement platform's own IDs, and every section carries a
confidence score so your team knows what to trust and what to review.

**Why it exists:** staffing coordinators re-key candidate data from resumes by hand — slow,
inconsistent, and error-prone. This service automates that step with genuine healthcare
understanding: it knows a certification is not a state licence, keeps a credential's post-nominals
intact, decomposes a travel nurse's assignments into the facilities where the work happened, and
maps free-text specialties to a controlled vocabulary the platform can match on. Built for
**BlueIQ**, delivered as a managed HTTPS API on the **GigHealth** taxonomy.

| | |
|---|---|
| **Formats** | Digital PDF, DOCX, RTF, and scanned PDF / PNG / JPG / TIFF / WEBP (OCR) |
| **Healthcare domain** | 350+ specialties, credential & state-licence capture, per-facility travel history |
| **Platform IDs** | Specialty, facility, profession, country, state, and city resolved to GigHealth IDs |
| **Confidence** | Per-section 0–1 scores to route weak records to human review |
| **Honesty** | Never an empty record and never a silent partial — a degraded parse says so |
| **Security** | API-key & session auth, magic-byte validation, SSRF-guarded webhooks, no resume storage |

---

## Installation

**Prerequisites:** Python 3.12, [Poetry](https://python-poetry.org/), Docker (for local AWS via
LocalStack), and the Tesseract OCR binary (`tesseract-ocr`) for scanned documents.

```bash
git clone https://github.com/gagankarthik/resume-parser-blue-iq-dev.git
cd resume-parser-blue-iq-dev

poetry install                 # install dependencies (poetry.lock is committed)
cp .env.example .env           # then fill in OPENAI_API_KEY, GIG_SPECIALTIES_API_KEY, AUTH_SECRET

docker-compose up              # API + LocalStack (S3 + DynamoDB) for local development
```

The service needs an `OPENAI_API_KEY` (model: `gpt-4.1-mini`) and, for platform-ID resolution, a
`GIG_SPECIALTIES_API_KEY`. All settings are documented in [`.env.example`](.env.example).

---

## Usage

One uniform flow for **every** file: submit it, then poll (or take a webhook) for the JSON. Submit
a resume:

```bash
curl -X POST https://<your-api-host>/api/v1/resume/parse \
  -H "X-API-Key: rp_live_..." \
  -F "file=@nurse_resume.pdf"
# -> { "job_id": "01K...", "status": "processing", "poll_url": "/api/v1/resume/job/01K..." }
```

Every request returns a `job_id` + `poll_url` **immediately** — nothing parses on the request path,
so it never blocks or trips a gateway timeout, whatever the file's type or size. A background worker
runs the full parse (digital text extraction or OCR, then the AI parse ladder), and you retrieve the
structured JSON by polling `GET /api/v1/resume/job/{job_id}` until `status` is `completed`, or by
registering a webhook:

```bash
curl https://<your-api-host>/api/v1/resume/job/01K... -H "X-API-Key: rp_live_..."
# -> { "status": "completed", "data": { ...parsed resume... }, "confidence": {...}, "partial": false }
```

> **Note (breaking change):** the API used to return parsed JSON inline on the POST for digital
> files. It now **always** returns a `job_id` to poll. Direct callers that read `data` from the POST
> response must switch to polling (or a webhook). The `async_only` flag is deprecated and ignored —
> everything is async now.

**Core endpoints** (all under `/api/v1`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/resume/parse` | Parse one resume |
| `POST` | `/resume/upload-url` → `/resume/parse-uploaded` | Presigned upload for files above ~6 MB |
| `GET` | `/resume/job/{job_id}` | Poll an async job |
| `POST` | `/resume/batch` | Batch submit (≤ 200 files) |
| `POST` | `/resume/{job_id}/feedback` | Submit corrections to improve accuracy |
| `POST`/`GET`/`DELETE` | `/webhooks` | Manage HMAC-signed delivery |

Full integration walkthrough: [`docs/CLIENT_INTEGRATION_GUIDE.md`](docs/CLIENT_INTEGRATION_GUIDE.md).

**Developer commands:**

```bash
poetry run pytest              # full test suite
poetry run ruff check app/     # lint
poetry run mypy app/           # type-check
python -m benchmark.run        # score parser output against the labelled benchmark
```

---

## Examples

A parsed record — every section present, each role and credential resolved to platform IDs, with
per-section confidence:

```jsonc
{
  "job_id": "01K...",
  "status": "completed",
  "data": {
    "personal_info": { "full_name": "Jane Smith", "email": "jane@example.com",
                       "phone": "865-541-1111", "credentials": ["RN", "BSN"] },
    "experience": [
      { "company": "Fort Sanders Regional Medical Center", "role": "RN - Med Surg/Tele",
        "start_date": "01/2022", "end_date": "Present", "city": "Knoxville", "state": "TN",
        "state_id": "42", "city_id": "1234", "profession": "RN", "profession_id": "1",
        "specialties": [{ "name": "Med Surg/Tele", "specialty_id": "88", "confidence": 1.0 }],
        "description": ["Charge nurse on a 30-bed telemetry unit"] }
    ],
    "education":      [{ "institution": "University of Tennessee", "degree": "BSN", "graduation_year": 2021 }],
    "certifications": [{ "name": "BLS", "issued_date": "01/2024", "expiry_date": "01/2026" }],
    "licenses":       [{ "license_type": "RN", "state": "TN", "is_compact": true }]
  },
  "confidence": { "overall": 0.9, "experience": 1.0, "catalog_mapping": 0.8 },
  "partial": false,
  "warnings": []
}
```

A degraded parse returns `partial: true` with human-readable `warnings[]` — **never an empty
record, never a silent partial.** An unresolved platform ID comes back `null`, never a guess.

---

## License

**Proprietary.** © Ocean Blue Solutions. All rights reserved. Built exclusively for BlueIQ; not
licensed for redistribution or use outside that engagement.

---

## Contributors & Contact

Developed and maintained by **Ocean Blue Solutions**.

- Maintainer: [@gagankarthik](https://github.com/gagankarthik)
- Contact: **oceanbluesolutions@gmail.com**

For engineering documentation (architecture, deployment, operations, and the rules for changing the
system safely), see [`docs/`](docs/) — start with [`docs/PROJECT.md`](docs/PROJECT.md).
