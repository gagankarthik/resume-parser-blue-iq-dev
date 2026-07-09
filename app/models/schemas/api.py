"""HTTP request/response DTOs for the public API.

Strict validation + OpenAPI metadata — distinct from the resume DOMAIN models in
resume.py, which sanitize rather than reject.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.schemas.analysis import ConfidenceScores, SkillsValidation
from app.models.schemas.resume import ParsedResumeAI
from app.models.schemas.validators import _URL_RE


class ParseResponse(BaseModel):
    """Response for POST /api/v1/resume/parse"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "completed",
                "data": {"personal_info": {}, "experience": [], "education": [], "skills": []},
                "confidence": {"overall": 0.91, "personal_info": 0.96, "experience": 0.88, "education": 0.90, "skills": 1.0},
                "poll_url": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="Unique job identifier (ULID)")
    status:            str                        = Field(..., description="'completed' (clean) or 'partial' (degraded — see `partial`/`warnings`) for sync jobs; 'processing' for async (OCR) jobs")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed resume data — present when status is 'completed' or 'partial'")
    confidence:        ConfidenceScores | None = Field(None, description="Per-section confidence scores — present when status is 'completed' or 'partial'")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — present when status is 'completed' or 'partial'")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered (e.g. contact anchors) and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing (e.g. AI parse failed and a partial record was returned). Empty on a clean parse.")
    poll_url:          str | None              = Field(None, description="Polling URL — present when status is 'processing'")


class UploadUrlRequest(BaseModel):
    """Request body for POST /api/v1/resume/upload-url"""

    model_config = ConfigDict(
        json_schema_extra={"example": {"filename": "jane_smith_rn.pdf"}}
    )

    filename: str = Field(
        ...,
        description="Original filename including extension (.pdf, .docx, .rtf, .png, .jpg, .jpeg, .tiff, .webp)",
        examples=["jane_smith_rn.pdf"],
    )

    @field_validator("filename")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("filename is required")
        return v


