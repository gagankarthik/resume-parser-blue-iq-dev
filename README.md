# Resume Parser API

**Turn healthcare resumes into placement-ready candidate records — automatically.**

The Resume Parser API converts a nurse or allied-health resume — PDF, Word, RTF, or a
photographed scan — into structured, validated JSON that drops straight into a candidate
profile and Work History form. Every specialty, facility, licence, and location is resolved to
the placement platform's own IDs, and every section carries a confidence score so your team
knows what to trust and what to review.

Built for **BlueIQ**, delivered as a managed HTTPS API on the **GigHealth** taxonomy.

---

## The problem it solves

Staffing coordinators re-key candidate data from resumes into placement systems by hand — slow,
inconsistent, and error-prone in ways that surface later as a bad placement. This service removes
that step with genuine healthcare understanding: it knows a certification is not a state licence,
it keeps a credential's post-nominals intact, it decomposes a travel nurse's assignments into the
facilities where the work actually happened, and it maps free-text specialties to a controlled
vocabulary your platform can match on.

The result: minutes of manual entry become one API call, and weak records are flagged for review
instead of quietly entering your pipeline wrong.

## What you get

| Area | Capability |
|---|---|
| **Formats** | Digital PDF, DOCX, RTF, and scanned PDF / PNG / JPG / TIFF / WEBP (OCR) |
| **Healthcare domain** | 350+ clinical specialties, credential & state-licence capture, per-facility travel history, Work History field mapping |
| **Platform IDs** | Specialty, facility, profession, country, state, and city resolved to GigHealth IDs |
| **Accuracy** | Per-role work-history extraction with bullet-count verification |
| **Confidence** | Per-section 0–1 scores to route low-quality records to human review |
| **Honesty** | Never returns an empty record and never a silent partial — a degraded parse says so |
| **Integration** | Synchronous parse, async (OCR) jobs, batch upload, webhooks, and a correction-feedback loop |
| **Security** | API-key & session auth, magic-byte file validation, SSRF-guarded webhooks, no resume storage |

---

## Quick start

Send a resume, get a structured candidate record back in one authenticated call:

```bash
curl -X POST https://<your-api-host>/api/v1/resume/parse \
  -H "X-API-Key: rp_live_..." \
  -F "file=@nurse_resume.pdf"
```

**Every section is always present** (empty when the resume doesn't mention it), each role and
credential resolved to the placement platform's own IDs, with a per-section confidence score:

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

A scan or an unusually dense CV may instead return `status: "processing"` with a `job_id` to
**poll** at `GET /api/v1/resume/job/{job_id}` (or receive a webhook). A degraded parse comes back
with `partial: true` and human-readable `warnings[]` — **never an empty record, never a silent
partial.**

> **Integrating behind a short gateway timeout** (e.g. a 30 s serverless host)? Send
> `async_only=true` and poll — a complete parse can take longer than a blocking request allows.
> See the [Client Integration Guide](docs/CLIENT_INTEGRATION_GUIDE.md).

---

## Platform ID mapping

Free text becomes GigHealth IDs, so records map onto your forms with no manual lookup. **An
unresolved ID comes back `null` — never a guess.**

| Catalog | Coverage |
|---|---|
| Specialty | 350+ clinical specialties, profession-scoped |
| Facility | 8,000+ facilities |
| Geography | Countries and states |
| City | Live lookup against the partner catalog |

## Security & data privacy

| Control | What it means |
|---|---|
| **Authentication** | Per-company API keys, session tokens, and a separate admin token |
| **No resume storage** | Resume bytes pass through transiently and are deleted immediately after parsing |
| **Tenant isolation** | Every job, webhook, and record is scoped to your company |
| **Encryption** | Encrypted at rest and in transit |
| **PII-safe logging** | Resume content is never logged |

**One deliberate exception:** the optional feedback endpoint stores submitted original + corrected
JSON (which contains candidate PII) for 90 days, so the parser can be improved. It is written
*only* when you explicitly submit corrections — if your data policy forbids that, simply don't
call it; nothing else depends on it.

---

## Documentation

| Guide | For |
|---|---|
| [Client Integration Guide](docs/CLIENT_INTEGRATION_GUIDE.md) | Consuming the API: auth, sync vs. async, polling, webhooks, examples |
| [Architecture](docs/ARCHITECTURE.md) | How the system is built and why |
| [Deployment & Operations](docs/DEPLOYMENT.md) | CI/CD, AWS services, OpenAI configuration, runbook |
| [Engineering guide](docs/PROJECT.md) | Mission, invariants, and rules for changing the system safely |
| [Custom API domain](docs/custom-api-domain.md) | CloudFront + ACM setup for a branded hostname |

Engineers building or operating the service should start with the
[Engineering guide](docs/PROJECT.md).

## Support

This is a managed, single-tenant service for BlueIQ. For access, API keys, or integration help,
contact the Ocean Blue Solutions team.
