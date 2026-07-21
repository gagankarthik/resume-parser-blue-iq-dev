"""Resume parsing use-case (application) layer.

Every parse request follows one uniform flow: the HTTP handler validates and stores
the file, then dispatches it to the async worker (this module), which runs the full
pipeline on its own budget and persists the result for the poll endpoint. Nothing
parses on the request path. The helpers here assemble the worker payload and enqueue
it, so the HTTP handlers stay thin (validate -> store -> dispatch -> respond).
"""

from fastapi import BackgroundTasks

from app.core.errors import ErrorCode
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.workers.background import process_resume_async
from app.workers.dispatch import enqueue_job

log = get_logger(__name__)


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
    """Enqueue an async parse job on the worker queue, or run it in-process
    (BackgroundTasks) when no queue is configured (local dev)."""
    if settings.use_queue_worker:
        if not enqueue_job(settings, payload):
            # Fail the job NOW so pollers get a clear "failed" with a reason,
            # not an eternal "processing" that only ends when the client gives up.
            db.update_job_failed(
                payload["job_id"],
                "Background processing could not be started for this file.",
                ErrorCode.WORKER_DISPATCH_FAILED.value,
            )
    else:
        background_tasks.add_task(process_resume_async, **payload)
