"""Batch submit/status DTOs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BatchSkipped(BaseModel):
    filename:  str = Field(..., description="Original filename")
    reason:    str = Field(..., description="Why this file was rejected")


class BatchJob(BaseModel):
    job_id:    str = Field(..., description="Poll this at /api/v1/resume/job/{job_id}")
    filename:  str = Field(..., description="The file this job was created for")


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
                "jobs": [{"job_id": "01J3K5M2...", "filename": "jane_smith_rn.pdf"}],
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
    jobs:           list[BatchJob]    = Field(
        default_factory=list,
        description="Accepted files paired with their job ID, so each result can be "
                    "matched back to the file it came from",
    )
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
    completed_at:   str | None = Field(None, description="ISO 8601 timestamp when all files finished")
