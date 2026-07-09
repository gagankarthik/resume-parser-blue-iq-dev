"""Pydantic schemas package.

Split by concern — validators / resume domain models / analysis / api DTOs /
feedback / batch. This ``__init__`` re-exports the full public surface (plus the
three private helpers other modules import) so existing
``from app.models.schemas import X`` imports keep working unchanged.
"""

from app.models.schemas.analysis import ConfidenceScores, SkillsValidation
from app.models.schemas.api import (
    ErrorDetail,
    HealthResponse,
    JobStatusResponse,
    ParseResponse,
    ParseUploadedRequest,
    RetryResponse,
    UploadUrlRequest,
    UploadUrlResponse,
    WebhookCreateRequest,
    WebhookResponse,
)
from app.models.schemas.batch import (
    BatchSkipped,
    BatchStatusResponse,
    BatchSubmitResponse,
)
from app.models.schemas.feedback import FeedbackRequest, FeedbackResponse
from app.models.schemas.resume import (
    CertificationItem,
    ClinicalRotation,
    ComplianceInfo,
    EducationItem,
    ExperienceItem,
    ExtractionNote,
    LicenseItem,
    ParsedResumeAI,
    PersonalInfo,
    ProjectItem,
    ReferenceItem,
    SpecialtyMatch,
)
from app.models.schemas.validators import (
    _coerce_list,
    _sanitize_date,
    _sanitize_str,
)

__all__ = [
    # validators (private helpers imported by other modules)
    "_coerce_list",
    "_sanitize_date",
    "_sanitize_str",
    # resume domain models
    "SpecialtyMatch",
    "PersonalInfo",
    "ExperienceItem",
    "EducationItem",
    "CertificationItem",
    "LicenseItem",
    "ProjectItem",
    "ReferenceItem",
    "ClinicalRotation",
    "ComplianceInfo",
    "ExtractionNote",
    "ParsedResumeAI",
    # analysis
    "ConfidenceScores",
    "SkillsValidation",
    # api DTOs
    "ParseResponse",
    "UploadUrlRequest",
    "UploadUrlResponse",
    "ParseUploadedRequest",
    "JobStatusResponse",
    "RetryResponse",
    "WebhookCreateRequest",
    "WebhookResponse",
    "ErrorDetail",
    "HealthResponse",
    # feedback
    "FeedbackRequest",
    "FeedbackResponse",
    # batch
    "BatchSkipped",
    "BatchSubmitResponse",
    "BatchStatusResponse",
]
