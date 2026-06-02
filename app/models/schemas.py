"""
All Pydantic schemas used across the application.

Design principles:
  • ParsedResumeAI   — OpenAI structured output target. Validators SANITIZE (return
                       None/default) rather than raise, so malformed LLM output never
                       crashes the pipeline.
  • API I/O schemas  — Request/response models with strict validation and OpenAPI metadata.
  • ConfidenceScores — Per-section 0.0–1.0 scores.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── Shared validators ─────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_DATE_RE   = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_URL_RE    = re.compile(r"^https?://", re.I)
_PHONE_DIGITS_RE = re.compile(r"\d{6,}")


def _sanitize_email(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower()
    return v if _EMAIL_RE.match(v) else None


def _sanitize_url(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if not _URL_RE.match(v):
        v = f"https://{v}"
    return v if len(v) <= 2048 else None


def _sanitize_date(v: str | None) -> str | None:
    """Accept YYYY-MM or 'Present'; return None for unrecognised formats."""
    if not v:
        return None
    v = v.strip()
    if v.lower() == "present":
        return "Present"
    return v if _DATE_RE.match(v) else None


def _sanitize_year(v: int | None) -> int | None:
    if v is None:
        return None
    return v if 1900 <= v <= 2035 else None


def _sanitize_str(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    return v if v else None


def _coerce_list(v) -> list:  # type: ignore[no-untyped-def]
    """Ensure list fields are always lists — guards against LLM returning null."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return []


# ── Resume data models (OpenAI structured output target) ──────────────────────

class PersonalInfo(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "full_name": "Jane Smith, RN",
                "email": "jane.smith@email.com",
                "phone": "+1 555 234 5678",
                "location": "Houston, TX",
                "linkedin_url": "https://linkedin.com/in/janesmith",
                "github_url": None,
                "portfolio_url": None,
                "summary": "ICU Registered Nurse with 8 years of critical care experience.",
            }
        }
    )

    full_name:      Optional[str] = Field(None, description="Full name of the candidate, including credentials if listed (e.g. 'Jane Smith, RN')")
    email:          Optional[str] = Field(None, description="Primary email address")
    phone:          Optional[str] = Field(None, description="Primary phone number in original format")
    location:       Optional[str] = Field(None, description="City, state and/or country")
    linkedin_url:   Optional[str] = Field(None, description="LinkedIn profile URL")
    github_url:     Optional[str] = Field(None, description="GitHub profile URL")
    portfolio_url:  Optional[str] = Field(None, description="Personal website or portfolio URL")
    summary:        Optional[str] = Field(None, description="Professional summary or objective statement")

    @field_validator("full_name", "location", "summary", mode="before")
    @classmethod
    def sanitize_strings(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: object) -> str | None:
        return _sanitize_email(str(v)) if isinstance(v, str) else None

    @field_validator("linkedin_url", "github_url", "portfolio_url", mode="before")
    @classmethod
    def sanitize_urls(cls, v: object) -> str | None:
        return _sanitize_url(str(v)) if isinstance(v, str) else None

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, v: object) -> str | None:
        if not isinstance(v, str):
            return None
        v = v.strip()
        return v if _PHONE_DIGITS_RE.search(v) else None


class ExperienceItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "company": "Memorial Hermann Hospital",
                "role": "RN - ICU",
                "start_date": "2020-03",
                "end_date": "Present",
                "is_current": True,
                "location": "Houston, TX",
                "description": "Provided critical care nursing for 12-bed MICU.",
                "achievements": [
                    "Charge nurse for 18-month period",
                    "Preceptor for 6 new graduate nurses",
                ],
            }
        }
    )

    company:       str           = Field(..., description="Employer or facility name")
    role:          str           = Field(..., description="Job title or role, including credential if present (e.g. 'RN - MICU')")
    start_date:    Optional[str] = Field(None, description="Start date in YYYY-MM format")
    end_date:      Optional[str] = Field(None, description="End date in YYYY-MM format, or 'Present' for current role")
    is_current:    bool          = Field(False, description="True if this is the candidate's current position")
    location:      Optional[str] = Field(None, description="City and state of the workplace")
    description:   Optional[str] = Field(None, description="Role description and responsibilities")
    achievements:  list[str]     = Field(default_factory=list, description="Notable accomplishments in this role")

    @field_validator("company", "role", mode="before")
    @classmethod
    def required_strings(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown"
        return v.strip()

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def sanitize_dates(cls, v: object) -> str | None:
        return _sanitize_date(str(v)) if isinstance(v, str) else None

    @field_validator("location", "description", mode="before")
    @classmethod
    def sanitize_optional_strings(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("achievements", mode="before")
    @classmethod
    def coerce_achievements(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [i for i in items if isinstance(i, str) and i.strip()]

    @model_validator(mode="after")
    def sync_is_current(self) -> "ExperienceItem":
        if self.end_date and self.end_date.lower() == "present":
            self.is_current = True
        return self


class EducationItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "institution": "University of Texas Health Science Center",
                "degree": "Bachelor of Science in Nursing",
                "field_of_study": "Nursing",
                "start_year": 2012,
                "graduation_year": 2016,
                "gpa": "3.8",
            }
        }
    )

    institution:      str           = Field(..., description="Name of the school or university")
    degree:           Optional[str] = Field(None, description="Degree earned (e.g. 'Bachelor of Science in Nursing')")
    field_of_study:   Optional[str] = Field(None, description="Major or field of study")
    start_year:       Optional[int] = Field(None, description="Year started (1900–2035)", ge=1900, le=2035)
    graduation_year:  Optional[int] = Field(None, description="Graduation year (1900–2035)", ge=1900, le=2035)
    gpa:              Optional[str] = Field(None, description="GPA if stated")

    @field_validator("institution", mode="before")
    @classmethod
    def required_institution(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown Institution"
        return v.strip()

    @field_validator("degree", "field_of_study", "gpa", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("start_year", "graduation_year", mode="before")
    @classmethod
    def sanitize_year(cls, v: object) -> int | None:
        if isinstance(v, (int, float)):
            return _sanitize_year(int(v))
        if isinstance(v, str) and v.strip().isdigit():
            return _sanitize_year(int(v.strip()))
        return None


class CertificationItem(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "ACLS",
                "issuer": "American Heart Association",
                "issued_date": "2023-06",
                "expiry_date": "2025-06",
                "credential_id": None,
            }
        }
    )

    name:           str           = Field(..., description="Certification name (e.g. 'ACLS', 'BLS', 'CCRN')")
    issuer:         Optional[str] = Field(None, description="Issuing organisation")
    issued_date:    Optional[str] = Field(None, description="Issue date in YYYY-MM format")
    expiry_date:    Optional[str] = Field(None, description="Expiry date in YYYY-MM format")
    credential_id:  Optional[str] = Field(None, description="Credential ID or certificate number")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            return "Unknown Certification"
        return v.strip()

    @field_validator("issuer", "credential_id", mode="before")
    @classmethod
    def sanitize_optional(cls, v: object) -> str | None:
        return _sanitize_str(str(v)) if isinstance(v, str) else None

    @field_validator("issued_date", "expiry_date", mode="before")
    @classmethod
    def sanitize_dates(cls, v: object) -> str | None:
        return _sanitize_date(str(v)) if isinstance(v, str) else None


