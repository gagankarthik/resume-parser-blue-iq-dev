"""
FastAPI BackgroundTask handler for async (OCR) resume processing.

Flow:
  1. Mark job as "processing" in DynamoDB
  2. Run the full pipeline
  3. Store result in DynamoDB (TTL 1h — no permanent storage)
  4. Delete temp file from S3
  5. Fire webhooks
  6. Write audit log
"""

import json

from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import ParsedResumeAI, ConfidenceScores
from app.services.pipeline import PipelineInput, PipelineResult, run as run_pipeline
from app.storage import s3_client
from app.workers.webhook_sender import deliver_event

log = get_logger(__name__)


async def process_resume_async(
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
    file_size_bytes: int,
) -> None:
    """Runs inside FastAPI BackgroundTasks — no return value, must not raise."""
    log.info("background_job_start", job_id=job_id)
    db.update_job_processing(job_id)

    content: bytes | None = None
    result: PipelineResult | None = None
    error_code = ""
    status = "failed"

    try:
        content = s3_client.download_file(s3_key)

        pipeline_input = PipelineInput(
            job_id=job_id,
            filename=filename,
            content=content,
            company_id=company_id,
        )
        result = await run_pipeline(pipeline_input)
        status = "completed"

        # Store result in DynamoDB (TTL managed by create_job)
        db.update_job_completed(
            job_id,
            {
                "data": result.parsed.model_dump(),
                "confidence": result.confidence.model_dump(),
            },
        )

        await deliver_event(
            company_id,
            "parse.completed",
            {"job_id": job_id, "data": result.parsed.model_dump()},
        )

    except Exception as exc:
        error_code = type(exc).__name__
        log.error("background_job_failed", job_id=job_id, error=str(exc), error_code=error_code)
        db.update_job_failed(job_id, str(exc), error_code)

        await deliver_event(
            company_id,
            "parse.failed",
            {"job_id": job_id, "error": str(exc)},
        )

    finally:
        # Always delete the temp file — even on failure
        s3_client.delete_file(s3_key)

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

        log.info("background_job_done", job_id=job_id, status=status)