class UploadUrlResponse(BaseModel):
    """Response for POST /api/v1/resume/upload-url.

    Use this for files larger than ~6 MB. POST the file as multipart form data to
    `upload_url`, including every key in `fields` plus a `file` field, then call
    `parse_url` with the returned `job_id`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "upload_url": "https://resume-parser-blue-iq-temp.s3.amazonaws.com/",
                "fields": {
                    "key": "temp/01J3K5M2N4P6Q8R0S2T4U6V8W0/jane_smith_rn.pdf",
                    "x-amz-server-side-encryption": "AES256",
                    "policy": "<base64-policy>",
                    "x-amz-signature": "<signature>",
                },
                "s3_key": "temp/01J3K5M2N4P6Q8R0S2T4U6V8W0/jane_smith_rn.pdf",
                "max_file_size_mb": 10,
                "expires_in_seconds": 900,
                "parse_url": "/api/v1/resume/parse-uploaded",
            }
        }
    )

    job_id:             str            = Field(..., description="Job identifier (ULID) — pass this to parse-uploaded")
    upload_url:         str            = Field(..., description="S3 endpoint to POST the file to (multipart form data)")
    fields:             dict[str, str] = Field(..., description="Form fields that must accompany the upload, exactly as given")
    s3_key:             str            = Field(..., description="The S3 object key the file will be stored under")
    max_file_size_mb:   int            = Field(..., description="Maximum accepted file size; larger uploads are rejected by S3")
    expires_in_seconds: int            = Field(..., description="Seconds until the upload URL expires")
    parse_url:          str            = Field(..., description="Endpoint to call (with job_id) once the upload completes")


class ParseUploadedRequest(BaseModel):
    """Request body for POST /api/v1/resume/parse-uploaded"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0", "force_textract": False}
        }
    )

    job_id: str = Field(..., description="The job_id returned by /resume/upload-url")
    force_textract: bool = Field(
        False,
        description="Skip Tesseract and use AWS Textract directly for any OCR this "
                    "file needs (scanned PDF/image, or a digital PDF with a broken "
                    "text layer). Higher accuracy on hard scans, higher cost.",
    )

    @field_validator("job_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("job_id is required")
        return v


class JobStatusResponse(BaseModel):
    """Response for GET /api/v1/resume/job/{job_id}"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "completed",
                "data": None,
                "confidence": None,
                "error": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="Job identifier")
    status:            str                        = Field(..., description="pending | processing | completed | partial | failed")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed data — set when status is 'completed' or 'partial'")
    confidence:        ConfidenceScores | None = Field(None, description="Confidence scores — set when status is 'completed' or 'partial'")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — set when status is 'completed' or 'partial'")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing. Empty on a clean parse.")
    error:             str | None              = Field(None, description="Error description — set when status is 'failed'")


class WebhookCreateRequest(BaseModel):
    """Request body for POST /api/v1/webhooks"""

    url:    str        = Field(
        ...,
        description="HTTPS URL that will receive webhook POST requests",
        examples=["https://your-server.com/hooks/resume"],
    )
    events: list[str]  = Field(
        ...,
        description="Events to subscribe to",
        examples=[["parse.completed", "parse.failed", "batch.completed"]],
        min_length=1,
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not _URL_RE.match(v):
            raise ValueError("Webhook URL must begin with http:// or https://")
        if len(v) > 2048:
            raise ValueError("Webhook URL too long")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        valid = {"parse.completed", "parse.failed", "batch.completed"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Invalid events: {sorted(invalid)}. Valid: {sorted(valid)}")
        return v


class WebhookResponse(BaseModel):
    """Response for webhook endpoints"""

    webhook_id:   str           = Field(..., description="Webhook identifier")
    url:          str           = Field(..., description="Delivery URL")
    events:       list[str]     = Field(..., description="Subscribed events")
    hmac_secret:  str | None = Field(None, description="HMAC signing secret — only returned on creation")
    status:       str           = Field(..., description="active | disabled")
    created_at:   str           = Field(..., description="ISO 8601 creation timestamp")


class ErrorDetail(BaseModel):
    """Uniform error envelope used by all 4xx/5xx responses"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "status_code": 413,
                    "error_code":  "FILE_TOO_LARGE",
                    "detail":      "File size 12288 KB exceeds the 10 MB limit",
                    "hint":        "Please reduce the file size or split the document and try again.",
                    "request_id":  "8f3a-b212-4c7e-9d1f-a8b3c0e1d2f4",
                }
            }
        }
    )

    status_code:  int = Field(..., description="HTTP status code")
    error_code:   str = Field(..., description="Machine-readable error identifier (e.g. FILE_TOO_LARGE)")
    detail:       str = Field(..., description="Developer-readable error description")
    hint:         str = Field(..., description="User-facing actionable message — display this to your end user")
    request_id:   str = Field(..., description="Request ID — include in support tickets")


class HealthResponse(BaseModel):
    status:        str            = Field(..., description="'ok' or 'degraded'")
    version:       str            = Field(..., description="API version")
    environment:   str            = Field(..., description="development | production")
    latency_ms:    int | None  = Field(None, description="Dependency probe round-trip time in ms")
    dependencies:  dict | None = Field(None, description="Per-dependency status: ok | unreachable")


class RetryResponse(BaseModel):
    """Response for POST /api/v1/resume/{job_id}/retry"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "01J3K5M3N5P7Q9R1S3T5U7V9W1",
                "original_job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "retry_count": 1,
                "status": "completed",
                "data": None,
                "confidence": None,
                "poll_url": None,
            }
        }
    )

    job_id:            str                        = Field(..., description="New job ID for this retry attempt")
    original_job_id:   str                        = Field(..., description="The job ID that was retried")
    retry_count:       int                        = Field(..., description="How many times this job has been retried (1 = first retry)")
    status:            str                        = Field(..., description="completed | partial | processing")
    data:              ParsedResumeAI | None   = Field(None, description="Parsed data — set when status is completed or partial")
    confidence:        ConfidenceScores | None = Field(None, description="Confidence scores — set when status is completed or partial")
    skills_validation: SkillsValidation | None = Field(None, description="Skills validated against the healthcare taxonomy — set when status is completed or partial")
    partial:           bool                       = Field(False, description="True when parsing degraded — `data` holds only what could be recovered and needs human review. See `warnings`.")
    warnings:          list[str]                  = Field(default_factory=list, description="Non-fatal issues detected during parsing. Empty on a clean parse.")
    poll_url:          str | None              = Field(None, description="Polling URL — set for async retries")

