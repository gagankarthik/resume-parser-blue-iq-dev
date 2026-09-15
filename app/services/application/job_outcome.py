"""Terminal job outcomes: failing a job, counting it against its batch, notifying.

A batch finishes when completed + failed == total, and only this module moves
those counters. Every way a batch job can end goes through
`record_batch_outcome` - the worker finishing it either way, and the batch
endpoint failing a file that could not even be queued. Before, only the worker
counted, so a failed enqueue left its batch `processing` until the TTL and
batch.completed never fired.
"""

import asyncio

from app.core.errors import ErrorCode, PublicError
from app.core.logging import get_logger
from app.db import dynamodb as db
from app.workers import webhook_sender

log = get_logger(__name__)

# The public error of a job that was created but never reached the worker.
DISPATCH_FAILED = PublicError(
    503, ErrorCode.WORKER_DISPATCH_FAILED,
    "Background processing could not be started for this file.",
)

# Outer safety net around webhook delivery - NOT the delivery budget itself. It has to
# cover the sender's whole retry ladder (sleeps + one HTTP timeout per attempt), or it
# cancels the final attempts and the sender's circuit breaker can never open. Derived
# from the sender rather than hardcoded so tuning the ladder can't silently re-break it.
# Delivery happens after the job row is already `completed`, so a poller never waits on
# this; it only extends the worker invocation, which has 300s (and a 360s SQS visibility
# timeout) to spend. `deliver_event` fans out to all hooks concurrently, so this bounds
# the slowest hook, not their sum.
WEBHOOK_TIMEOUT = webhook_sender.DELIVERY_BUDGET_SECONDS + 15


async def deliver_safely(company_id: str, event: str, payload: dict) -> None:
    """Deliver a webhook event with an outer timeout; never raises - delivery is best-effort."""
    try:
        await asyncio.wait_for(
            webhook_sender.deliver_event(company_id, event, payload),
            timeout=WEBHOOK_TIMEOUT,
        )
    except TimeoutError:
        log.warning("webhook_delivery_timeout", event_name=event, company_id=company_id)
    except Exception as exc:
        log.warning("webhook_delivery_error", event_name=event, error=str(exc))


async def record_batch_outcome(*, batch_id: str, job_id: str, company_id: str, completed: bool) -> None:
    """Count one finished job against its batch; fire batch.completed on the last one.

    Idempotent per job - the job row carries the claim - so an SQS redelivery can
    never count a job twice. Raises when the counter write fails: the claim is
    already taken at that point, so that count is lost and the caller must say so.
    """
    if not db.mark_batch_counted(job_id):
        log.info("batch_already_counted", job_id=job_id, batch_id=batch_id)
        return
    batch_done = db.increment_batch_counter(
        batch_id=batch_id, field="completed" if completed else "failed",
    )
    if not batch_done:
        return
    batch = db.get_batch(batch_id)
    if batch:
        await deliver_safely(
            company_id, "batch.completed",
            {
                "batch_id":  batch_id,
                "total":     int(batch.get("total", 0)),
                "completed": int(batch.get("completed", 0)),
                "failed":    int(batch.get("failed", 0)),
            },
        )


async def fail_job(
    *,
    job_id: str,
    company_id: str,
    error: PublicError,
    batch_id: str | None = None,
) -> None:
    """Fail a job that will never reach the worker, and count it against its batch.

    Failing it now gives pollers a clear `failed` with a reason instead of an
    eternal `processing` that only ends when the client gives up.
    """
    db.update_job_failed(job_id, error.detail, error.error_code.value)
    if batch_id:
        await record_batch_outcome(batch_id=batch_id, job_id=job_id, company_id=company_id, completed=False)
