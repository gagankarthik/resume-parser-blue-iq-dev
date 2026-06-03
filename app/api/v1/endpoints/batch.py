"""
Batch resume parsing endpoints.

POST /api/v1/resume/batch
  Upload up to MAX_BATCH_SIZE resumes in one request.
  Files are validated (size + magic bytes) immediately.
  Valid files → S3 + async processing (Lambda invoke or BackgroundTasks).
  Invalid files → reported in skipped_files, not counted in total.
  Always returns immediately — results arrive via webhook + polling.

GET /api/v1/resume/batch/{batch_id}
  Poll overall progress: total / completed / failed / processing counts.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from ulid import ULID

from app.api.dependencies import enforce_rate_limit
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.exceptions import UnsupportedFileTypeError
from app.core.file_validator import validate_file
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import BatchSkipped, BatchStatusResponse, BatchSubmitResponse
from app.storage import s3_client
from app.workers.batch_processor import process_batch_locally
from app.workers.dispatch import invoke_worker

router = APIRouter()
log = get_logger(__name__)


@router.post(
    "/resume/batch",
    response_model=BatchSubmitResponse,
    status_code=202,
    summary="Batch parse resumes",
    description=(
        "Upload up to `MAX_BATCH_SIZE` resume files in one request. "
        "Each file is validated immediately; invalid files are listed in `skipped_files`. "
        "Valid files are queued for async processing and results are delivered "
        "per-file via the `parse.completed` webhook and the single-job polling endpoint. "
        "A `batch.completed` webhook fires once **all** files have finished. "
        "Poll overall progress with GET `/api/v1/resume/batch/{batch_id}`."
    ),
    tags=["Batch"],
)
async def batch_parse(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="Resume files — PDF, DOCX, PNG, JPG, TIFF"),
    record: dict = Depends(enforce_rate_limit),
) -> BatchSubmitResponse:
    settings = get_settings()
    company_id: str = record["company_id"]
    batch_id = str(ULID())

    if len(files) > settings.max_batch_size:
        raise api_error(
            422, ErrorCode.BATCH_TOO_LARGE,
            f"Too many files: {len(files)}. Maximum is {settings.max_batch_size} per batch.",
        )

    accepted_jobs: list[dict] = []
    skipped: list[BatchSkipped] = []

    for file in files:
        filename = file.filename or "upload"
        content = await file.read()

        # Per-file size check
        if len(content) > settings.max_file_size_bytes:
            skipped.append(BatchSkipped(
                filename=filename,
                reason=f"File exceeds {settings.max_file_size_mb} MB limit",
            ))
            continue

        # Per-file magic bytes + extension check
        try:
            validate_file(filename, content)
        except UnsupportedFileTypeError as exc:
            skipped.append(BatchSkipped(filename=filename, reason=str(exc)))
            continue

        job_id = str(ULID())
        s3_key = s3_client.upload_temp_file(job_id, filename, content)
        db.create_job(job_id, company_id, batch_id=batch_id)

        accepted_jobs.append({
            "job_id": job_id,
            "company_id": company_id,
            "s3_key": s3_key,
            "filename": filename,
            "file_size_bytes": len(content),
            "batch_id": batch_id,
        })

    total = len(accepted_jobs)

    if total == 0:
        raise api_error(422, ErrorCode.EMPTY_BATCH,
                        "No valid files in batch. Check skipped_files for reasons.")

    db.create_batch(batch_id, company_id, [j["job_id"] for j in accepted_jobs], total)

    log.info(
        "batch_submitted",
        batch_id=batch_id,
        company_id=company_id,
        total=total,
        skipped=len(skipped),
    )

    if settings.use_lambda_worker:
        for job in accepted_jobs:
            invoke_worker(settings, job)
    else:
        background_tasks.add_task(process_batch_locally, batch_id, accepted_jobs)

    return BatchSubmitResponse(
        batch_id=batch_id,
        total=total,
        skipped=len(skipped),
        skipped_files=skipped,
        job_ids=[j["job_id"] for j in accepted_jobs],
        status="processing",
        poll_url=f"/api/v1/resume/batch/{batch_id}",
    )


@router.get(
    "/resume/batch/{batch_id}",
    response_model=BatchStatusResponse,
    summary="Poll batch status",
    description=(
        "Returns overall progress for a batch: how many files are completed, "
        "failed, or still processing. "
        "Batch records are retained for 24 hours after submission."
    ),
    tags=["Batch"],
)
async def get_batch_status(
    batch_id: str,
    record: dict = Depends(enforce_rate_limit),
) -> BatchStatusResponse:
    company_id: str = record["company_id"]
    batch = db.get_batch(batch_id)

    if not batch or batch.get("company_id") != company_id:
        raise api_error(404, ErrorCode.BATCH_NOT_FOUND,
                        "Batch not found or does not belong to this account")

    total     = int(batch.get("total", 0))
    completed = int(batch.get("completed", 0))
    failed    = int(batch.get("failed", 0))

    return BatchStatusResponse(
        batch_id=batch_id,
        status=batch.get("status", "processing"),
        total=total,
        completed=completed,
        failed=failed,
        processing=max(total - completed - failed, 0),
        created_at=batch.get("created_at", ""),
        completed_at=batch.get("completed_at"),
    )
