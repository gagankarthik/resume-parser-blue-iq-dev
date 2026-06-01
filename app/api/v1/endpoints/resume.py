"""
Resume parsing endpoints.

POST /api/v1/resume/parse
  - Validates file type (extension + magic bytes) and size
  - Digital PDF / DOCX  → synchronous pipeline → result returned immediately
  - Scanned PDF / Image → stored in S3 → async processing:
      production  : Worker Lambda invoked asynchronously (InvocationType=Event)
      development : FastAPI BackgroundTasks

GET /api/v1/resume/job/{job_id}
  - Polls async job status from DynamoDB
"""

import json

import boto3
from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from python_ulid import ULID

from app.api.dependencies import enforce_rate_limit
from app.core.config import get_settings
from app.core.exceptions import (
    UnsupportedFileTypeError,
    http_404,
    http_413,
    http_415,
)
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import (
    ConfidenceScores,
    JobStatusResponse,
    ParsedResumeAI,
    ParseResponse,
)
from app.services.extraction.classifier import ExtractionStrategy, classify
from app.services.pipeline import PipelineInput, run as run_pipeline
from app.storage import s3_client
from app.workers.background import process_resume_async

router = APIRouter()
log = get_logger(__name__)


def _invoke_worker_lambda(settings, payload: dict) -> None:
    """Fire-and-forget Lambda invocation for the OCR worker."""
    client = boto3.client("lambda", region_name=settings.aws_region)
    client.invoke(
        FunctionName=settings.worker_lambda_function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )
    log.info("worker_lambda_invoked", job_id=payload["job_id"])


@router.post(
    "/resume/parse",
    response_model=ParseResponse,
    summary="Parse a resume",
    description=(
        "Upload a resume (PDF, DOCX, or image). "
        "Digital PDFs and DOCX files are processed synchronously and return the result immediately. "
        "Scanned PDFs and images are processed asynchronously — a `job_id` is returned "
        "and results are delivered via webhook and/or the polling endpoint."
    ),
    tags=["Resume"],
)
async def parse_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Resume file: PDF, DOCX, PNG, JPG, or TIFF"),
    record: dict = Depends(enforce_rate_limit),
) -> ParseResponse:
    settings = get_settings()
    company_id: str = record["company_id"]
    job_id = str(ULID())

    content = await file.read()

    if len(content) > settings.max_file_size_bytes:
        raise http_413(
            f"File size {len(content) // 1024} KB exceeds the {settings.max_file_size_mb} MB limit"
        )

    filename = file.filename or "upload"
    try:
        strategy, needs_async = classify(filename, content)
    except UnsupportedFileTypeError as exc:
        raise http_415(str(exc))

    log.info(
        "parse_request",
        job_id=job_id,
        company_id=company_id,
        strategy=strategy,
        needs_async=needs_async,
        size_bytes=len(content),
        filename=filename,
    )

    if not needs_async:
        # ── Synchronous path: digital PDF / DOCX ──────────────────────────────
        result = await run_pipeline(
            PipelineInput(
                job_id=job_id,
                filename=filename,
                content=content,
                company_id=company_id,
            )
        )

        db.write_audit_log(
            job_id=job_id,
            company_id=company_id,
            file_type=result.file_type,
            file_size_bytes=len(content),
            status="completed",
            duration_ms=result.duration_ms,
            ocr_used=result.ocr_used,
            ai_tokens_used=result.ai_tokens_used,
        )

        return ParseResponse(
            job_id=job_id,
            status="completed",
            data=result.parsed,
            confidence=result.confidence,
        )

    # ── Async path: scanned PDF / image ───────────────────────────────────────
    s3_key = s3_client.upload_temp_file(job_id, filename, content)
    db.create_job(job_id, company_id)

    worker_payload = {
        "job_id": job_id,
        "company_id": company_id,
        "s3_key": s3_key,
        "filename": filename,
        "file_size_bytes": len(content),
    }

    if settings.use_lambda_worker:
        _invoke_worker_lambda(settings, worker_payload)
    else:
        background_tasks.add_task(process_resume_async, **worker_payload)

    return ParseResponse(
        job_id=job_id,
        status="processing",
        poll_url=f"/api/v1/resume/job/{job_id}",
    )


@router.get(
    "/resume/job/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll async job status",
    description=(
        "Check the status of an asynchronous parsing job. "
        "Returns `processing` until complete, then `completed` with parsed data, "
        "or `failed` with an error description. "
        "Results are available for 1 hour after completion."
    ),
    tags=["Resume"],
)
async def get_job_status(
    job_id: str,
    record: dict = Depends(enforce_rate_limit),
) -> JobStatusResponse:
    company_id: str = record["company_id"]
    job = db.get_job(job_id)

    if not job or job.get("company_id") != company_id:
        raise http_404("Job not found or does not belong to this account")

    status = job["status"]
    raw_result = job.get("result")

    parsed_data: ParsedResumeAI | None = None
    confidence: ConfidenceScores | None = None

    if raw_result and status == "completed":
        parsed_data = ParsedResumeAI.model_validate(raw_result.get("data", {}))
        confidence = ConfidenceScores.model_validate(raw_result.get("confidence", {}))

    return JobStatusResponse(
        job_id=job_id,
        status=status,
        data=parsed_data,
        confidence=confidence,
        error=job.get("error"),
    )
