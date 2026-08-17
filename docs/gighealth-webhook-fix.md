# GigHealth — closing out the webhook 401 and the "stuck job"

**Company id:** `gighealth-1f4cd3`
**Date:** 2026-08-17
**Audience:** GigHealth integration engineers, cc Ocean Blue
**Supersedes** the open items in `ocean-blue-webhook-timing-note.md` §2.

---

## What your integration actually uses

Worth stating, because it narrows the fix a long way:

| Aspect | Value |
|---|---|
| Submit endpoint | `POST /resume/parse` (all traffic) |
| Registrations | **one**, `01KYMMD337H5V02NB08F0HJPFA` → `https://uat-api.gighealth.com/webhook/blue-iq` |
| Subscribed events | `parse.completed`, `parse.failed` |
| Parses to date | 42, **all `completed`** |

So in practice **`parse.completed` is the only event you have ever been sent.** No parse has
ever failed, so `parse.failed` has never fired, and you are not subscribed to
`batch.completed`. Getting `parse.completed` verifying is the whole job.

Base URL is `https://api.parsinglab.blue-iq.ai` — please use that host rather than any
`*.lambda-url.us-east-2.on.aws` URL, which is internal detail and not guaranteed stable.

---

## Part 1 — the "stuck" job was not stuck

Job `01M07ZG3R9M1NQX9C1N722DH41` (`Dr James Mitchell CV`) parsed **successfully in 15.2s**. The
result was in our store the whole time you were seeing `pending`.

| UTC | event |
|---|---|
| 13:45:10.154 | we receive the file (174,830 bytes) |
| 13:45:10.397 | **your only poll** → `200 {"status":"pending"}` |
| 13:45:10.406 | worker starts — 9ms *after* your poll |
| 13:45:25.499 | parse complete, full record stored |

You polled **once, 140ms after submitting**, which landed in the sub-second window before the
worker picked the job up. The Zina Smith job that "worked" is identical except something polled
it a second time ~2m47s later and got the record.

**On us:** that poll should not have returned `pending`. Our submit response says
`status: "processing"` and the endpoint docs promised `processing` until done, so `pending` was
an internal state leaking out. Fixed — **`processing` is now the only non-terminal status you
can ever observe.**

**On you:** one request is not a poll loop. A parse takes 10–30s, so any single poll immediately
after submit will legitimately be non-terminal. You need either a real poll loop or a working
webhook — right now neither is completing, which is why the flow dead-ends.

```text
submit -> persist job_id
       -> loop: GET /api/v1/resume/job/{job_id} every 2-3s, ceiling ~2 min
                stop on completed | partial | failed
                anything else (or unrecognised) = keep polling
```

⚠️ **Results carry a 1-hour TTL.** After that a `job_id` returns `404 JOB_NOT_FOUND` and the
document has to be submitted again. Persist the record as soon as you receive it. The Mitchell
result has since expired; that CV needs re-submitting.

---

## Part 2 — the 401. Our documentation was wrong, and that is very likely your bug

The dashboard docs page at `/docs` described the signature as:

> ~~`X-Signature: sha256=<hex>` — HMAC-SHA256 **of the raw body**, keyed with your endpoint secret.~~

**That is incomplete and, implemented literally, fails 100% of the time.** The timestamp is part
of the signed message. If your handler was written from that page, this is the bug and the
mistake is ours — the page has been corrected and now carries working code in both Node and
Python.

The correct message is the timestamp, a literal dot, then the raw body:

```text
HMAC_SHA256(secret, X-Timestamp + "." + raw_body)
```

Two details each independently break verification:

**1. Include the `X-Timestamp` prefix.** Signing the body alone never matches. The timestamp is
what makes a captured delivery unreplayable, so it has to be inside the digest.

**2. Sign the RAW body bytes.** Capture the body *before* any JSON middleware touches it.
Re-serialising a parsed object changes the bytes — separators, possibly key order — so the
digest differs even with the correct secret. `express.raw()`, not `express.json()`.

```javascript
// Node / Express
const crypto = require("crypto");

app.post("/webhook/blue-iq",
  express.raw({ type: "application/json" }),   // req.body stays a Buffer
  (req, res) => {
    const ts  = req.get("X-Timestamp");
    const sig = req.get("X-Signature");

    if (Math.abs(Date.now() / 1000 - Number(ts)) > 300) return res.sendStatus(400);

    const expected = "sha256=" + crypto
      .createHmac("sha256", process.env.BLUEIQ_WEBHOOK_SECRET)
      .update(ts + ".")           // <-- the timestamp prefix
      .update(req.body)           // <-- the RAW bytes
      .digest("hex");

    const ok = expected.length === sig.length &&
      crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sig));
    if (!ok) return res.sendStatus(401);

    const event = JSON.parse(req.body.toString());   // parse AFTER verifying
    res.sendStatus(202);                             // ack fast, process async
  });
```