class ProjectItem(BaseModel):
    name:          str           = Field(..., description="Project name")
    description:   Optional[str] = Field(None, description="Brief description of the project")
    technologies:  list[str]     = Field(default_factory=list, description="Technologies used")
    url:           Optional[str] = Field(None, description="Project URL")

    @field_validator("name", mode="before")
    @classmethod
    def required_name(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) and str(v).strip() else "Untitled Project"

    @field_validator("technologies", mode="before")
    @classmethod
    def coerce_list(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [i for i in items if isinstance(i, str) and i.strip()]


class ParsedResumeAI(BaseModel):
    """
    Structured output schema enforced on the OpenAI response.
    All list fields default to [] so the model is never None on missing sections.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "personal_info": {
                    "full_name": "Jane Smith, RN",
                    "email": "jane.smith@email.com",
                    "phone": "+1 555 234 5678",
                    "location": "Houston, TX",
                    "linkedin_url": "https://linkedin.com/in/janesmith",
                    "summary": "ICU Registered Nurse with 8 years of critical care experience.",
                },
                "experience": [
                    {
                        "company": "Memorial Hermann Hospital",
                        "role": "RN - ICU",
                        "start_date": "2020-03",
                        "end_date": "Present",
                        "is_current": True,
                        "location": "Houston, TX",
                        "description": "Provided critical care nursing in a 12-bed MICU.",
                        "achievements": ["Charge nurse for 18 months"],
                    }
                ],
                "education": [
                    {
                        "institution": "UT Health Science Center",
                        "degree": "Bachelor of Science in Nursing",
                        "field_of_study": "Nursing",
                        "graduation_year": 2016,
                    }
                ],
                "skills": ["RN", "ICU", "NICU", "ACLS", "BLS"],
                "certifications": [
                    {"name": "ACLS", "issuer": "American Heart Association", "expiry_date": "2025-06"}
                ],
                "projects": [],
                "languages": ["English", "Spanish"],
            }
        }
    )

    personal_info:   PersonalInfo          = Field(default_factory=PersonalInfo, description="Candidate contact and identity information")
    experience:      list[ExperienceItem]  = Field(default_factory=list, description="Work experience entries, most recent first")
    education:       list[EducationItem]   = Field(default_factory=list, description="Education history")
    skills:          list[str]             = Field(default_factory=list, description="Clinical specialties, credentials, and skills (e.g. ICU, NICU, BLS, ACLS)")
    certifications:  list[CertificationItem] = Field(default_factory=list, description="Professional certifications and licenses")
    projects:        list[ProjectItem]     = Field(default_factory=list, description="Notable projects")
    languages:       list[str]             = Field(default_factory=list, description="Spoken/written languages")

    @field_validator("experience", "education", "certifications", "projects", mode="before")
    @classmethod
    def coerce_lists(cls, v: object) -> list:
        return _coerce_list(v)

    @field_validator("skills", "languages", mode="before")
    @classmethod
    def coerce_string_lists(cls, v: object) -> list[str]:
        items = _coerce_list(v)
        return [str(i).strip() for i in items if i and str(i).strip()]


# ── Confidence scores ─────────────────────────────────────────────────────────

class ConfidenceScores(BaseModel):
    overall:       float = Field(..., ge=0.0, le=1.0, description="Weighted overall confidence (0.0 = no data, 1.0 = complete)")
    personal_info: float = Field(..., ge=0.0, le=1.0, description="Confidence in personal contact fields")
    experience:    float = Field(..., ge=0.0, le=1.0, description="Confidence in experience entries")
    education:     float = Field(..., ge=0.0, le=1.0, description="Confidence in education entries")
    skills:        float = Field(..., ge=0.0, le=1.0, description="Confidence in skills / specialties list")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "overall": 0.91,
                "personal_info": 0.96,
                "experience": 0.88,
                "education": 0.90,
                "skills": 1.0,
            }
        }
    )


# ── API request / response schemas ───────────────────────────────────────────

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

    job_id:      str                        = Field(..., description="Unique job identifier (ULID)")
    status:      str                        = Field(..., description="'completed' for sync jobs, 'processing' for async (OCR) jobs")
    data:        Optional[ParsedResumeAI]   = Field(None, description="Parsed resume data — present when status is 'completed'")
    confidence:  Optional[ConfidenceScores] = Field(None, description="Per-section confidence scores — present when status is 'completed'")
    poll_url:    Optional[str]              = Field(None, description="Polling URL — present when status is 'processing'")


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

    job_id:      str                        = Field(..., description="Job identifier")
    status:      str                        = Field(..., description="pending | processing | completed | failed")
    data:        Optional[ParsedResumeAI]   = Field(None, description="Parsed data — set when status is 'completed'")
    confidence:  Optional[ConfidenceScores] = Field(None, description="Confidence scores — set when status is 'completed'")
    error:       Optional[str]              = Field(None, description="Error description — set when status is 'failed'")


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
    hmac_secret:  Optional[str] = Field(None, description="HMAC signing secret — only returned on creation")
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
    latency_ms:    Optional[int]  = Field(None, description="Dependency probe round-trip time in ms")
    dependencies:  Optional[dict] = Field(None, description="Per-dependency status: ok | unreachable")


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

    job_id:          str                        = Field(..., description="New job ID for this retry attempt")
    original_job_id: str                        = Field(..., description="The job ID that was retried")
    retry_count:     int                        = Field(..., description="How many times this job has been retried (1 = first retry)")
    status:          str                        = Field(..., description="completed | processing")
    data:            Optional[ParsedResumeAI]   = Field(None, description="Parsed data — set when status is completed")
    confidence:      Optional[ConfidenceScores] = Field(None, description="Confidence scores — set when status is completed")
    poll_url:        Optional[str]              = Field(None, description="Polling URL — set for async retries")


# ── Batch schemas ─────────────────────────────────────────────────────────────

class BatchSkipped(BaseModel):
    filename:  str = Field(..., description="Original filename")
    reason:    str = Field(..., description="Why this file was rejected")


class BatchSubmitResponse(BaseModel):
    """Response for POST /api/v1/resume/batch"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "total": 148,
                "skipped": 2,
                "skipped_files": [
                    {"filename": "notes.txt", "reason": "Unsupported file extension '.txt'"},
                ],
                "job_ids": ["01J3K5M2...", "01J3K5M3..."],
                "status": "processing",
                "poll_url": "/api/v1/resume/batch/01J3K5M2N4P6Q8R0S2T4U6V8W0",
            }
        }
    )

    batch_id:       str               = Field(..., description="Batch identifier")
    total:          int               = Field(..., description="Number of files accepted for processing")
    skipped:        int               = Field(..., description="Number of files rejected at upload time")
    skipped_files:  list[BatchSkipped] = Field(default_factory=list, description="Details of rejected files")
    job_ids:        list[str]         = Field(..., description="Job IDs for accepted files, in submission order")
    status:         str               = Field(..., description="Always 'processing' — results arrive via webhook or polling")
    poll_url:       str               = Field(..., description="URL to poll for overall batch status")


class BatchStatusResponse(BaseModel):
    """Response for GET /api/v1/resume/batch/{batch_id}"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "partial",
                "total": 150,
                "completed": 145,
                "failed": 5,
                "processing": 0,
                "created_at": "2026-06-01T10:00:00+00:00",
                "completed_at": "2026-06-01T10:08:42+00:00",
            }
        }
    )

    batch_id:       str           = Field(..., description="Batch identifier")
    status:         str           = Field(..., description="processing | completed | partial | failed")
    total:          int           = Field(..., description="Total files in batch")
    completed:      int           = Field(..., description="Successfully parsed files")
    failed:         int           = Field(..., description="Files that failed parsing")
    processing:     int           = Field(..., description="Files still in progress (total - completed - failed)")
    created_at:     str           = Field(..., description="ISO 8601 timestamp when batch was submitted")
    completed_at:   Optional[str] = Field(None, description="ISO 8601 timestamp when all files finished")
