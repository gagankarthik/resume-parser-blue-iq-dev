"""
Resume parsing endpoints.

POST /api/v1/resume/parse
  Single file parse. Digital PDF/DOCX → synchronous (returns result immediately).
  Scanned PDF/image → asynchronous (returns job_id; use webhook or polling).

GET /api/v1/resume/job/{job_id}
  Poll async job status.

POST /api/v1/resume/{job_id}/retry
  Re-parse a resume when the original result was unsatisfactory.
  Client re-uploads the file; a new job_id is created linked to the original.
  Up to MAX_RETRY_COUNT retries per original job.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from ulid import ULID

from app.api.dependencies import get_api_key_record
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.exceptions import UnsupportedFileTypeError
from app.core.file_validator import validate_file
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import (
    ConfidenceScores,
    JobStatusResponse,
    ParsedResumeAI,
    ParseResponse,
    RetryResponse,
)
from app.services.extraction.classifier import classify
from app.services.pipeline import PipelineInput
from app.services.pipeline import run as run_pipeline
from app.storage import s3_client
from app.workers.background import process_resume_async
from app.workers.dispatch import invoke_worker

router = APIRouter()
log = get_logger(__name__)


async def _dispatch_async(
    settings,
    background_tasks: BackgroundTasks,
    payload: dict,
) -> None:
    if settings.use_lambda_worker:
        invoke_worker(settings, payload)
    else:
        background_tasks.add_task(process_resume_async, **payload)


async def _validate_file(file: UploadFile, settings) -> tuple[bytes, str]:
    """Read, size-check, and magic-byte validate the uploaded file."""
    content  = await file.read()
    filename = file.filename or "upload"

    if len(content) > settings.max_file_size_bytes:
        raise api_error(
            413, ErrorCode.FILE_TOO_LARGE,
            f"File size {len(content) // 1024} KB exceeds the {settings.max_file_size_mb} MB limit",
        )
    try:
        validate_file(filename, content)
    except UnsupportedFileTypeError as exc:
        raise api_error(415, ErrorCode.UNSUPPORTED_FILE_TYPE, str(exc))

    return content, filename


# ── Single-file parse ─────────────────────────────────────────────────────────

@router.post(
    "/resume/parse",
    response_model=ParseResponse,
    summary="Parse a resume",
    description=(
        "Upload a single resume (PDF, DOCX, PNG, JPG, TIFF). "
        "Digital PDFs and DOCX files are processed **synchronously** and the parsed JSON "
        "is returned immediately. "
        "Scanned PDFs and images require OCR and are processed **asynchronously** — "
        "a `job_id` is returned and results are delivered via webhook and the polling endpoint."
    ),
    tags=["Resume"],
)
async def parse_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Resume file: PDF, DOCX, PNG, JPG, or TIFF"),
    record: dict = Depends(get_api_key_record),
) -> ParseResponse:
    settings   = get_settings()
    company_id = record["company_id"]
    job_id     = str(ULID())

    content, filename = await _validate_file(file, settings)
    strategy, needs_async = classify(filename, content)

    log.info("parse_request", job_id=job_id, company_id=company_id,
             strategy=strategy, needs_async=needs_async, size_bytes=len(content))

    if not needs_async:
        result = await run_pipeline(PipelineInput(
            job_id=job_id, filename=filename, content=content, company_id=company_id,
        ))
        db.write_audit_log(
            job_id=job_id, company_id=company_id,
            file_type=result.file_type, file_size_bytes=len(content),
            status="completed", duration_ms=result.duration_ms,
            ocr_used=result.ocr_used, ai_tokens_used=result.ai_tokens_used,
        )
        return ParseResponse(job_id=job_id, status="completed",
                             data=result.parsed, confidence=result.confidence)

    s3_key = s3_client.upload_temp_file(job_id, filename, content)
    db.create_job(job_id, company_id)
    await _dispatch_async(settings, background_tasks, {
        "job_id": job_id, "company_id": company_id, "s3_key": s3_key,
        "filename": filename, "file_size_bytes": len(content),
    })
    return ParseResponse(job_id=job_id, status="processing",
                         poll_url=f"/api/v1/resume/job/{job_id}")


# ── Job status polling ────────────────────────────────────────────────────────

@router.get(
    "/resume/job/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll async job status",
    description=(
        "Check the status of an asynchronous parse job. "
        "Returns `processing` until done, then `completed` with parsed data "
        "or `failed` with an error description. "
        "Results are retained for 1 hour after completion."
    ),
    tags=["Resume"],
)
async def get_job_status(
    job_id: str,
    record: dict = Depends(get_api_key_record),
) -> JobStatusResponse:
    company_id = record["company_id"]
    job        = db.get_job(job_id)

    if not job or job.get("company_id") != company_id:
        raise api_error(404, ErrorCode.JOB_NOT_FOUND, "Job not found")

    status     = job["status"]
    raw_result = job.get("result")
    parsed_data: ParsedResumeAI | None   = None
    confidence:  ConfidenceScores | None = None

    if raw_result and status == "completed":
        parsed_data = ParsedResumeAI.model_validate(raw_result.get("data", {}))
        confidence  = ConfidenceScores.model_validate(raw_result.get("confidence", {}))

    return JobStatusResponse(
        job_id=job_id, status=status, data=parsed_data,
        confidence=confidence, error=job.get("error"),
    )


# ── Retry ─────────────────────────────────────────────────────────────────────

@router.post(
    "/resume/{job_id}/retry",
    response_model=RetryResponse,
    summary="Retry parsing a resume",
    description=(
        "Re-parse a resume when the original result was unsatisfactory. "
        "Re-upload the same file — the parser will re-run the full extraction "
        "and AI pipeline. A new `job_id` is created and linked to the original. "
        "Maximum retries per job: `MAX_RETRY_COUNT` (default 3). "
        "Retries count against your rate limit."
    ),
    tags=["Resume"],
)
async def retry_parse(
    job_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The same resume file to re-parse"),
    record: dict = Depends(get_api_key_record),
) -> RetryResponse:
    settings   = get_settings()
    company_id = record["company_id"]

    # Verify original job belongs to this company
    original = db.get_job(job_id)
    if not original or original.get("company_id") != company_id:
        raise api_error(404, ErrorCode.JOB_NOT_FOUND,
                        "Original job not found or does not belong to this account")

    # Enforce retry limit
    retry_count = int(original.get("retry_count", 0)) + 1
    if retry_count > settings.max_retry_count:
        raise api_error(
            422, ErrorCode.RETRY_LIMIT_REACHED,
            f"Maximum {settings.max_retry_count} retries allowed per job",
        )

    content, filename = await _validate_file(file, settings)
    strategy, needs_async = classify(filename, content)
    new_job_id = str(ULID())

    log.info("retry_request", original_job_id=job_id, new_job_id=new_job_id,
             company_id=company_id, retry_count=retry_count)

    # Create retry job linked to original
    db.create_job(new_job_id, company_id, retried_from=job_id, retry_count=retry_count)

    if not needs_async:
        result = await run_pipeline(PipelineInput(
            job_id=new_job_id, filename=filename, content=content, company_id=company_id,
        ))
        db.write_audit_log(
            job_id=new_job_id, company_id=company_id,
            file_type=result.file_type, file_size_bytes=len(content),
            status="completed", duration_ms=result.duration_ms,
            ocr_used=result.ocr_used, ai_tokens_used=result.ai_tokens_used,
        )
        db.update_job_completed(new_job_id, {
            "data": result.parsed.model_dump(),
            "confidence": result.confidence.model_dump(),
        })
        return RetryResponse(
            job_id=new_job_id, original_job_id=job_id, retry_count=retry_count,
            status="completed", data=result.parsed, confidence=result.confidence,
        )

    s3_key = s3_client.upload_temp_file(new_job_id, filename, content)
    await _dispatch_async(settings, background_tasks, {
        "job_id": new_job_id, "company_id": company_id, "s3_key": s3_key,
        "filename": filename, "file_size_bytes": len(content),
    })
    return RetryResponse(
        job_id=new_job_id, original_job_id=job_id, retry_count=retry_count,
        status="processing", poll_url=f"/api/v1/resume/job/{new_job_id}",
    )
