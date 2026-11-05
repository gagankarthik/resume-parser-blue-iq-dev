"""
Batch processing orchestrator - local (asyncio) mode.

In production the batch endpoint enqueues one SQS message per file and the Worker
Lambda processes each. For local dev (no queue configured), this module runs all
files concurrently under a semaphore so we never flood OpenAI or Textract beyond
the configured limit.

Semaphore is module-level and created once per process.
"""

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.workers.background import process_resume_async
from app.workers.dispatch import AsyncJobPayload

log = get_logger(__name__)

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        limit = get_settings().max_concurrent_jobs
        _semaphore = asyncio.Semaphore(limit)
        log.info("batch_semaphore_created", max_concurrent=limit)
    return _semaphore


async def _process_one(sem: asyncio.Semaphore, job: AsyncJobPayload) -> None:
    """Process a single file inside the semaphore - errors are swallowed
    because process_resume_async already handles them and updates DynamoDB."""
    async with sem:
        await process_resume_async(**job)


async def process_batch_locally(batch_id: str, jobs: list[AsyncJobPayload]) -> None:
    """
    Process all jobs in a batch concurrently, bounded by MAX_CONCURRENT_JOBS.
    Called as a FastAPI BackgroundTask in development mode.
    """
    sem = _get_semaphore()
    log.info("batch_start_local", batch_id=batch_id, total=len(jobs))

    tasks = [_process_one(sem, job) for job in jobs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any unexpected exceptions that escaped process_resume_async
    for job, r in zip(jobs, results, strict=True):
        if isinstance(r, BaseException):
            log.error(
                "batch_task_escaped_error",
                batch_id=batch_id,
                job_id=job["job_id"],
                error=str(r),
            )

    log.info("batch_tasks_dispatched", batch_id=batch_id, total=len(jobs))
