"""Feedback request/response DTOs (parser corrections for model improvement)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Reject feedback whose JSON would overflow a single DynamoDB item (400 KB hard
# limit). Parser output is normally a few KB, so this only catches abuse.
_MAX_FEEDBACK_JSON_BYTES = 350_000


class FeedbackRequest(BaseModel):
    """
    Request body for POST /api/v1/resume/{job_id}/feedback.

    Sent server-to-server after a user reviews and corrects a parsed resume.
    Both `original` and `updated` are stored verbatim (free-form JSON, not
    re-sanitised) so the corrections retain full fidelity for model improvement.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "original": {"personal_info": {"full_name": "Jane Smith"}, "skills": ["ICU"]},
                "updated": {"personal_info": {"full_name": "Jane A. Smith, RN"}, "skills": ["ICU", "CCRN"]},
                "changed": True,
                "profile_id": "gig-profile-8821",
                "notes": "User fixed the name suffix and added a certification.",
            }
        }
    )

    original: dict[str, Any] = Field(
        ..., description="The original parser JSON, exactly as returned by /resume/parse"
    )
    updated: dict[str, Any] = Field(
        ..., description="The user-corrected JSON (post-review)"
    )
    changed: bool | None = Field(
        None,
        description="Whether the user changed anything. If omitted, it is derived from the diff.",
    )
    profile_id: str | None = Field(
        None, max_length=200, description="Optional client-side profile/record identifier"
    )
    notes: str | None = Field(
        None, max_length=2000, description="Optional free-form reviewer notes"
    )

    @model_validator(mode="after")
    def _check_size(self) -> FeedbackRequest:
        import json

        size = len(json.dumps({"original": self.original, "updated": self.updated}).encode())
        if size > _MAX_FEEDBACK_JSON_BYTES:
            raise ValueError(
                f"Feedback payload too large ({size} bytes); "
                f"limit is {_MAX_FEEDBACK_JSON_BYTES} bytes"
            )
        return self


class FeedbackResponse(BaseModel):
    """Response for POST /api/v1/resume/{job_id}/feedback (accepted asynchronously)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "feedback_id": "01J3K5M9N1P3Q5R7S9T1U3V5W7",
                "job_id": "01J3K5M2N4P6Q8R0S2T4U6V8W0",
                "status": "accepted",
                "changed": True,
                "changed_fields": ["personal_info.full_name", "skills[1]"],
                "created_at": "2026-06-03T12:34:56.000000+00:00",
            }
        }
    )

    feedback_id:    str       = Field(..., description="Unique identifier for this feedback record (ULID)")
    job_id:         str       = Field(..., description="The parse job this feedback relates to")
    status:         str       = Field(..., description="Always 'accepted' — feedback is processed asynchronously")
    changed:        bool      = Field(..., description="Whether any field differed between original and updated")
    changed_fields: list[str] = Field(..., description="Dotted paths of the leaf fields that changed")
    created_at:     str       = Field(..., description="ISO 8601 timestamp the feedback was recorded")

