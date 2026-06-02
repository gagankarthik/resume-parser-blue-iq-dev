"""
Async resume processing handler.

Used by:
  • FastAPI BackgroundTasks (development / single-file async path)
  • Worker Lambda handler (production — invoked per-file)

Resilience:
  • DynamoDB writes: 3 retries with 1s backoff on ClientError
  • Webhook delivery: 15s timeout per attempt
  • S3 delete: 1 retry before giving up (file leaks are non-critical)
  • batch_id propagation: atomic counter update triggers batch.completed webhook
"""

import asyncio

from botocore.exceptions import ClientError

from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.pipeline import PipelineInput, PipelineResult, run as run_pipeline
from app.storage import s3_client
from app.workers.webhook_sender import deliver_event

log = get_logger(__name__)

_DB_RETRIES    = 3
_DB_RETRY_WAIT = 1.0   # seconds between DynamoDB retries
_WEBHOOK_TIMEOUT = 15  # seconds per webhook delivery attempt


async def process_resume_async(
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
    file_size_bytes: int,
    batch_id: str | None = None,
) -> None:
    """
    Full pipeline for one resume file.
    Never raises — all errors are caught, logged, and reflected in DynamoDB.
    """
    log.info("job_start", job_id=job_id, batch_id=batch_id)
    _db_call(db.update_job_processing, job_id)

    result: PipelineResult | None = None
    error_code = ""
    status = "failed"

    try:
        content = s3_client.download_file(s3_key)
        result = await run_pipeline(
            PipelineInput(
                job_id=job_id,
                filename=filename,
                content=content,
                company_id=company_id,
            )
        )
        status = "completed"

        _db_call(
            db.update_job_completed,
            job_id,
            {
                "data": result.parsed.model_dump(),
                "confidence": result.confidence.model_dump(),
            },
        )

        await _safe_deliver(
            company_id, "parse.completed",
            {"job_id": job_id, "data": result.parsed.model_dump()},
        )

    except Exception as exc:
        error_code = type(exc).__name__
        log.error("job_failed", job_id=job_id, error=str(exc), error_code=error_code)
        _db_call(db.update_job_failed, job_id, str(exc), error_code)

        await _safe_deliver(
            company_id, "parse.failed",
            {"job_id": job_id, "error": str(exc)},
        )

    finally:
        _s3_delete(s3_key)

        db.write_audit_log(
            job_id=job_id,
            company_id=company_id,
            file_type=result.file_type if result else "unknown",
            file_size_bytes=file_size_bytes,
            status=status,
            duration_ms=result.duration_ms if result else 0,
            ocr_used=result.ocr_used if result else False,
            ai_tokens_used=result.ai_tokens_used if result else 0,
            error_code=error_code,
        )

        if batch_id:
            batch_done = db.increment_batch_counter(
                batch_id=batch_id,
                field="completed" if status == "completed" else "failed",
            )
            if batch_done:
                batch = db.get_batch(batch_id)
                if batch:
                    await _safe_deliver(
                        company_id,
                        "batch.completed",
                        {
                            "batch_id": batch_id,
                            "total":     int(batch.get("total", 0)),
                            "completed": int(batch.get("completed", 0)),
                            "failed":    int(batch.get("failed", 0)),
                        },
                    )

        log.info("job_done", job_id=job_id, status=status, batch_id=batch_id)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _db_call(fn, *args, **kwargs) -> None:
    """Call a DynamoDB function with up to _DB_RETRIES retries on ClientError."""
    import time
    for attempt in range(_DB_RETRIES):
        try:
            fn(*args, **kwargs)
            return
        except ClientError as exc:
            if attempt == _DB_RETRIES - 1:
                log.error("db_write_failed", fn=fn.__name__, error=str(exc))
            else:
                time.sleep(_DB_RETRY_WAIT)


async def _safe_deliver(company_id: str, event: str, payload: dict) -> None:
    """Deliver webhook with timeout; swallow errors — delivery is best-effort."""
    try:
        await asyncio.wait_for(
            deliver_event(company_id, event, payload),
            timeout=_WEBHOOK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("webhook_delivery_timeout", event=event, company_id=company_id)
    except Exception as exc:
        log.warning("webhook_delivery_error", event=event, error=str(exc))


def _s3_delete(s3_key: str) -> None:
    """Delete S3 temp file; retry once on failure."""
    try:
        s3_client.delete_file(s3_key)
    except Exception as exc:
        log.warning("s3_delete_retry", key=s3_key, error=str(exc))
        try:
            s3_client.delete_file(s3_key)
        except Exception as exc2:
            log.error("s3_delete_failed", key=s3_key, error=str(exc2))
