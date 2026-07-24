# Ocean Blue — Webhook Timing & Request Latency (Remediation Note)

**Audience:** Ocean Blue integration engineers
**Subject:** (1) "the `parse.completed` webhook arrives before our `POST /resume/parse` call
returns" and (2) "Postman calls are faster than calls through our UI/system."

**TL;DR:** Both symptoms have the **same root cause** — latency added inside your
UI → backend path. Our submit endpoint returns in ~1s and the webhook is *always* sent
only after parsing fully completes (verified below). When your request path is slow, the
worker can finish and call your webhook before your own code has recorded the `job_id`,
which looks like "the webhook fired first." Fix the request path and make the webhook
handler tolerant of early delivery, and both issues go away.

---

## 1. The webhook is never sent before parsing completes

On our side the ordering is guaranteed. The submit request and the webhook run on **two
separate channels**:

- **Submit response** — `POST /resume/parse` only validates the file, stores it, opens a
  job row, and enqueues a background job, then returns `{ job_id, status: "processing",
  poll_url }`. **Nothing parses on the request path.** This returns in ~1s.
- **Webhook** — a *separate* background worker runs the full pipeline and only *then*, after
  the parsed result is written, POSTs `parse.completed` to your endpoint. Parsing takes
  seconds (up to ~55s for a dense resume), so the webhook is always emitted well *after* the
  submit response leaves our system.

So a `parse.completed` you receive "early" is not early on our clock — it arrives before
**your** code finished handling the submit response, because that handling was delayed on
your side.

## 2. Why it *looks* like the webhook comes first

```
Your backend                         Our API                 Our worker
     │  POST /resume/parse ─────────────▶ (enqueue, ~1s) │
     │                                      returns 202 ──┼──▶ (travels back to you)
     │   ‹‹ 202 delayed in your stack ››                 │  parse runs (seconds)
     │                                                   │  parse.completed ──▶ your /hooks
     │   ‹‹ still haven't stored job_id ›› ◀───────────────────── arrives FIRST
```

If your handling of the 202 is delayed — an extra proxy/SSR hop, a slow file upload, or code
that waits for inline `data` — the worker can finish and call your webhook endpoint **before**
you have persisted the `job_id`. Your webhook handler then can't correlate the event and it
appears the webhook "beat" the response.

## 3. What to change (client side)

1. **Persist the `job_id` immediately from the submit response**, before doing any other work.
   Don't wait until after downstream processing to record it.

2. **Make the webhook handler tolerant of an unknown `job_id`.** A fast parse can legitimately
   deliver `parse.completed` before your own bookkeeping settles. Instead of dropping an event
   for an unrecognized `job_id`, **upsert** it (or buffer + short-retry). Key everything on
   `job_id` and keep the handler idempotent — a delivery may also arrive more than once.

3. **Use the single-shot `POST /resume/parse`** for files under ~6 MB. If your UI is using the
   3-step presigned flow (`upload-url` → S3 upload → `parse-uploaded`) for small files, that's
   three round trips plus an S3 upload where one multipart POST would do — drop it to one call.
   Reserve the presigned flow for files that actually exceed the ~6 MB edge cap.

4. **Don't wait for inline `data`.** The POST **never** returns parsed `data` — it always
   returns `status: "processing"`. If any code still blocks/retries waiting for `data` in the
   POST response, it will spin needlessly and appear slow. Read the `job_id`, then poll
   `poll_url` or wait for the webhook.

5. **Respond 2xx to the webhook quickly**, then process asynchronously. Verify the HMAC
   signature against the **raw** body (see the Integration Guide, Section 6).

## 4. Why Postman is faster than your UI/system

The submit endpoint is ~1s for *every* caller. Postman is faster only because it skips work
your UI path adds — it is not a difference in our API:

| Postman | Your UI / system |
|---|---|
| One direct `POST /resume/parse` to our URL | Browser → your frontend/SSR → your backend → our API (extra hops) |
| No framework in the middle | Likely behind AWS Amplify SSR (hard 30s ceiling) + auth/session middleware per request |
| Single multipart upload | May be running the 3-step presigned flow for small files |
| Reads the 202 and stops | May still block/poll-tight waiting for inline `data` |
| Warm function during testing | Sporadic real traffic can hit cold starts |
| Sends the file bytes as-is | Browser file read / base64 / re-encode / scan before it reaches us |

Closing those gaps (fewer hops, single-shot upload, stop waiting for inline `data`) both speeds
up the request **and** removes the latency that was letting the webhook arrive before your code
recorded the `job_id`.

## 5. Checklist

- [ ] Record `job_id` immediately from the submit response
- [ ] Webhook handler upserts / tolerates an unknown `job_id` (no dropping), idempotent on `job_id`
- [ ] Use single-shot `/resume/parse` for files < ~6 MB; presigned flow only for larger files
- [ ] No code path waits for inline `data` in the POST response
- [ ] Verify HMAC on the raw body; respond 2xx fast, process async
- [ ] Confirm which layers sit between your app and our API (SSR/proxy/middleware) and their added latency

Questions: include a `request_id` from any response where relevant.
