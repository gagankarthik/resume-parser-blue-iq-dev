"""Resume parsing use-case (application) layer.

Every parse request follows one uniform flow: the HTTP handler validates the file,
then `submit` stores it and dispatches it to the async worker, which runs the full
pipeline on its own budget and persists the result for the poll endpoint. Nothing
parses on the request path. The helpers here stage the job, assemble the worker
payload and enqueue it, so the HTTP handlers stay thin (validate -> submit -> respond).
"""

import asyncio

from fastapi import BackgroundTasks

from app.core.logging import get_logger
from app.db import dynamodb as db
from app.services.application import job_outcome
from app.storage import s3_client
from app.workers.background import process_resume_async
from app.workers.dispatch import AsyncJobPayload, enqueue_job

log = get_logger(__name__)


class DispatchFailedError(Exception):
    """The job was staged but could not be handed to the worker.

    By the time this is raised the job has been marked failed and its file
    deleted (best effort), so the caller only has to tell the client.
    """


def build_async_payload(
    *,
    job_id: str,
    company_id: str,
    s3_key: str,
    filename: str,
    file_size_bytes: int,
    force_textract: bool,
    record: dict,
    endpoint: str,
    batch_id: str | None = None,
) -> AsyncJobPayload:
    """Assemble the payload handed to the async worker (SQS or BackgroundTasks).

    `endpoint` is the route TEMPLATE that accepted the request (e.g.
    "POST /resume/parse"), not the concrete path. Templated keeps the cardinality
    bounded and keeps job ids out of the metric key. It rides through to the
    audit record so usage can be broken down per endpoint.
    """
    payload: AsyncJobPayload = {
        "job_id": job_id,
        "company_id": company_id,
        "s3_key": s3_key,
        "filename": filename,
        "file_size_bytes": file_size_bytes,
        "force_textract": force_textract,
        "key_hash": record["key_hash"],
        "key_prefix": record.get("key_prefix", ""),
        "endpoint": endpoint,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


async def dispatch_async(
    settings,
    background_tasks: BackgroundTasks,
    payload: AsyncJobPayload,
) -> None:
    """Enqueue an async parse job on the worker queue, or run it in-process
    (BackgroundTasks) when no queue is configured (local dev)."""
    if settings.use_queue_worker:
        if not enqueue_job(settings, payload):
            await job_outcome.fail_job(
                job_id=payload["job_id"], company_id=payload["company_id"],
                error=job_outcome.DISPATCH_FAILED, batch_id=payload.get("batch_id"),
            )
            # No worker will ever read the file - don't leave resume bytes behind.
            s3_client.delete_file(payload["s3_key"])
    else:
        background_tasks.add_task(process_resume_async, **payload)


async def submit(
    settings,
    background_tasks: BackgroundTasks,
    *,
    job_id: str,
    company_id: str,
    filename: str,
    file_size_bytes: int,
    force_textract: bool,
    record: dict,
    endpoint: str,
    content: bytes | None = None,
    s3_key: str | None = None,
    retried_from: str | None = None,
    retry_count: int = 0,
) -> None:
    """Stage one parse job and hand it to the worker - the submit path of every endpoint.

    A new upload passes `content`: the job row is created first, then the file is
    stored. A presigned upload passes `s3_key`: its row (already claimed) and its
    object exist. Whatever fails after the row exists, the job is marked failed and
    its file deleted, best effort, so a poller sees `failed` instead of
    `processing` until the TTL. A dispatch failure is raised as
    DispatchFailedError; a staging failure is re-raised unchanged.
    """
    if (content is None) == (s3_key is None):
        raise ValueError("submit needs exactly one of content or s3_key")
    row_exists = content is None
    try:
        if content is not None:
            await asyncio.to_thread(
                db.create_job, job_id, company_id,
                retried_from=retried_from, retry_count=retry_count,
            )
            row_exists = True
            s3_key = await asyncio.to_thread(s3_client.upload_temp_file, job_id, filename, content)
        payload = build_async_payload(
            job_id=job_id, company_id=company_id, s3_key=str(s3_key), filename=filename,
            file_size_bytes=file_size_bytes, force_textract=force_textract,
            record=record, endpoint=endpoint,
        )
        try:
            await dispatch_async(settings, background_tasks, payload)
        except Exception as exc:
            raise DispatchFailedError(job_id) from exc
    except Exception:
        log.exception("submit_failed", job_id=job_id)
        await asyncio.to_thread(_abandon, job_id, row_exists, s3_key)
        raise


def _abandon(job_id: str, row_exists: bool, s3_key: str | None) -> None:
    """Best-effort cleanup of a job that will never reach the worker."""
    if row_exists:
        # Only when the row exists: update_item on a missing key would create a
        # stray row with no company_id and no TTL.
        try:
            db.update_job_failed(
                job_id, job_outcome.DISPATCH_FAILED.detail,
                job_outcome.DISPATCH_FAILED.error_code.value,
            )
        except Exception:
            log.exception("abandon_job_row_failed", job_id=job_id)
    if s3_key:
        try:
            s3_client.delete_file(s3_key)
        except Exception:
            log.exception("abandon_job_file_failed", job_id=job_id)
