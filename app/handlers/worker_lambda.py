"""
Async OCR / multi-agent worker - drains the parse-jobs SQS queue.

Deployed as its own Lambda function (`app.handlers.worker_lambda.handler`), sized
for the heavy path (OCR/Textract + the 10-way per-role LLM fan-out) independently
of the thin API function. Lambda's SQS event-source mapping delivers a batch of
messages; this handler processes each and, via `ReportBatchItemFailures`, tells SQS
exactly which messages to redeliver - the rest are deleted. A message that keeps
failing past the queue's maxReceiveCount lands in the dead-letter queue as a
visible, alertable event instead of a job that silently never finishes.

Each SQS message body is the async-job payload:
  {
    "job_id":          "01J3K5...",
    "company_id":      "acme-corp",
    "s3_key":          "temp/01J3K5.../resume.pdf",
    "filename":        "resume.pdf",
    "file_size_bytes": 204800,
    "batch_id":        "01J3K5..."   (optional)
    "force_textract":  false          (optional - skip Tesseract, use Textract)
    "key_hash":        "..."          (optional - attribute usage to the API key)
    "key_prefix":      "rp_live_ab"   (optional - display prefix for the key)
  }

The unified entry point (`app.handlers.lambda_handler`) also routes any non-HTTP
event here, so a plain job dict (no SQS envelope) is still accepted - used by local
tooling and tests.
"""

import asyncio
import json
from typing import Any

from app.core.logging import configure_logging, get_logger
from app.db import dynamodb as db
from app.workers.background import process_resume_async

configure_logging()
log = get_logger(__name__)

_REQUIRED_FIELDS = {"job_id", "company_id", "s3_key", "filename", "file_size_bytes"}

# A job already in one of these states has been fully processed. SQS is
# at-least-once: a redelivery (e.g. after a post-completion timeout) would
# otherwise re-run the whole pipeline and re-emit a duplicate webhook.
_TERMINAL_STATUSES = {"completed", "failed", "partial"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any] | None:
    """Route an SQS batch event to the worker; fall back to a single job dict.

    For SQS events returns the partial-batch-failure report
    (`{"batchItemFailures": [...]}`). For a direct job dict returns
    `{"status": "ok", ...}` and raises on a bad payload (marking the invocation
    failed in the Lambda console), preserving the pre-SQS direct-invoke contract.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        if _is_sqs_event(event):
            return loop.run_until_complete(_handle_sqs_batch(event))
        return loop.run_until_complete(_handle_direct(event))
    finally:
        loop.close()
        # Install a FRESH, open loop. The unified handler may serve HTTP (Mangum)
        # from this same warm container; leaving a closed loop installed would
        # poison every later HTTP request with "RuntimeError: Event loop is closed".
        asyncio.set_event_loop(asyncio.new_event_loop())


def _is_sqs_event(event: Any) -> bool:
    """True for an SQS event-source-mapping batch (has SQS-sourced Records)."""
    if not isinstance(event, dict):
        return False
    records = event.get("Records")
    return (
        isinstance(records, list)
        and len(records) > 0
        and isinstance(records[0], dict)
        and records[0].get("eventSource") == "aws:sqs"
    )


async def _handle_sqs_batch(event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Process each SQS record; report the ones to redeliver.

    A record whose body can't be parsed/validated, or whose processing raises
    unexpectedly, is reported as a batch-item failure so SQS redelivers it (and
    eventually DLQs a persistent poison message). Records that succeed - including
    jobs that legitimately end in `failed` - are acknowledged (deleted).
    """
    failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            payload = json.loads(record.get("body") or "{}")
            _require_fields(payload)
            await _process_one(payload)
        except Exception as exc:
            log.error("worker_record_failed",
                      message_id=message_id, error=str(exc))
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


async def _handle_direct(event: dict[str, Any]) -> dict[str, Any]:
    """Process a single job dict (no SQS envelope). Raises on a bad payload."""
    _require_fields(event)
    log.info("worker_direct_start", job_id=event["job_id"], batch_id=event.get("batch_id"))
    try:
        await _process_one(event)
    except Exception as exc:
        log.error("worker_direct_error", job_id=event["job_id"], error=str(exc))
        raise  # re-raise so Lambda marks the invocation as failed
    return {"status": "ok", "job_id": event["job_id"]}


def _require_fields(payload: dict[str, Any]) -> None:
    missing = _REQUIRED_FIELDS - set(payload.keys())
    if missing:
        raise ValueError(f"Missing required job fields: {sorted(missing)}")


async def _process_one(payload: dict[str, Any]) -> None:
    """Run the full pipeline for one job, skipping ones already finished.

    `process_resume_async` handles its own errors (it marks the job failed and
    never raises), so a normal call always resolves - only infrastructure faults
    (timeout/OOM) or the idempotency read below surface as exceptions here.
    """
    job_id = payload["job_id"]

    existing = db.get_job(job_id)
    if existing and existing.get("status") in _TERMINAL_STATUSES:
        log.info("worker_job_already_done", job_id=job_id, status=existing["status"])
        return

    await process_resume_async(
        job_id=job_id,
        company_id=payload["company_id"],
        s3_key=payload["s3_key"],
        filename=payload["filename"],
        file_size_bytes=int(payload["file_size_bytes"]),
        batch_id=payload.get("batch_id"),
        force_textract=bool(payload.get("force_textract", False)),
        key_hash=payload.get("key_hash", ""),
        key_prefix=payload.get("key_prefix", ""),
    )
    log.info("worker_job_done", job_id=job_id, batch_id=payload.get("batch_id"))
