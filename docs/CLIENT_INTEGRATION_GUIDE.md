# Resume Parser API — Client Integration Guide

This guide is for engineers integrating the Resume Parser API into their own
application (ATS, candidate portal, internal tooling). The API accepts a resume
file (PDF, DOCX, or image) and returns clean, structured JSON suitable for
auto-filling candidate forms.

- **Base URL:** `https://<your-function-url>/` (provided to you with your API key)
- **Auth:** API key in the `X-API-Key` header
- **Content:** `multipart/form-data` for uploads; JSON for everything else
- **All endpoints are under** `/api/v1`

---

## 1. Quick start

```bash
curl -X POST "https://<your-function-url>/api/v1/resume/parse" \
  -H "X-API-Key: rp_live_your_key_here" \
  -F "file=@/path/to/resume.pdf"
```

A digital PDF/DOCX returns the parsed result directly (synchronous). A scanned
PDF or image returns a `job_id` to poll (asynchronous). Both paths are covered
below.

---

## 2. Authentication

Every request (except `/health`) must include your API key:

```
X-API-Key: rp_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- Keys are issued to you out-of-band. **Treat the key as a secret** — store it
  server-side, never ship it in browser/mobile code.
- If a key is compromised, contact us to revoke and reissue it.

| Failure | HTTP | `error_code` |
|---|---|---|
| No header | 401 | `MISSING_API_KEY` |
| Malformed key | 401 | `INVALID_API_KEY_FORMAT` |
| Unknown key | 401 | `INVALID_API_KEY` |
| Revoked key | 403 | `REVOKED_API_KEY` |

---

## 3. Parsing a resume

### `POST /api/v1/resume/parse`

**Request** — `multipart/form-data` with a single `file` field.

| Constraint | Value |
|---|---|
| Field name | `file` |
| Supported types | `.pdf`, `.docx`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.webp` |
| Max size | **10 MB** (application limit). Note: the endpoint is currently fronted by a Lambda Function URL that caps requests at ~6 MB at the edge — uploads above ~6 MB need the presigned-S3 upload flow. Contact us for files larger than ~6 MB. |

Files are validated by **magic bytes**, not just extension — a renamed file is rejected.

### Two response modes

The processing path is chosen automatically from the file:

- **Digital PDF / DOCX → synchronous.** The full result is in the response.
- **Scanned PDF / image → asynchronous** (OCR is slow). You get a `job_id`;
  fetch the result by polling (Section 4) or via a webhook (Section 6).

**Synchronous response** (`status: "completed"`):

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": { "...": "see Output schema (Section 8)" },
  "confidence": { "overall": 0.91, "personal_info": 0.96, "experience": 0.88, "education": 0.90, "skills": 1.0 },
  "poll_url": null
}
```

**Asynchronous response** (`status: "processing"`):

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "processing",
  "data": null,
  "confidence": null,
  "poll_url": "/api/v1/resume/job/01J3K5M2N4P6Q8R0S2T4U6V8W0"
}
```

> **Integration tip:** branch on `status`. If `completed`, use `data` immediately.
> If `processing`, poll `poll_url` or wait for the `parse.completed` webhook.

---

## 4. Polling an async job

### `GET /api/v1/resume/job/{job_id}`

```json
{
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "completed",
  "data": { "...": "..." },
  "confidence": { "...": "..." },
  "error": null
}
```

- Statuses: `pending` → `processing` → `completed` | `failed`.
- On `failed`, `error` contains a description and `data` is `null`.
- **Results live for 1 hour**, then expire (`404 JOB_NOT_FOUND`). Fetch and persist
  them on your side promptly.
- Suggested polling: every 2–3 s, with a ~2 min ceiling. Prefer webhooks for scale.

---

## 5. Other endpoints

### Retry a parse — `POST /api/v1/resume/{job_id}/retry`
Re-upload the **same file** to re-run extraction + AI when a result was poor. Returns
a new `job_id` linked to the original. Up to 3 retries per job (`RETRY_LIMIT_REACHED`
after that). Same sync/async behavior as `/parse`.

### Submit feedback — `POST /api/v1/resume/{job_id}/feedback`
After a user reviews and corrects a parsed resume, send the original parser JSON and the
corrected JSON so we can improve parsing accuracy. Server-to-server (uses your `X-API-Key`).
Returns `202 Accepted` — feedback is processed asynchronously.

```jsonc
// request body
{
  "original": { /* the JSON returned by /resume/parse */ },
  "updated":  { /* the user-corrected JSON */ },
  "changed":  true,            // optional — derived from the diff if omitted
  "profile_id": "gig-8821",    // optional — your record id
  "notes": "fixed name suffix" // optional
}
```
```jsonc
// 202 response
{
  "feedback_id": "01J3K5M9N1P3Q5R7S9T1U3V5W7",
  "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
  "status": "accepted",
  "changed": true,
  "changed_fields": ["personal_info.full_name", "skills[1]"],
  "created_at": "2026-06-03T12:34:56+00:00"
}
```
Send it only when the user actually changed something, or always as a quality signal —
both are accepted. `changed_fields` lists the exact leaf paths that differed. Payloads are
capped at ~350 KB. Records are retained 90 days then auto-deleted.

