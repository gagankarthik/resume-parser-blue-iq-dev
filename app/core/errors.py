"""
Centralized error code registry, HTTP error factory, and user-facing hints.

Every API error surfaces:
  * error_code   - machine-readable, for frontend branching
  * detail       - developer-readable description
  * hint         - user-facing actionable message (display this to the end user)
  * docs_url     - link to relevant docs section (optional)

Response envelope (serialised by app/main.py):
    {
        "error": {
            "status_code": 413,
            "error_code":  "FILE_TOO_LARGE",
            "detail":      "File size 12 MB exceeds the 10 MB limit",
            "hint":        "Please reduce the file size or split the document and try again.",
            "request_id":  "abc-123"
        }
    }
"""

from dataclasses import dataclass
from enum import StrEnum
from string import Template

from fastapi import HTTPException

from app.core.config import get_settings


class ErrorCode(StrEnum):
    # -- Authentication --------------------------------------------------------
    MISSING_API_KEY        = "MISSING_API_KEY"
    INVALID_API_KEY_FORMAT = "INVALID_API_KEY_FORMAT"
    INVALID_API_KEY        = "INVALID_API_KEY"
    REVOKED_API_KEY        = "REVOKED_API_KEY"
    ACCOUNT_DEACTIVATED    = "ACCOUNT_DEACTIVATED"

    # -- File Validation -------------------------------------------------------
    FILE_TOO_LARGE         = "FILE_TOO_LARGE"
    UNSUPPORTED_FILE_TYPE  = "UNSUPPORTED_FILE_TYPE"
    # One code for BOTH an empty document and a document that is not a resume
    # (job description, cover letter, invoice...). Clients branch on one value.
    NOT_A_RESUME           = "NOT_A_RESUME"
    EMPTY_BATCH            = "EMPTY_BATCH"
    BATCH_TOO_LARGE        = "BATCH_TOO_LARGE"

    # -- Processing ------------------------------------------------------------
    EXTRACTION_FAILED      = "EXTRACTION_FAILED"
    OCR_FAILED             = "OCR_FAILED"
    PARSE_FAILED           = "PARSE_FAILED"
    PARSE_TIMEOUT          = "PARSE_TIMEOUT"
    WORKER_DISPATCH_FAILED = "WORKER_DISPATCH_FAILED"

    # -- Resources -------------------------------------------------------------
    JOB_NOT_FOUND          = "JOB_NOT_FOUND"
    BATCH_NOT_FOUND        = "BATCH_NOT_FOUND"
    WEBHOOK_NOT_FOUND      = "WEBHOOK_NOT_FOUND"
    UPLOAD_NOT_FOUND       = "UPLOAD_NOT_FOUND"
    UPLOAD_ALREADY_PARSED  = "UPLOAD_ALREADY_PARSED"

    # -- Retry -----------------------------------------------------------------
    RETRY_LIMIT_REACHED    = "RETRY_LIMIT_REACHED"

    # -- Input Validation ------------------------------------------------------
    VALIDATION_ERROR       = "VALIDATION_ERROR"
    INVALID_REQUEST        = "INVALID_REQUEST"
    REQUEST_TOO_LARGE      = "REQUEST_TOO_LARGE"

    # -- Rate limiting ---------------------------------------------------------
    RATE_LIMITED           = "RATE_LIMITED"

    # -- Internal --------------------------------------------------------------
    INTERNAL_ERROR         = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE    = "SERVICE_UNAVAILABLE"


# -- User-facing hints - shown directly to end users by the frontend -----------
# Keep these plain-language, jargon-free, and actionable. `$name` placeholders are
# filled from settings by get_hint(), so a hint never quotes a stale limit.

