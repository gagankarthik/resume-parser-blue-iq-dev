# Ocean Blue — Webhook Timing & Request Latency

**Audience:** Ocean Blue / GigHealth integration engineers
**Status:** Supersedes the note of 2026-07-21. That note attributed the slow submit and the
early webhook to latency in the client's request path. **That was wrong** — the cause was on
our side, and the UAT timings supplied on 2026-07-24 disproved it. This version replaces it.
**Section 4 (2026-08-17) is the current state**; §1 and §2 are the original diagnosis and are
retained because they explain how we got here.

---

## 4. Update — 2026-08-17

Defect 1 (§1) is fixed and verified in production: the worker stack is provisioned, the queue
path is live, and submit now answers in ~240ms.

### The "stuck" job was not stuck

Reported: job `01M07ZG3R9M1NQX9C1N722DH41` (`Dr James Mitchell CV`) sat at
`{"status": "pending", "data": null}` while `01M07Z8WS6TXZGD39EHBPECT52` (Zina Smith) worked.
Nothing about the file failed. It parsed in **15.2s** and the result was in storage the whole
time:

| UTC | event |
|---|---|
| 13:45:10.154 | `parse_request` (174,830 bytes) |
| 13:45:10.264 | stored to S3 |
| 13:45:10.397 | **client polls `GET /resume/job/…` → 200 `pending`** |
| 13:45:10.406 | worker sets `started_at` — 9ms *after* that poll |
| 13:45:25.499 | `completed_at`, full result written |

The client polled **once, 140ms after submit**, landing in the sub-second window before the
worker took the message off the queue. It got `pending`, and never polled again. The Zina job
is identical in every respect except that something polled it a *second* time 2m47s later and
got the data. That is the entire difference between "works" and "stuck".

Two things came out of it, one each side:

- **Ours:** the poll endpoint was leaking the internal `pending` state, contradicting both the
  submit response (`status: "processing"`) and its own API description ("Returns `processing`
  until done"). **`processing` is now the only non-terminal status a poller can ever see.** A
  single poll can no longer end a client's loop. (`pending_upload` is unchanged — it is a real
  state the caller must act on.)
- **Theirs:** a poll loop is still required. One poll is not a poll loop, whatever it returns.
  Poll every 2–3s with a ~2min ceiling, and treat any unrecognised status as non-terminal.

### The signature mismatch

The stale ngrok registration (`01KXZWX06W5ZD0QDARDZXWFN60`, created 2026-07-20) was **still
active as of today** — §2's remediation was never carried out, so it kept receiving the full
parsed record (name, email, phone, address, work history, licences) on every parse for four
weeks. **It has now been deleted.** `01KYMMD337H5V02NB08F0HJPFA` → `uat-api.gighealth.com` is
the only registration, so there is no longer any ambiguity about which secret signs UAT.

Our side of the signing contract is now covered by tests that run the real sender against a
real socket and verify the delivery with **the verifier snippet from the Integration Guide §6,
copied verbatim** (`tests/integration/test_webhook_delivery.py`). It passes. So if UAT still
rejects deliveries, the cause is one of exactly two things, and there is now a safe way to tell
them apart without either side pasting a secret anywhere:

```bash
python scripts/webhook_secret_fingerprint.py --company-id gighealth-1f4cd3   # ours
BLUEIQ_WEBHOOK_SECRET=... python scripts/webhook_secret_fingerprint.py --stdin-secret  # theirs
```

Compare the printed `sha256[:12]`. A fingerprint is not a credential — the secret cannot be
recovered from it or used to sign anything, so it is safe in a ticket.

- **Fingerprints match** → the secret is correct and the fault is in the receiver's
  verification. Overwhelmingly the most likely cause is a JSON body-parser consuming the raw
  bytes, so the receiver signs a **re-serialized** body. `HMAC(secret, ts + "." +
  json.dumps(parsed))` ≠ `HMAC(secret, ts + "." + raw_bytes)` — different separators, possibly
  different key order. Capture the raw body *before* any parser touches it. This failure mode
  is pinned by a test, so it is a demonstrated cause and not a guess.
- **Fingerprints differ** → the receiver holds a different registration's secret, or the
  registration was recreated after the value was handed over. Re-register and store the new
  `hmac_secret` immediately; it is shown once and is not retrievable through the API.

For the record, ours is `sha256[:12] = 11d4f0fda5d0`, 64 hex characters (the expected shape —
a shorter length means a truncated paste).

### We were blind, which is why this took a call to surface

`resume-parser-production-worker` **had no CloudWatch log group at all** despite running every
parse. It shares the API's execution role, whose basic-execution policy scoped
`logs:CreateLogStream`/`PutLogEvents` to `/aws/lambda/resume-parser-production-api:*` only. The
worker could create its log group but never write to it, so every `webhook_delivered
status=401`, `job_start`, and `pipeline_complete` since the worker was provisioned was
discarded. Fixed by attaching a `resume-parser-worker-logs` inline policy. Ironically, moving
the parse off the request path (the §1 fix) is what hid the 401s — they used to land in the API
log group.

A rejected delivery now also logs `webhook_rejected_not_retried` at WARNING with the status
code, so a misconfigured secret is a searchable event rather than an `info` line nobody reads.

### Also fixed: the retry ladder could not complete

The sender's ladder sleeps 2+5+10 = 17s, but the worker cancelled delivery at 15s. The final
attempt was unreachable, and because `_record_failure` ran only *after* the loop, the circuit
breaker could never open however many deliveries failed. The outer budget is now derived from
the ladder (`DELIVERY_BUDGET_SECONDS`) instead of hardcoded, and a cancelled delivery records
its failure. This only ever affected 5xx/connection retries — a 401 is terminal on the first
attempt either way.

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
restores the intended sub-second submit. To stop this recurring, a missing `WORKER_QUEUE_URL` in
production is now logged at cold start, **fails the deploy** via the smoke test, and is reported
by `GET /api/v1/health`:

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

*(state as of 2026-08-17 — see §4)*

**Ours**

- [x] Worker stack provisioned; `WORKER_QUEUE_URL` set on the API function
- [x] `GET /api/v1/health` reports `"worker": "queue"`
- [x] Stale webhook registration deleted — `01KXZWX06W5ZD0QDARDZXWFN60`, 2026-08-17
- [x] Worker granted CloudWatch write access, so delivery outcomes are visible again
- [x] Poll endpoint reports `processing`, never the internal `pending`
- [x] Signing contract covered by tests against the guide's own verifier
- [ ] **Needs deploy** — the poll-status and retry-budget fixes are merged but not yet released
- [ ] Secret fingerprints compared with GigHealth (§4) to close out the 401

**Client's**

- [ ] **Poll in a loop** — every 2–3s to a ~2min ceiling. One poll is not a poll loop; a poll
      inside the first second will legitimately see a non-terminal status.
- [ ] Treat any unrecognised status as non-terminal rather than as an end state
- [ ] Handler idempotent on `job_id`, tolerant of an unknown `job_id` (upsert or buffer)
- [ ] Signature verified against the **raw** body captured before any JSON parser runs;
      respond 2xx fast, process asynchronously
- [ ] Alert on signature rejections — a 401 is never retried, so a silent 401 is a lost event
- [ ] No code path waits for inline `data` in the POST response

Include a `request_id` from any response when raising an issue — every log line above is keyed
by it.