### Batch — `POST /api/v1/resume/batch`
`multipart/form-data` with multiple `files`. Up to **200** files. Returns `202` with a
`batch_id` and per-file `job_ids`; invalid files are listed in `skipped_files`. All
files process asynchronously — track via per-file `parse.completed` webhooks and the
`batch.completed` webhook, or poll:

### Batch status — `GET /api/v1/resume/batch/{batch_id}`
Returns `total` / `completed` / `failed` / `processing` counts. Batch records live for 24 h.

### Health — `GET /api/v1/health`
No auth required. Returns `status` (`ok`/`degraded`) and dependency status. Use for uptime checks.

---

## 6. Webhooks (recommended for async)

Instead of polling, register a webhook and we'll POST results to your endpoint.

### Register — `POST /api/v1/webhooks`

```json
{ "url": "https://your-server.com/hooks/resume", "events": ["parse.completed", "parse.failed"] }
```

Response (`201`) — **the `hmac_secret` is returned only once; store it now:**

```json
{
  "webhook_id": "01J3...",
  "url": "https://your-server.com/hooks/resume",
  "events": ["parse.completed", "parse.failed"],
  "hmac_secret": "a3f8d2e1c9b7...",
  "status": "active",
  "created_at": "2026-06-02T10:00:00+00:00"
}
```

- Your URL must be **public HTTPS** (private/loopback/internal addresses are rejected).
- Manage with `GET /api/v1/webhooks` and `DELETE /api/v1/webhooks/{webhook_id}`.
- Available events: `parse.completed`, `parse.failed`, `batch.completed`.

### Delivery payloads

`parse.completed`:
```json
{ "event": "parse.completed", "job_id": "01J3...", "data": { "...": "Output schema" } }
```
`parse.failed`:
```json
{ "event": "parse.failed", "job_id": "01J3...", "error": "OCR failed: ..." }
```
`batch.completed`:
```json
{ "event": "batch.completed", "batch_id": "01J3...", "total": 50, "completed": 48, "failed": 2 }
```

### Verifying the signature (do this on every delivery)

Each request carries these headers:

```
X-Signature: sha256=<hex digest>
X-Timestamp: <unix seconds>
X-Event:     parse.completed
```

The signature is `HMAC_SHA256(secret, "<timestamp>." + raw_body)`. Verify against the
**raw request body** (not a re-serialized object), and reject deliveries older than 5 minutes.

```python
import hmac, hashlib, time

def verify(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:      # reject replays > 5 min
        return False
    message  = f"{timestamp}.".encode() + raw_body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

```javascript
const crypto = require("crypto");

