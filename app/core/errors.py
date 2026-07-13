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

from enum import StrEnum

from fastapi import HTTPException


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
    CORRUPTED_FILE         = "CORRUPTED_FILE"
    EMPTY_BATCH            = "EMPTY_BATCH"
    BATCH_TOO_LARGE        = "BATCH_TOO_LARGE"

    # -- Processing ------------------------------------------------------------
    EXTRACTION_FAILED      = "EXTRACTION_FAILED"
    OCR_FAILED             = "OCR_FAILED"
    PARSE_FAILED           = "PARSE_FAILED"
    EXTRACTION_TIMEOUT     = "EXTRACTION_TIMEOUT"
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
# Keep these plain-language, jargon-free, and actionable.

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
        "The uploaded file is too large. Maximum size is 10 MB. "
        "Try compressing the PDF or splitting the document into smaller files."
    ),
    ErrorCode.UNSUPPORTED_FILE_TYPE: (
        "This file type is not supported. Please upload a PDF, DOCX, PNG, JPG, or TIFF file."
    ),
    ErrorCode.CORRUPTED_FILE: (
        "The file appears to be corrupted or is not a valid document. "
        "Try re-exporting the resume from the original application and upload again."
    ),
    ErrorCode.EMPTY_BATCH: (
        "No valid files were found in the batch. Check the 'skipped_files' field in the "
        "response for details on why each file was rejected."
    ),
    ErrorCode.BATCH_TOO_LARGE: (
        "The batch contains too many files. Maximum is 200 files per request. "
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
    ErrorCode.EXTRACTION_TIMEOUT: (
        "Text extraction took too long and was cancelled. "
        "Large or complex documents may need to be split into smaller files."
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
        "first, then call /resume/parse-uploaded. Upload URLs expire after 15 minutes."
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
    return _HINTS.get(error_code, "Please check the API documentation or contact support.")


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
