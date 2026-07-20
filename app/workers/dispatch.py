"""
Async dispatch helper - enqueue parse jobs onto the worker SQS queue.

The API Lambda stays thin: it validates the upload, writes the job row, and pushes
a message (job metadata + S3 pointer) onto SQS, then returns. A separate Worker
Lambda drains the queue and runs the heavy OCR / multi-agent pipeline. SQS gives us
redelivery on transient failure, a visibility timeout that stops a still-running job
being picked up twice, a queue-depth backpressure metric, and a dead-letter queue
for poison messages - none of which self-invocation provided.

`settings.worker_queue_url` points at that queue. Locally (no queue configured)
callers fall back to FastAPI BackgroundTasks - see `use_queue_worker`.
"""

import json

import boto3

from app.core.logging import get_logger

log = get_logger(__name__)

# SQS SendMessageBatch accepts at most 10 entries per call.
_SQS_BATCH_LIMIT = 10


def _sqs_client(settings):
    return boto3.client("sqs", region_name=settings.aws_region)


def enqueue_job(settings, payload: dict) -> bool:
    """Push one async parse job onto the worker queue.

    Returns True when the message was accepted. Returns False on failure (e.g. an
    IAM AccessDeniedException on sqs:SendMessage) so the caller can mark the job
    FAILED immediately - otherwise the job sits in "processing" forever and clients
    poll until they give up.
    """
    try:
        _sqs_client(settings).send_message(
            QueueUrl=settings.worker_queue_url,
            MessageBody=json.dumps(payload),
        )
        return True
    except Exception as exc:
        log.error("worker_enqueue_failed", job_id=payload.get("job_id"), error=str(exc))
        return False


def enqueue_jobs(settings, payloads: list[dict]) -> set[str]:
    """Push many async parse jobs onto the worker queue via SendMessageBatch.

    Batches the sends in chunks of 10 (the SQS limit) so a large upload becomes a
    handful of API calls instead of one per file. Returns the set of job_ids that
    FAILED to enqueue (empty on full success) so the caller can fail exactly those
    jobs and let the rest proceed. A whole-chunk transport error fails every job_id
    in that chunk.
    """
    failed: set[str] = set()
    client = _sqs_client(settings)

    for start in range(0, len(payloads), _SQS_BATCH_LIMIT):
        chunk = payloads[start : start + _SQS_BATCH_LIMIT]
        # Entry Ids must be unique within a batch and are echoed back in the
        # response; index-in-chunk keeps them unique and maps straight to the job.
        entries = [
            {"Id": str(i), "MessageBody": json.dumps(job)}
            for i, job in enumerate(chunk)
        ]
        try:
            resp = client.send_message_batch(
                QueueUrl=settings.worker_queue_url,
                Entries=entries,
            )
        except Exception as exc:
            log.error("worker_enqueue_batch_failed",
                      count=len(chunk), error=str(exc))
            failed.update(job.get("job_id") for job in chunk)
            continue

        for item in resp.get("Failed", []):
            job = chunk[int(item["Id"])]
            log.error("worker_enqueue_entry_failed",
                      job_id=job.get("job_id"), code=item.get("Code"))
            failed.add(job.get("job_id"))

    return failed
