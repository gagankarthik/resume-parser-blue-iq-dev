"""
AWS Lambda entry point — Async OCR Worker Lambda.

Invoked asynchronously (InvocationType='Event') by the API Lambda.
Handler: app.handlers.worker_lambda.handler

Expected event payload:
  {
    "job_id":          "01J3K5...",
    "company_id":      "acme-corp",
    "s3_key":          "temp/01J3K5.../resume.pdf",
    "filename":        "resume.pdf",
    "file_size_bytes": 204800,
    "batch_id":        "01J3K5..."   (optional)
  }
"""

import asyncio
from typing import Any

from app.core.logging import configure_logging, get_logger
from app.workers.background import process_resume_async

configure_logging()
log = get_logger(__name__)

_REQUIRED_FIELDS = {"job_id", "company_id", "s3_key", "filename", "file_size_bytes"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for async resume OCR processing.
    Returns {"status": "ok"} on success, {"status": "error", "message": ...} on failure.
    Raising an exception here marks the Lambda invocation as failed in CloudWatch.
    """
    missing = _REQUIRED_FIELDS - set(event.keys())
    if missing:
        msg = f"Missing required event fields: {sorted(missing)}"
        log.error("worker_lambda_bad_event", missing=sorted(missing))
        raise ValueError(msg)   # marks invocation as failed in Lambda console

    log.info("worker_lambda_start", job_id=event["job_id"], batch_id=event.get("batch_id"))

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            process_resume_async(
                job_id=event["job_id"],
                company_id=event["company_id"],
                s3_key=event["s3_key"],
                filename=event["filename"],
                file_size_bytes=int(event["file_size_bytes"]),
                batch_id=event.get("batch_id"),
            )
        )
        loop.close()
    except Exception as exc:
        log.error("worker_lambda_error", job_id=event["job_id"], error=str(exc))
        raise   # re-raise so Lambda marks invocation as failed

    log.info("worker_lambda_done", job_id=event["job_id"])
    return {"status": "ok", "job_id": event["job_id"]}