function verify(secret, timestamp, rawBody, signature) {
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  const expected =
    "sha256=" + crypto.createHmac("sha256", secret).update(`${timestamp}.`).update(rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
}
```

**Delivery semantics:** retried up to 3 times (≈2 s, 5 s, 10 s) on 5xx/connection errors;
2xx/4xx are not retried. Respond `2xx` quickly and process asynchronously. Make your
handler **idempotent** (key on `job_id`) — a delivery may arrive more than once.

---

## 7. Rate limits

There are no request-rate limits on the API. Please still upload responsibly
(reasonable concurrency) so the async OCR workers aren't starved.

---

## 8. Output schema

`data` (present on `completed`). All fields are nullable — missing info is `null`, not omitted.

```json
{
  "personal_info": {
    "full_name": "Jane Smith",
    "email": "jane.smith@email.com",
    "phone": "+1 555 234 5678",
    "location": "San Francisco, CA",
    "linkedin_url": "linkedin.com/in/janesmith",
    "github_url": "github.com/janesmith",
    "portfolio_url": "janesmith.dev",
    "summary": "Senior engineer with 8 years of experience..."
  },
  "experience": [
    {
      "company": "Acme Corporation",
      "role": "Senior Software Engineer",
      "start_date": "2021-03",
      "end_date": "Present",
      "is_current": true,
      "location": "San Francisco, CA",
      "description": "Led backend architecture...",
      "achievements": ["Reduced API latency by 40%", "Mentored 4 engineers"]
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
  "skills": ["Python", "FastAPI", "AWS", "Docker"],
  "certifications": [
    { "name": "AWS Solutions Architect", "issuer": "Amazon Web Services", "issued_date": "2023-06", "expiry_date": "2026-06", "credential_id": "ABC123" }
  ],
  "projects": [
    { "name": "Analytics Dashboard", "description": "Real-time log analytics", "technologies": ["React", "Node.js"], "url": "github.com/janesmith/dash" }
  ],
  "languages": ["English", "Spanish"]
}
```

Dates are normalized to ISO `YYYY-MM` where possible (`"Present"` for current roles).

### Confidence scores

`confidence` is `0.0–1.0` per section — use it to route low-confidence records to human review.

| Range | Meaning |
|---|---|
| 0.90–1.00 | High — fields present and valid |
| 0.70–0.89 | Good — minor gaps |
| 0.50–0.69 | Partial — verify key fields |
| < 0.50 | Low — recommend manual review |

---

## 9. Errors

All errors share one envelope:

```json
{
  "error": {
    "status_code": 413,
    "error_code": "FILE_TOO_LARGE",
    "detail": "File size 12288 KB exceeds the 10 MB limit",
    "hint": "The uploaded file is too large. Maximum size is 10 MB...",
    "request_id": "a1b2c3d4-..."
  }
}
```

- **`error_code`** — machine-readable; branch your logic on this.
- **`hint`** — plain-language message safe to show end users.
- **`request_id`** — include it in any support request.

| HTTP | Common `error_code`s |
|---|---|
| 400 | `INVALID_REQUEST` |
| 401 | `MISSING_API_KEY`, `INVALID_API_KEY`, `INVALID_API_KEY_FORMAT` |
| 403 | `REVOKED_API_KEY` |
| 404 | `JOB_NOT_FOUND`, `BATCH_NOT_FOUND`, `WEBHOOK_NOT_FOUND` |
| 413 | `FILE_TOO_LARGE` |
| 415 | `UNSUPPORTED_FILE_TYPE`, `CORRUPTED_FILE` |
| 422 | `VALIDATION_ERROR`, `BATCH_TOO_LARGE`, `EMPTY_BATCH`, `RETRY_LIMIT_REACHED` |
| 500 | `PARSE_FAILED`, `EXTRACTION_FAILED`, `OCR_FAILED`, `INTERNAL_ERROR` |

For transient `5xx`, retry with exponential backoff. For `4xx`, fix the request.

---

## 10. End-to-end examples

### Python (sync + async)

```python
import time, requests

BASE = "https://<your-function-url>/api/v1"
HEADERS = {"X-API-Key": "rp_live_your_key_here"}

def parse_resume(path: str) -> dict:
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/resume/parse", headers=HEADERS, files={"file": f})
    r.raise_for_status()
    res = r.json()

    if res["status"] == "completed":
        return res["data"]

    # async — poll the job
    job_id = res["job_id"]
    for _ in range(60):                       # ~2 minutes
        time.sleep(2)
        jr = requests.get(f"{BASE}/resume/job/{job_id}", headers=HEADERS).json()
        if jr["status"] == "completed":
            return jr["data"]
        if jr["status"] == "failed":
            raise RuntimeError(jr["error"])
    raise TimeoutError("parse did not finish in time")

print(parse_resume("resume.pdf"))
```

### Node.js (sync + async)

```javascript
const BASE = "https://<your-function-url>/api/v1";
const HEADERS = { "X-API-Key": "rp_live_your_key_here" };

async function parseResume(file /* Blob/File */) {
  const form = new FormData();
  form.append("file", file);
  let res = await (await fetch(`${BASE}/resume/parse`, { method: "POST", headers: HEADERS, body: form })).json();

  if (res.status === "completed") return res.data;

  const jobId = res.job_id;
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const jr = await (await fetch(`${BASE}/resume/job/${jobId}`, { headers: HEADERS })).json();
    if (jr.status === "completed") return jr.data;
    if (jr.status === "failed") throw new Error(jr.error);
  }
  throw new Error("parse did not finish in time");
}
```

---

## 11. Data & privacy

- **Resume files are never stored.** They are processed in memory / a temp location and
  deleted immediately after parsing.
- **Parsed results** for async jobs are kept for **1 hour** then auto-deleted; sync results
  are returned in the response and never stored.
- We retain only **content-free audit metadata** (job id, file type/size, status, timings).
- Resume text is sent to OpenAI for parsing. No copy is retained by us.

---

## 12. Integration checklist

- [ ] Store the API key server-side (never in client code)
- [ ] Branch on `status` (`completed` vs `processing`)
- [ ] Handle both sync results and async polling/webhooks
- [ ] Register a webhook and **verify the HMAC signature** on every delivery
- [ ] Make webhook handling idempotent (key on `job_id`)
- [ ] Persist results within the 1-hour window
- [ ] Surface `error.hint` to users; log `error.request_id` for support
- [ ] Keep uploads under 10 MB (and under ~6 MB while the API is fronted by a Lambda Function URL)

Questions or higher limits: contact us with your `request_id` where relevant.
