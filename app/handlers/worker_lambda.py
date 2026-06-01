"""
AWS Lambda entry point — Async OCR Worker Lambda.

Invoked asynchronously (InvocationType='Event') by the API Lambda
when a scanned PDF or image resume needs OCR processing.

Handler: app.handlers.worker_lambda.handler

Expected event payload:
  {
    "job_id":         "01J3K5...",
    "company_id":     "acme-corp",
    "s3_key":         "temp/01J3K5.../resume.pdf",
    "filename":       "resume.pdf",
    "file_size_bytes": 204800
  }
"""

import asyncio
import logging

from app.core.logging import configure_logging
from app.workers.background import process_resume_async

log = logging.getLogger(__name__)


def handler(event: dict, context) -> None:  # type: ignore[type-arg]
    """
    Lambda handler for async resume OCR processing.
    This function is fire-and-forget — return value is ignored.
    """
    configure_logging()

    required = {"job_id", "company_id", "s3_key", "filename", "file_size_bytes"}
    missing = required - set(event.keys())
    if missing:
        log.error("worker_lambda_bad_event", missing=list(missing))
        return

    log.info("worker_lambda_start", job_id=event["job_id"])

    asyncio.run(
        process_resume_async(
            job_id=event["job_id"],
            company_id=event["company_id"],
            s3_key=event["s3_key"],
            filename=event["filename"],
            file_size_bytes=int(event["file_size_bytes"]),
        )
    )
