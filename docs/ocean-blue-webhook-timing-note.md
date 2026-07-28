# Ocean Blue — Webhook Timing & Request Latency

**Audience:** Ocean Blue / GigHealth integration engineers
**Status:** Supersedes the note of 2026-07-21. That note attributed the slow submit and the
early webhook to latency in the client's request path. **That was wrong** — the cause was on
our side, and the UAT timings supplied on 2026-07-24 disproved it. This version replaces it.

---

## TL;DR

Two separate defects, both ours:

1. **`POST /resume/parse` was holding the connection for the whole parse (~22s).** The API was
   deployed without its async worker queue, so parsing silently fell back to running
   in-process on the request thread. That is also why `parse.completed` arrived *before* the
   submit response delivered the `job_id` — the webhook fires at the end of the parse, and the
   response could not return until the parse (and the rest of the job's bookkeeping) finished.
2. **The UAT webhook signature mismatch** is a secret mismatch caused by **two active webhook
   registrations** for the same company, each with its own independent signing secret.

The client's instrumentation was accurate: their storage write, commit, and `job_id` persistence
were all sub-150ms, and none of the 22.6s was theirs.

---

## 1. The slow submit — what actually happened

Every parse request is supposed to validate the file, store it, push the job onto an SQS queue,
and return `{ job_id, status: "processing", poll_url }` in well under a second, with a separate
Worker Lambda running the pipeline.

The API decides between those two paths on one environment variable:

```python
if settings.use_queue_worker:            # bool(WORKER_QUEUE_URL)
    enqueue_job(settings, payload)       # SQS -> Worker Lambda; returns immediately
else:
    background_tasks.add_task(...)       # in-process fallback (local-dev path)
```

`WORKER_QUEUE_URL` was never set on the production function and the Worker Lambda had not been
created, so every request took the fallback. Starlette runs `BackgroundTasks` **inside** the ASGI
cycle, so on Lambda the response is not returned until the task completes — the caller's
"async submit" blocked for the entire parse.

### The UAT run (job `01KYAF9D27HE68NVA64YGK20XR`), from our logs

All of it in the **API** log group under a single request id — proof the parse ran on the
request thread rather than on a worker:

| our clock (UTC) | event |
|---|---|
| 16:28:12.743 | `parse_request` received |
| 16:28:12.880 | `job_start` — parse begins **on the request** |
| 16:28:23.923 | `pipeline_complete` (11.0s, no OCR, confidence 0.82) |
| 16:28:24.205 | `parse.completed` POSTed to `uat-api.gighealth.com` → **401** |
| 16:28:29.911 | `parse.completed` POSTed to a second registered endpoint → 200 (took 5.7s) |
| 16:28:29.949 | `job_done` → HTTP response finally returns |

So the reported 22,336ms breaks down as the parse itself (~11s) plus webhook delivery to a
second, stale registration (~5.7s) plus transfer and bookkeeping. The webhook genuinely
preceded the submit response **on our clock**, not because of client-side delay.

### Fix

Provision the worker stack and point the API at it (`scripts/provision_worker.sh`), which
restores the intended sub-second submit. To stop this recurring, the API now **refuses to boot
in production without `WORKER_QUEUE_URL`** rather than silently degrading, and
`GET /api/v1/health` reports the live dispatch mode:

```json
{ "status": "ok", "dependencies": { "dynamodb": "ok", "s3": "ok", "worker": "queue" } }
```

`"worker": "in-process"` in any deployed environment means this defect has returned.

### Answering the client's question

> "If there's an async submit endpoint that returns the job_id immediately, we'd switch to it."

`POST /resume/parse` **is** that endpoint — no client change is needed. It returns
`job_id` + `poll_url` and never returns parsed `data` inline. Once the worker is provisioned it
behaves as documented, and the race the client is defending against disappears.

Their defensive handling (retry the lookup, finalize on arrival, reconcile job) is good practice
and worth keeping: delivery can still legitimately arrive before local bookkeeping settles, and
a delivery may arrive more than once. Keep the handler idempotent on `job_id`.

---

## 2. The UAT signature mismatch

Signing has not changed and matches the Integration Guide §6 exactly:

```
X-Signature: sha256=<hex digest of HMAC_SHA256(secret, "<timestamp>." + raw_body)>
X-Timestamp: <unix seconds>
X-Event:     parse.completed
```

The secret is **per registration**, not per company: `POST /api/v1/webhooks` mints a fresh
32-byte secret, returns it exactly once, and it is never retrievable afterwards. There is no
rotate endpoint.

The company had **two active registrations**, both subscribed to `parse.completed` and
`parse.failed`, created a day apart — and every event is delivered to **both**, each signed with
its own secret. The secret configured in UAT belongs to one registration while UAT deliveries
are signed with the other's, which fails verification 100% of the time.

Two consequences worth flagging:

- **Rejected deliveries are not retried.** A 401 is a 4xx, and our sender treats any non-5xx as
  delivered. Every UAT event since the second registration was created was dropped on our side;
  only the client's reconcile job recovered them.
- **A stale registration keeps receiving data.** `parse.completed` carries the full parsed
  record — name, email, phone, address, work history, licences. Any registration left active
  keeps receiving that until it is deleted.

### Fix

1. Delete the stale registration: `DELETE /api/v1/webhooks/{webhook_id}` (list with
   `GET /api/v1/webhooks`).
2. Re-register the UAT endpoint and store the returned `hmac_secret` immediately — that value
   is shown once and cannot be recovered.
3. Verify against the **raw** request body (not a re-serialized object) — the most common
   client-side cause of a signature mismatch is a JSON body-parser consuming the raw bytes
   before verification.

---

## 3. Checklist

**Ours**

- [ ] Worker stack provisioned; `WORKER_QUEUE_URL` set on the API function
- [ ] `GET /api/v1/health` reports `"worker": "queue"`
- [ ] Stale webhook registration deleted
- [ ] UAT re-registered and its secret handed over on a secure channel

**Client's**

- [ ] Handler idempotent on `job_id`, tolerant of an unknown `job_id` (upsert or buffer)
- [ ] Signature verified against the raw body; respond 2xx fast, process asynchronously
- [ ] No code path waits for inline `data` in the POST response

Include a `request_id` from any response when raising an issue — every log line above is keyed
by it.