```python
# Python / FastAPI
import hashlib, hmac, time

@app.post("/webhook/blue-iq")
async def capture(request: Request):
    raw = await request.body()               # raw bytes, before any parsing
    ts  = request.headers["X-Timestamp"]
    if abs(time.time() - int(ts)) > 300:
        raise HTTPException(400, "stale delivery")

    message  = f"{ts}.".encode() + raw
    expected = "sha256=" + hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers["X-Signature"]):
        raise HTTPException(401, "invalid signature")

    event = json.loads(raw)
    return Response(status_code=202)
```

### Confirm we are even using the same secret

Before changing code, rule the secret out. Neither side needs to send the other a secret —
compare a one-way fingerprint instead:

```bash
# your side
node -e 'console.log(require("crypto").createHash("sha256").update(process.env.S).digest("hex").slice(0,12))'
# or
python -c 'import hashlib,os;print(hashlib.sha256(os.environ["S"].encode()).hexdigest()[:12])'
```

**Ours is `11d4f0fda5d0`, and the secret is 64 hex characters.**

- **Matches** → the secret is right; the fault is in verification. Apply the code above.
- **Differs** → you hold a different registration's secret (or the value was truncated in
  transit — check the length is 64). Tell us and we will re-register and hand over a fresh
  secret; it is shown once and we cannot retrieve the old one.

A fingerprint is not a credential: the secret cannot be recovered from it or used to sign
anything, so it is safe to put in a ticket.

### Why this cost weeks rather than hours

**A 4xx is never retried.** We treat any non-5xx reply as delivered, so every 401 your endpoint
returned **discarded that event permanently** — nothing was queued for later. Two asks:

- **Alert on signature rejections**, don't just log them. A silent 401 is a lost record.
- **Keep a reconcile pass** that polls for any `job_id` you never received a delivery for.
  Yours is what recovered the events; keep it.

---

## Part 3 — what we changed on our side

| Area | Change |
|---|---|
| Poll endpoint | `processing` is now the only non-terminal status; internal `pending` no longer leaks |
| Stale registration | An old ngrok tunnel hook, active since 2026-07-20, **deleted**. It had been receiving every parsed record (name, email, phone, address, work history, licences) for four weeks. `uat-api` is now your only registration, so there is no secret ambiguity left. |
| `/docs` signature docs | Corrected — the timestamp prefix was missing. Working Node + Python verifiers added, plus the raw-body warning. |
| Duplicate registrations | The dashboard now warns before registering a URL that already exists, and explains the two-secret consequence. |
| Rejected deliveries | Now logged as `webhook_rejected_not_retried` at WARNING with the status code, so a 401 is an alertable event instead of a buried info line. |
| Worker observability | Our worker had **no CloudWatch logs at all** (IAM policy scoped to the API log group), which is why we could not see your 401s. Fixed. |
| Retry ladder | The sender's 2s/5s/10s ladder was being cancelled at 15s, so the final attempt was unreachable and the circuit breaker could never open. Budget now derived from the ladder. Only affected 5xx retries — a 401 was always terminal on the first attempt. |
| Signing contract | Now covered by tests that POST real deliveries to a real socket and verify them with the published verifier snippet **copied verbatim**, so our docs and our sender cannot drift apart again. |

---

## Checklist

### Ours

- [x] Stale ngrok registration deleted
- [x] Worker CloudWatch logging restored
- [x] `/docs` signature description corrected, verifier snippets added
- [x] Poll endpoint no longer leaks `pending`
- [x] Signing contract covered by tests
- [ ] Deploy the API + UI changes above
- [ ] Compare secret fingerprints with GigHealth

### Yours

- [ ] Compare your secret's fingerprint against `11d4f0fda5d0`
- [ ] Verify over `X-Timestamp + "." + raw_body`, not the body alone
- [ ] Capture the raw body before any JSON middleware (`express.raw`)
- [ ] Reject deliveries older than 5 minutes; respond 2xx fast, process async
- [ ] Alert on signature rejections — a 401 is never retried
- [ ] Poll in a loop (2–3s, ~2 min ceiling); treat unrecognised statuses as non-terminal
- [ ] Handler idempotent on `job_id`, tolerant of an unknown `job_id`
- [ ] Persist the record on receipt — results expire after 1 hour
- [ ] Re-submit the Dr James Mitchell CV; that result has expired

Quote a `request_id` from any response when raising an issue — our logs are keyed by it.
