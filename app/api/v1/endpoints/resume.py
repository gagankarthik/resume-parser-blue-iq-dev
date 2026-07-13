"""
Resume parsing endpoints.

POST /api/v1/resume/parse
  Single file parse. Digital PDF/DOCX -> synchronous (returns result immediately).
  Scanned PDF/image -> asynchronous (returns job_id; use webhook or polling).

GET /api/v1/resume/job/{job_id}
  Poll async job status.

POST /api/v1/resume/{job_id}/retry
  Re-parse a resume when the original result was unsatisfactory.
  Client re-uploads the file; a new job_id is created linked to the original.
  Up to MAX_RETRY_COUNT retries per original job.

POST /api/v1/resume/{job_id}/feedback
  Submit the original parser JSON + the user-corrected JSON after review.
  Stored for model improvement. Accepted asynchronously (HTTP 202).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from ulid import ULID

from app.api.dependencies import get_api_key_record
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.exceptions import UnsupportedFileTypeError
from app.core.file_validator import is_supported_extension, validate_file
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.models.schemas import (
    ConfidenceScores,
    FeedbackRequest,
    FeedbackResponse,
    JobStatusResponse,
    ParsedResumeAI,
    ParseResponse,
    ParseUploadedRequest,
    RetryResponse,
    SkillsValidation,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.application import resume_service
from app.services.extraction.classifier import classify
from app.services.feedback import diff_fields
from app.services.normalization.skills_validator import validate_skills
from app.storage import s3_client

router = APIRouter()
log = get_logger(__name__)


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


# -- Single-file parse ---------------------------------------------------------

@router.post(
    "/resume/parse",
    response_model=ParseResponse,
    summary="Parse a resume",
    description=(
        "Upload a single resume (PDF, DOCX, RTF, PNG, JPG, TIFF). "
        "Digital PDFs, DOCX, and RTF files are processed **synchronously** and the parsed JSON "
        "is returned immediately. "
        "Scanned PDFs and images require OCR and are processed **asynchronously** - "
        "a `job_id` is returned and results are delivered via webhook and the polling endpoint."
    ),
    tags=["Resume"],
)
async def parse_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Resume file: PDF, DOCX, RTF, PNG, JPG, or TIFF"),
    force_textract: bool = Form(
        False,
        description="Skip Tesseract and use AWS Textract directly for any OCR this "
                    "file needs (scanned PDFs/images, or a digital PDF with a broken "
                    "text layer). Higher accuracy on hard scans, higher cost.",
    ),
    async_only: bool = Form(
        False,
        description="Never parse synchronously: return a `job_id` + `poll_url` "
                    "immediately and run the full parse on the async worker. Use this "
                    "when your own gateway cannot hold a request open long enough for "
                    "a complete parse (a proxy or serverless host with a short request "
                    "timeout) - blocking there would cost you a 504 with no data. "
                    "Costs one poll round-trip; never returns a partial.",
    ),
    record: dict = Depends(get_api_key_record),
) -> ParseResponse:
    settings   = get_settings()
    company_id = record["company_id"]
    job_id     = str(ULID())

    content, filename = await _validate_file(file, settings)
    strategy, needs_async = classify(filename, content)
    needs_async = needs_async or async_only

    log.info("parse_request", job_id=job_id, company_id=company_id,
             strategy=strategy, needs_async=needs_async, size_bytes=len(content),
             force_textract=force_textract, async_only=async_only)

    # Fast synchronous PROBE for digital files. If it parses cleanly inside the
    # gateway budget, return the complete JSON inline. If it can't finish (a dense
    # resume that would otherwise degrade to a partial), PROMOTE it to the async
    # worker - which runs the full multi-agent parse with no gateway ceiling - and
    # hand back a poll URL, so the caller always ends up with a COMPLETE record and
    # never a partial.
    sync_result = None
    if not needs_async:
        sync_result = await resume_service.run_parse(
            job_id=job_id, filename=filename, content=content,
            company_id=company_id, force_textract=force_textract, sync_probe=True,
        )
        if not sync_result.partial:
            resume_service.audit_parse(
                job_id=job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record,
            )
            return ParseResponse(job_id=job_id,
                                 status=resume_service.terminal_status(sync_result),
                                 data=sync_result.parsed, confidence=sync_result.confidence,
                                 skills_validation=validate_skills(sync_result.parsed),
                                 partial=False, warnings=sync_result.warnings)
        log.info("sync_partial_promoted_to_async", job_id=job_id)

    # Async: originally needed (scanned files) OR a sync partial promoted to be
    # completed end-to-end on the full-budget worker.
    try:
        s3_key = s3_client.upload_temp_file(job_id, filename, content)
        db.create_job(job_id, company_id)
        await resume_service.dispatch_async(settings, background_tasks,
            resume_service.build_async_payload(
                job_id=job_id, company_id=company_id, s3_key=s3_key, filename=filename,
                file_size_bytes=len(content), force_textract=force_textract, record=record,
            ))
    except Exception:
        # Couldn't hand off to the worker. If we at least have a probe result,
        # return it (flagged partial) rather than failing the whole request.
        log.exception("async_dispatch_failed", job_id=job_id)
        if sync_result is not None:
            resume_service.audit_parse(
                job_id=job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record, status="partial",
            )
            return ParseResponse(job_id=job_id,
                                 status=resume_service.terminal_status(sync_result),
                                 data=sync_result.parsed, confidence=sync_result.confidence,
                                 skills_validation=validate_skills(sync_result.parsed),
                                 partial=sync_result.partial, warnings=sync_result.warnings)
        raise
    return ParseResponse(job_id=job_id, status="processing",
                         poll_url=f"/api/v1/resume/job/{job_id}")


# -- Large-file upload (presigned, two-step) -----------------------------------

@router.post(
    "/resume/upload-url",
    response_model=UploadUrlResponse,
    summary="Request a direct-upload URL (large files)",
    description=(
        "Get a presigned S3 URL to upload a resume **directly to storage**, "
        "bypassing the ~6 MB request limit of the standard `/resume/parse` endpoint. "
        "Use this for files up to the full `max_file_size_mb`.\n\n"
        "**Flow:** call this -> POST the file to the returned `upload_url` (multipart "
        "form data: every key in `fields`, then a `file` field) -> call "
        "`POST /resume/parse-uploaded` with the returned `job_id`. "
        "The upload URL expires after `expires_in_seconds`."
    ),
    tags=["Resume"],
)
async def create_upload_url(
    payload: UploadUrlRequest,
    record: dict = Depends(get_api_key_record),
) -> UploadUrlResponse:
    settings   = get_settings()
    company_id = record["company_id"]

    if not is_supported_extension(payload.filename):
        raise api_error(
            415, ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"Unsupported file extension for '{payload.filename}'. "
            "Accepted: .pdf, .docx, .rtf, .png, .jpg, .jpeg, .tiff, .webp",
        )

    job_id    = str(ULID())
    presigned = s3_client.create_presigned_upload(
        job_id, payload.filename,
        settings.max_file_size_bytes, settings.presigned_upload_expiry_seconds,
    )
    db.create_upload_job(job_id, company_id, presigned["key"], payload.filename)

    log.info("upload_url_issued", job_id=job_id, company_id=company_id)
    return UploadUrlResponse(
        job_id=job_id,
        upload_url=presigned["url"],
        fields=presigned["fields"],
        s3_key=presigned["key"],
        max_file_size_mb=settings.max_file_size_mb,
        expires_in_seconds=settings.presigned_upload_expiry_seconds,
        parse_url="/api/v1/resume/parse-uploaded",
    )


@router.post(
    "/resume/parse-uploaded",
    response_model=ParseResponse,
    summary="Parse a file uploaded via a presigned URL",
    description=(
        "Parse a resume that was uploaded with `/resume/upload-url`. "
        "Behaves exactly like `/resume/parse`: digital PDF/DOCX/RTF return the parsed JSON "
        "**synchronously**; scanned PDFs and images return a `job_id` for "
        "**asynchronous** (OCR) processing via webhook + polling."
    ),
    tags=["Resume"],
)
async def parse_uploaded(
    payload: ParseUploadedRequest,
    background_tasks: BackgroundTasks,
    record: dict = Depends(get_api_key_record),
) -> ParseResponse:
    settings   = get_settings()
    company_id = record["company_id"]

    job = db.get_job(payload.job_id)
    if not job or job.get("company_id") != company_id:
        raise api_error(404, ErrorCode.JOB_NOT_FOUND, "Upload job not found")
    if job.get("status") != "pending_upload":
        raise api_error(
            409, ErrorCode.UPLOAD_ALREADY_PARSED,
            "This upload has already been processed",
        )

    s3_key   = job["s3_key"]
    filename = job.get("filename", "upload")

    try:
        content = s3_client.download_file(s3_key)
    except Exception:
        raise api_error(
            422, ErrorCode.UPLOAD_NOT_FOUND,
            "No uploaded file found for this job - complete the upload first",
        )

    # Validate the downloaded bytes (size + magic bytes) before processing.
    if len(content) > settings.max_file_size_bytes:
        s3_client.delete_file(s3_key)
        raise api_error(
            413, ErrorCode.FILE_TOO_LARGE,
            f"File size {len(content) // 1024} KB exceeds the {settings.max_file_size_mb} MB limit",
        )
    try:
        validate_file(filename, content)
    except UnsupportedFileTypeError as exc:
        s3_client.delete_file(s3_key)
        raise api_error(415, ErrorCode.UNSUPPORTED_FILE_TYPE, str(exc))

    # Atomically claim the job (pending_upload -> processing) before the billed
    # parse. Two concurrent parse-uploaded calls for the same job_id both pass the
    # status read above; only the claim winner proceeds - the loser gets 409 and
    # we do NOT delete the S3 file (the winner still needs it).
    if not db.claim_upload_job(payload.job_id):
        raise api_error(
            409, ErrorCode.UPLOAD_ALREADY_PARSED,
            "This upload has already been processed",
        )

    strategy, needs_async = classify(filename, content)
    needs_async = needs_async or payload.async_only
    log.info("parse_uploaded_request", job_id=payload.job_id, company_id=company_id,
             strategy=strategy, needs_async=needs_async, size_bytes=len(content),
             async_only=payload.async_only)

    # Fast synchronous PROBE; promote a partial to the full-budget async worker so
    # the caller always ends up with a COMPLETE record (see parse_resume).
    sync_result = None
    if not needs_async:
        sync_result = await resume_service.run_parse(
            job_id=payload.job_id, filename=filename, content=content,
            company_id=company_id, force_textract=payload.force_textract, sync_probe=True,
        )
        if not sync_result.partial:
            db.update_job_completed(payload.job_id, resume_service.result_record(sync_result))
            resume_service.audit_parse(
                job_id=payload.job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record,
            )
            s3_client.delete_file(s3_key)
            return ParseResponse(job_id=payload.job_id,
                                 status=resume_service.terminal_status(sync_result),
                                 data=sync_result.parsed, confidence=sync_result.confidence,
                                 skills_validation=validate_skills(sync_result.parsed),
                                 partial=False, warnings=sync_result.warnings)
        log.info("sync_partial_promoted_to_async", job_id=payload.job_id)

    # Async: the file is already in S3; the worker downloads and deletes it. This
    # covers scanned files AND a promoted sync partial - do NOT delete the S3 file
    # here (the worker needs it) and leave the job "processing" for the worker to
    # finish.
    try:
        await resume_service.dispatch_async(settings, background_tasks,
            resume_service.build_async_payload(
                job_id=payload.job_id, company_id=company_id, s3_key=s3_key, filename=filename,
                file_size_bytes=len(content), force_textract=payload.force_textract, record=record,
            ))
    except Exception:
        log.exception("async_dispatch_failed", job_id=payload.job_id)
        if sync_result is not None:
            db.update_job_completed(payload.job_id, resume_service.result_record(sync_result))
            resume_service.audit_parse(
                job_id=payload.job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record, status="partial",
            )
            s3_client.delete_file(s3_key)
            return ParseResponse(job_id=payload.job_id,
                                 status=resume_service.terminal_status(sync_result),
                                 data=sync_result.parsed, confidence=sync_result.confidence,
                                 skills_validation=validate_skills(sync_result.parsed),
                                 partial=sync_result.partial, warnings=sync_result.warnings)
        raise
    return ParseResponse(job_id=payload.job_id, status="processing",
                         poll_url=f"/api/v1/resume/job/{payload.job_id}")


# -- Job status polling --------------------------------------------------------

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
    skills_validation: SkillsValidation | None = None
    partial  = False
    warnings: list[str] = []

    if raw_result and status in ("completed", "partial"):
        parsed_data = ParsedResumeAI.model_validate(raw_result.get("data", {}))
        confidence  = ConfidenceScores.model_validate(raw_result.get("confidence", {}))
        skills_validation = validate_skills(parsed_data)
        partial  = bool(raw_result.get("partial", False))
        warnings = list(raw_result.get("warnings", []))

    return JobStatusResponse(
        job_id=job_id, status=status, data=parsed_data,
        confidence=confidence, skills_validation=skills_validation,
        partial=partial, warnings=warnings,
        error=job.get("error"),
    )


# -- Retry ---------------------------------------------------------------------

@router.post(
    "/resume/{job_id}/retry",
    response_model=RetryResponse,
    summary="Retry parsing a resume",
    description=(
        "Re-parse a resume when the original result was unsatisfactory. "
        "Re-upload the same file - the parser will re-run the full extraction "
        "and AI pipeline. A new `job_id` is created and linked to the original. "
        "Maximum retries per job: `MAX_RETRY_COUNT` (default 3). "
        "Each retry runs the full pipeline and consumes AI tokens like a fresh parse."
    ),
    tags=["Resume"],
)
async def retry_parse(
    job_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The same resume file to re-parse"),
    force_textract: bool = Form(
        False,
        description="Skip Tesseract and use AWS Textract directly for any OCR this "
                    "file needs. Useful when retrying a scan the tiered OCR misread.",
    ),
    async_only: bool = Form(
        False,
        description="Never parse synchronously: return a `job_id` + `poll_url` "
                    "immediately and run the full re-parse on the async worker. Use "
                    "this when your own gateway cannot hold a request open long enough "
                    "for a complete parse.",
    ),
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
    needs_async = needs_async or async_only
    new_job_id = str(ULID())

    log.info("retry_request", original_job_id=job_id, new_job_id=new_job_id,
             company_id=company_id, retry_count=retry_count, async_only=async_only)

    # Create retry job linked to original
    db.create_job(new_job_id, company_id, retried_from=job_id, retry_count=retry_count)

    # Same fast PROBE -> PROMOTE contract as /resume/parse: a retry is a synchronous
    # HTTP request under the same gateway ceiling, so it cannot hold the connection
    # for a full-budget parse either. If the probe can't finish, hand the file to the
    # async worker and return a poll URL rather than blocking until the gateway 504s.
    sync_result = None
    if not needs_async:
        sync_result = await resume_service.run_parse(
            job_id=new_job_id, filename=filename, content=content,
            company_id=company_id, force_textract=force_textract, sync_probe=True,
        )
        if not sync_result.partial:
            resume_service.audit_parse(
                job_id=new_job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record,
            )
            db.update_job_completed(new_job_id, resume_service.result_record(sync_result))
            return RetryResponse(
                job_id=new_job_id, original_job_id=job_id, retry_count=retry_count,
                status=resume_service.terminal_status(sync_result),
                data=sync_result.parsed, confidence=sync_result.confidence,
                skills_validation=validate_skills(sync_result.parsed),
                partial=False, warnings=sync_result.warnings,
            )
        log.info("sync_partial_promoted_to_async", job_id=new_job_id)

    try:
        s3_key = s3_client.upload_temp_file(new_job_id, filename, content)
        await resume_service.dispatch_async(settings, background_tasks,
            resume_service.build_async_payload(
                job_id=new_job_id, company_id=company_id, s3_key=s3_key, filename=filename,
                file_size_bytes=len(content), force_textract=force_textract, record=record,
            ))
    except Exception:
        # Couldn't hand off to the worker. If the probe produced something, return it
        # (flagged partial) rather than failing the retry outright.
        log.exception("async_dispatch_failed", job_id=new_job_id)
        if sync_result is not None:
            resume_service.audit_parse(
                job_id=new_job_id, company_id=company_id, result=sync_result,
                file_size_bytes=len(content), record=record, status="partial",
            )
            db.update_job_completed(new_job_id, resume_service.result_record(sync_result))
            return RetryResponse(
                job_id=new_job_id, original_job_id=job_id, retry_count=retry_count,
                status=resume_service.terminal_status(sync_result),
                data=sync_result.parsed, confidence=sync_result.confidence,
                skills_validation=validate_skills(sync_result.parsed),
                partial=sync_result.partial, warnings=sync_result.warnings,
            )
        raise
    return RetryResponse(
        job_id=new_job_id, original_job_id=job_id, retry_count=retry_count,
        status="processing", poll_url=f"/api/v1/resume/job/{new_job_id}",
    )


# -- Feedback (model improvement) ----------------------------------------------

@router.post(
    "/resume/{job_id}/feedback",
    response_model=FeedbackResponse,
    status_code=202,
    summary="Submit parsing feedback",
    description=(
        "Submit the original parser JSON together with the user-corrected JSON "
        "after a review. The corrections are stored (scoped to your account) and "
        "used to improve parsing accuracy over time.\n\n"
        "Send this **after** the review step - typically only when the user "
        "actually changed something (`changed: true`), though feedback with no "
        "changes is also accepted as a positive signal. Processed asynchronously; "
        "the response (HTTP 202) confirms the feedback was recorded."
    ),
    tags=["Resume"],
)
async def submit_feedback(
    job_id: str,
    payload: FeedbackRequest,
    record: dict = Depends(get_api_key_record),
) -> FeedbackResponse:
    company_id = record["company_id"]

    # Defense-in-depth: if the original job is still on record (jobs TTL ~1h),
    # make sure it belongs to this account. Feedback often arrives after the job
    # has expired, so a missing job is fine - we still accept it.
    original_job = db.get_job(job_id)
    if original_job and original_job.get("company_id") != company_id:
        raise api_error(404, ErrorCode.JOB_NOT_FOUND, "Job not found")

    changed_fields = diff_fields(payload.original, payload.updated)
    # Trust the client's flag if given; otherwise derive it from the diff.
    changed = payload.changed if payload.changed is not None else bool(changed_fields)

    feedback_id = str(ULID())
    created_at = datetime.now(UTC).isoformat()
    db.create_feedback(
        feedback_id=feedback_id,
        job_id=job_id,
        company_id=company_id,
        original=payload.original,
        updated=payload.updated,
        changed=changed,
        changed_fields=changed_fields,
        created_at=created_at,
        profile_id=payload.profile_id,
        notes=payload.notes,
    )
    log.info(
        "feedback_received",
        job_id=job_id, company_id=company_id, feedback_id=feedback_id,
        changed=changed, changed_field_count=len(changed_fields),
    )
    return FeedbackResponse(
        feedback_id=feedback_id,
        job_id=job_id,
        status="accepted",
        changed=changed,
        changed_fields=changed_fields,
        created_at=created_at,
    )
