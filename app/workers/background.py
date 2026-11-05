"""
Async resume processing handler.

Used by:
  * FastAPI BackgroundTasks (development / single-file async path)
  * Worker Lambda handler (production - one SQS message per file)

Resilience:
  * DynamoDB writes: up to 3 attempts, 1s apart, on ClientError or BotoCoreError
  * Webhook delivery: 10s deadline per attempt, 4 attempts (see webhook_sender)
  * S3 delete: 1 retry before giving up (file leaks are non-critical)
  * batch_id propagation: atomic counter update triggers batch.completed webhook
"""

import asyncio

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import public_error
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.application import job_outcome
from app.services.pipeline import PipelineInput, PipelineResult
from app.services.pipeline import run as run_pipeline
from app.storage import s3_client

log = get_logger(__name__)

_DB_RETRIES    = 3
_DB_RETRY_WAIT = 1.0   # seconds between DynamoDB attempts


async def process_resume_async(
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
    file_size_bytes: int,
    batch_id: str | None = None,
    force_textract: bool = False,
    key_hash: str = "",
    key_prefix: str = "",
    endpoint: str = "",
) -> None:
    """
    Full pipeline for one resume file.
    Never raises - all errors are caught, logged, and reflected in DynamoDB.
    """
    log.info("job_start", job_id=job_id, batch_id=batch_id)
    await _db_call(db.update_job_processing, job_id)

    result: PipelineResult | None = None
    error_code = ""
    rejected_tokens = 0
    status = "failed"

    try:
        content = s3_client.download_file(s3_key)
        result = await run_pipeline(
            PipelineInput(
                job_id=job_id,
                filename=filename,
                content=content,
                company_id=company_id,
                force_textract=force_textract,
            )
        )
        parsed = result.parsed.model_dump()
        completed_event = {
            "job_id": job_id,
            "data": parsed,
            "partial": result.partial,
            "warnings": result.warnings,
        }
        # Write first, announce second. The poll endpoint reads this row: a
        # parse.completed sent before the write lands tells the client "done" while
        # every poll still says "processing" until the TTL.
        stored = await _db_call(
            db.update_job_completed,
            job_id,
            {
                "data": parsed,
                "confidence": result.confidence.model_dump(),
                "partial": result.partial,
                "warnings": result.warnings,
            },
        )
        if not stored:
            raise RuntimeError("the parse result could not be stored")
        status = "completed"

    except Exception as exc:
        # The class name and raw text are for our logs only - they name buckets,
        # ARNs and request ids. The poll response and the webhook get the public error.
        failure = public_error(exc)
        error_code = failure.error_code.value
        rejected_tokens = getattr(exc, "tokens", 0)
        log.exception("job_failed", job_id=job_id, error_type=type(exc).__name__,
                      error_code=error_code)
        await _db_call(db.update_job_failed, job_id, failure.detail, error_code)

        await job_outcome.deliver_safely(
            company_id, "parse.failed",
            {"job_id": job_id, "error": failure.detail, "error_code": error_code},
        )

    else:
        await job_outcome.deliver_safely(company_id, "parse.completed", completed_event)

    finally:
        # Everything below is best-effort cleanup/bookkeeping. It MUST NOT raise:
        # on the SQS worker a propagated exception makes the message redeliver
        # (at-least-once), re-running the pipeline and re-emitting a byte-identical
        # parse.completed webhook + job write (duplicate output). Swallow and log
        # instead. (The worker also skips a job already in a terminal state, so a
        # redelivery after completion is a no-op - see worker_lambda._process_one.)
        _s3_delete(s3_key, job_id)

        try:
            db.write_audit_log(
                job_id=job_id,
                company_id=company_id,
                file_type=result.file_type if result else "unknown",
                file_size_bytes=file_size_bytes,
                status=status,
                duration_ms=result.duration_ms if result else 0,
                ocr_used=result.ocr_used if result else False,
                ai_tokens_used=result.ai_tokens_used if result else rejected_tokens,
                error_code=error_code,
                key_hash=key_hash,
                key_prefix=key_prefix,
                endpoint=endpoint,
            )
        except Exception:
            log.exception("audit_log_failed", job_id=job_id)

        if batch_id:
            try:
                await job_outcome.record_batch_outcome(
                    batch_id=batch_id, job_id=job_id, company_id=company_id,
                    completed=status == "completed",
                )
            except Exception:
                # The claim was taken but the counter never moved: this batch will
                # not reach its total on its own.
                log.exception("batch_bookkeeping_failed", job_id=job_id, batch_id=batch_id)

        log.info("job_done", job_id=job_id, status=status, batch_id=batch_id)


# -- Internal helpers ----------------------------------------------------------

async def _db_call(fn, *args) -> bool:
    """Run a DynamoDB write, retrying transient AWS errors. True when it landed.

    Retries both botocore families - ClientError (throttling, 5xx) and
    BotoCoreError (connection, endpoint and read-timeout errors) - and backs off
    with asyncio.sleep so the event loop keeps serving meanwhile.
    """
    for attempt in range(1, _DB_RETRIES + 1):
        try:
            fn(*args)
            return True
        except (ClientError, BotoCoreError):
            if attempt == _DB_RETRIES:
                log.exception("db_write_failed", fn=fn.__name__, attempts=attempt)
                return False
            await asyncio.sleep(_DB_RETRY_WAIT)
    return False


def _s3_delete(s3_key: str, job_id: str) -> None:
    """Delete S3 temp file; retry once on failure. Never logs the key (see s3_client)."""
    try:
        s3_client.delete_file(s3_key)
    except Exception as exc:
        log.warning("s3_delete_retry", job_id=job_id, error=str(exc))
        try:
            s3_client.delete_file(s3_key)
        except Exception:
            log.exception("s3_delete_failed", job_id=job_id)
