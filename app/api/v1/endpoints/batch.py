"""
Batch resume parsing endpoints.

POST /api/v1/resume/batch
  Upload up to MAX_BATCH_SIZE resumes in one request.
  Files are validated (empty + size + magic bytes) immediately.
  Valid files -> S3 + async processing (one SQS message per file, or
  BackgroundTasks locally).
  Invalid files -> reported in skipped_files, not counted in total.
  Always returns immediately - results arrive via webhook + polling.

GET /api/v1/resume/batch/{batch_id}
  Poll overall progress: total / completed / failed / processing counts.
"""

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from ulid import ULID

from app.api.dependencies import get_api_key_record
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.file_validator import check_upload, clamp_filename
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import BatchJob, BatchSkipped, BatchStatusResponse, BatchSubmitResponse
from app.services.application import job_outcome, resume_service
from app.storage import s3_client
from app.workers.batch_processor import process_batch_locally
from app.workers.dispatch import AsyncJobPayload, enqueue_jobs

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
    files: list[UploadFile] = File(..., description="Resume files - PDF, DOCX, PNG, JPG, TIFF"),
    record: dict = Depends(get_api_key_record),
) -> BatchSubmitResponse:
    settings = get_settings()
    company_id: str = record["company_id"]
    batch_id = str(ULID())

    if len(files) > settings.max_batch_size:
        raise api_error(
            422, ErrorCode.BATCH_TOO_LARGE,
            f"Too many files: {len(files)}. Maximum is {settings.max_batch_size} per batch.",
        )

    skipped: list[BatchSkipped] = []
    valid: list[tuple[str, bytes]] = []

    # Validate every file first - cheap, in-process, no I/O.
    for file in files:
        filename = clamp_filename(file.filename or "upload")
        content = await file.read()
        rejection = check_upload(filename, content, settings.max_file_size_bytes)
        if rejection is not None:
            skipped.append(BatchSkipped(
                filename=filename, reason=rejection.detail, error_code=rejection.error_code.value,
            ))
            continue
        valid.append((filename, content))

    def _stage(filename: str, content: bytes) -> AsyncJobPayload | None:
        """Open one job row, then put its file in S3. Blocking; runs off the loop."""
        job_id = str(ULID())
        try:
            db.create_job(job_id, company_id, batch_id=batch_id)
            s3_key = s3_client.upload_temp_file(job_id, filename, content)
        except Exception:
            # One bad file must not sink the whole batch - report it as skipped and
            # let the rest through.
            log.exception("batch_stage_failed", batch_id=batch_id, job_id=job_id)
            return None
        return resume_service.build_async_payload(
            job_id=job_id, company_id=company_id, s3_key=s3_key, filename=filename,
            file_size_bytes=len(content), force_textract=False, record=record,
            endpoint="POST /resume/batch", batch_id=batch_id,
        )

    # Stage the accepted files CONCURRENTLY. Staging is two blocking AWS calls per
    # file (DynamoDB put + S3 put); running them in a sequential loop made submit
    # time grow linearly with the batch, so a large batch could burn the caller's
    # gateway timeout before the 202 was ever returned. The worker fan-out below was
    # already parallel for exactly this reason - the uploads were not.
    staged = await asyncio.gather(
        *(asyncio.to_thread(_stage, filename, content) for filename, content in valid)
    )

    accepted_jobs: list[AsyncJobPayload] = []
    for (filename, _), job in zip(valid, staged, strict=True):
        if job is None:
            skipped.append(BatchSkipped(
                filename=filename,
                reason="Could not be queued for processing - please retry this file.",
                error_code=ErrorCode.WORKER_DISPATCH_FAILED.value,
            ))
        else:
            accepted_jobs.append(job)

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

    if settings.use_queue_worker:
        # Enqueue the whole batch onto SQS in chunks of 10 (SendMessageBatch). This
        # is a few blocking boto3 calls even for a 200-file batch, so run it off the
        # event loop to keep the 202 well under the gateway timeout.
        failed_ids = await asyncio.to_thread(enqueue_jobs, settings, accepted_jobs)
        for job in accepted_jobs:
            if job["job_id"] in failed_ids:
                await _fail_unqueued(job, batch_id)
    else:
        background_tasks.add_task(process_batch_locally, batch_id, accepted_jobs)

    return BatchSubmitResponse(
        batch_id=batch_id,
        total=total,
        skipped=len(skipped),
        skipped_files=skipped,
        jobs=[BatchJob(job_id=j["job_id"], filename=j["filename"]) for j in accepted_jobs],
        job_ids=[j["job_id"] for j in accepted_jobs],
        status="processing",
        poll_url=f"/api/v1/resume/batch/{batch_id}",
    )


async def _fail_unqueued(job: AsyncJobPayload, batch_id: str) -> None:
    """Fail a batch job that never reached the queue and count it on the batch.

    Only the job row used to be failed, so the batch kept counting it as
    `processing` until its TTL and batch.completed never fired.
    """
    try:
        await job_outcome.fail_job(
            job_id=job["job_id"], company_id=job["company_id"],
            error=job_outcome.DISPATCH_FAILED, batch_id=batch_id,
        )
    except Exception:
        log.exception("batch_unqueued_job_not_recorded", batch_id=batch_id, job_id=job["job_id"])
    # No worker will ever read the file - don't leave resume bytes behind.
    await asyncio.to_thread(s3_client.delete_file, job["s3_key"])


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
    record: dict = Depends(get_api_key_record),
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
