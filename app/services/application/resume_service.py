"""Resume parsing use-case (application) layer.

Orchestration shared by the resume endpoints — running the pipeline, writing the
usage audit log, shaping the persisted job result, and dispatching async work —
lives here so the HTTP handlers stay thin (validate → delegate → respond) and the
orchestration is defined once instead of being repeated per route.

None of this changes behavior: each helper is the exact call the endpoints made
inline, just named and shared.
"""

from fastapi import BackgroundTasks

from app.core.errors import ErrorCode
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.pipeline import PipelineInput, PipelineResult
from app.services.pipeline import run as run_pipeline
from app.workers.background import process_resume_async
from app.workers.dispatch import invoke_worker

log = get_logger(__name__)


def terminal_status(result: PipelineResult) -> str:
    """Client-facing terminal status for a finished parse.

    Returns "partial" when the parse degraded — `result.parsed` holds only what
    rule-based extraction could recover (contact anchors) and the record needs
    human review. Returns "completed" only for a clean parse. A partial parse
    must never be reported as "completed"; consumers gate ingestion on this.
    """
    return "partial" if result.partial else "completed"


async def run_parse(
    *,
    job_id: str,
    filename: str,
    content: bytes,
    company_id: str,
    force_textract: bool,
) -> PipelineResult:
    """Run the full parsing pipeline for one file."""
    return await run_pipeline(
        PipelineInput(
            job_id=job_id,
            filename=filename,
            content=content,
            company_id=company_id,
            force_textract=force_textract,
        )
    )


def audit_parse(
    *,
    job_id: str,
    company_id: str,
    result: PipelineResult,
    file_size_bytes: int,
    record: dict,
    status: str = "completed",
) -> None:
    """Write the usage/audit record for a completed parse (never stores content)."""
    db.write_audit_log(
        job_id=job_id,
        company_id=company_id,
        file_type=result.file_type,
        file_size_bytes=file_size_bytes,
        status=status,
        duration_ms=result.duration_ms,
        ocr_used=result.ocr_used,
        ai_tokens_used=result.ai_tokens_used,
        key_hash=record["key_hash"],
        key_prefix=record.get("key_prefix", ""),
    )


def result_record(result: PipelineResult) -> dict:
    """Shape a pipeline result for persistence on the job record."""
    return {
        "data": result.parsed.model_dump(),
        "confidence": result.confidence.model_dump(),
        "partial": result.partial,
        "warnings": result.warnings,
    }


def build_async_payload(
    *,
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
    file_size_bytes: int,
    force_textract: bool,
    record: dict,
) -> dict:
    """Assemble the payload handed to the async worker (Lambda or BackgroundTasks)."""
    return {
        "job_id": job_id,
        "company_id": company_id,
        "s3_key": s3_key,
        "filename": filename,
        "file_size_bytes": file_size_bytes,
        "force_textract": force_textract,
        "key_hash": record["key_hash"],
        "key_prefix": record.get("key_prefix", ""),
    }


async def dispatch_async(
    settings,
    background_tasks: BackgroundTasks,
    payload: dict,
) -> None:
    """Hand an async parse job to the Lambda worker, or to in-process BackgroundTasks."""
    if settings.use_lambda_worker:
        if not invoke_worker(settings, payload):
            # Fail the job NOW so pollers get a clear "failed" with a reason,
            # not an eternal "processing" that only ends when the client gives up.
            db.update_job_failed(
                payload["job_id"],
                "Background processing could not be started for this file.",
                ErrorCode.WORKER_DISPATCH_FAILED.value,
            )
    else:
        background_tasks.add_task(process_resume_async, **payload)