_HINTS: dict[str, str] = {
    ErrorCode.MISSING_API_KEY: (
        "An API key is required. Include your key in the X-API-Key request header."
    ),
    ErrorCode.INVALID_API_KEY_FORMAT: (
        "The API key format is invalid. Keys start with 'rp_live_' followed by 32+ characters."
    ),
    ErrorCode.INVALID_API_KEY: (
        "The API key was not recognised. Check that you are using the correct key "
        "and that it has not been regenerated."
    ),
    ErrorCode.REVOKED_API_KEY: (
        "This API key has been revoked. Contact your administrator to issue a new key."
    ),
    ErrorCode.ACCOUNT_DEACTIVATED: (
        "This account has been deactivated. Contact your administrator to reactivate it."
    ),
    ErrorCode.FILE_TOO_LARGE: (
        "The uploaded file is too large. Maximum size is $max_file_size_mb MB. "
        "Try compressing the PDF or splitting the document into smaller files."
    ),
    ErrorCode.UNSUPPORTED_FILE_TYPE: (
        "This file type is not supported. Please upload a PDF, DOCX, PNG, JPG, or TIFF file."
    ),
    ErrorCode.NOT_A_RESUME: (
        "This file is empty or is not a resume. Upload the candidate's resume (CV); "
        "job descriptions, cover letters, and other documents are not accepted."
    ),
    ErrorCode.EMPTY_BATCH: (
        "No valid files were found in the batch. Check the 'skipped_files' field in the "
        "response for details on why each file was rejected."
    ),
    ErrorCode.BATCH_TOO_LARGE: (
        "The batch contains too many files. Maximum is $max_batch_size files per request. "
        "Split the files into multiple batch requests."
    ),
    ErrorCode.WORKER_DISPATCH_FAILED: (
        "Background processing for this file could not be started. "
        "Please retry the upload; if the problem persists, contact support."
    ),
    ErrorCode.EXTRACTION_FAILED: (
        "The resume text could not be extracted. The document may be password-protected, "
        "heavily formatted, or corrupted. Try saving as a plain PDF and re-uploading."
    ),
    ErrorCode.OCR_FAILED: (
        "Text recognition failed for this scanned document. Ensure the image is clear, "
        "well-lit, and not rotated. Higher resolution scans (300 DPI+) work best."
    ),
    ErrorCode.PARSE_FAILED: (
        "The AI parser could not process this resume. The document may be unusual in format. "
        "Try the retry endpoint (POST /resume/{job_id}/retry) with the same file."
    ),
    ErrorCode.PARSE_TIMEOUT: (
        "AI parsing took too long. Try again - if the problem persists, "
        "the resume may be unusually long. Consider trimming to the most recent experience."
    ),
    ErrorCode.JOB_NOT_FOUND: (
        "This job ID was not found. Async job results are available for 1 hour after processing. "
        "The job may have expired - re-upload the resume to create a new job."
    ),
    ErrorCode.BATCH_NOT_FOUND: (
        "This batch ID was not found. Batch results are available for 24 hours. "
        "The batch may have expired - re-submit the files."
    ),
    ErrorCode.WEBHOOK_NOT_FOUND: (
        "This webhook was not found or does not belong to your account."
    ),
    ErrorCode.UPLOAD_NOT_FOUND: (
        "No uploaded file was found for this job. Upload the file to the presigned URL "
        "first, then call /resume/parse-uploaded. Upload URLs expire after "
        "$upload_expiry_minutes minutes."
    ),
    ErrorCode.UPLOAD_ALREADY_PARSED: (
        "This upload has already been parsed. Request a new upload URL to parse another file."
    ),
    ErrorCode.RETRY_LIMIT_REACHED: (
        "This resume has already been retried the maximum number of times. "
        "If the results are still unsatisfactory, contact support with the job_id."
    ),
    ErrorCode.VALIDATION_ERROR: (
        "The request contains invalid data. Check the 'detail' field for which fields failed validation."
    ),
    ErrorCode.INVALID_REQUEST: (
        "The request is invalid. Check the API documentation for the correct request format."
    ),
    ErrorCode.REQUEST_TOO_LARGE: (
        "The request body is too large. Use the presigned-upload flow "
        "(POST /resume/upload-url) for large files."
    ),
    ErrorCode.RATE_LIMITED: (
        "You have sent too many requests. Slow down and retry after the period "
        "indicated by the Retry-After header."
    ),
    ErrorCode.INTERNAL_ERROR: (
        "An unexpected error occurred on our side. This has been logged automatically. "
        "Please try again in a moment. If it persists, contact support with your X-Request-ID."
    ),
    ErrorCode.SERVICE_UNAVAILABLE: (
        "The service is temporarily unavailable. Please try again in a few minutes."
    ),
}


def get_hint(error_code: str) -> str:
    hint = _HINTS.get(error_code)
    if hint is None:
        return "Please check the API documentation or contact support."
    settings = get_settings()
    return Template(hint).safe_substitute(
        max_file_size_mb=settings.max_file_size_mb,
        max_batch_size=settings.max_batch_size,
        upload_expiry_minutes=max(1, settings.presigned_upload_expiry_seconds // 60),
    )


def api_error(
    status_code: int,
    error_code: ErrorCode,
    detail: str,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """
    Build an HTTPException with a structured detail dict.
    The global HTTPException handler in main.py serialises this into the
    standard error envelope including the user-facing hint. `headers` (e.g.
    Retry-After) are propagated onto the response by that handler.
    """
    return HTTPException(
        status_code=status_code,
        detail={
            "error_code": str(error_code),
            "detail":     detail,
            "hint":       get_hint(str(error_code)),
        },
        headers=headers,
    )


# -- What a client may be told about a failure ---------------------------------

@dataclass(frozen=True)
class PublicError:
    status_code: int
    error_code: ErrorCode
    detail: str


def public_error(exc: BaseException) -> PublicError:
    """Map an exception to the status, code and message a client may see.

    The one mapping behind the HTTP error envelope, a failed job's poll response
    and its `parse.failed` webhook, so the three can never disagree. Only domain
    errors whose message is written for the client keep their text. Everything
    else (botocore, OpenAI, a bug) gets a generic message: its text names buckets,
    ARNs and request ids, and its class name is not a contract. Log the exception
    itself for diagnosis.
    """
    # Imported here, not at module level: exceptions.py imports ErrorCode from us.
    from app.core.exceptions import (
        ExtractionError,
        FileValidationError,
        NotAResumeError,
        OCRError,
    )

    if isinstance(exc, NotAResumeError):
        return PublicError(422, ErrorCode.NOT_A_RESUME, str(exc))
    if isinstance(exc, OCRError):  # before ExtractionError: OCRError subclasses it
        return PublicError(422, ErrorCode.OCR_FAILED, str(exc))
    if isinstance(exc, ExtractionError):
        return PublicError(422, ErrorCode.EXTRACTION_FAILED, str(exc))
    if isinstance(exc, FileValidationError):
        return PublicError(415, ErrorCode.UNSUPPORTED_FILE_TYPE, str(exc))
    if isinstance(exc, TimeoutError):
        return PublicError(504, ErrorCode.PARSE_TIMEOUT, "Parsing took too long and was cancelled.")
    return PublicError(500, ErrorCode.PARSE_FAILED, "Resume processing failed.")
