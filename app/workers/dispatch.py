"""
Async dispatch helper.

In the single-Lambda deployment the function invokes **itself** asynchronously
(`InvocationType="Event"`); the unified handler (`app.handlers.lambda_handler`)
detects the non-HTTP event and routes it to the OCR worker pipeline.

`settings.worker_lambda_function_name` therefore points at this same function.
Locally (no worker function configured) callers fall back to FastAPI
BackgroundTasks instead — see `use_lambda_worker`.
"""

import json

import boto3

from app.core.logging import get_logger

log = get_logger(__name__)


def invoke_worker(settings, payload: dict) -> bool:
    """Async (self-)invoke of the worker pipeline.

    Returns True when the invocation was accepted. Returns False on failure
    (e.g. an IAM AccessDeniedException on lambda:InvokeFunction) so the caller
    can mark the job FAILED immediately — otherwise the job sits in
    "processing" forever and clients poll until they give up.
    """
    client = boto3.client("lambda", region_name=settings.aws_region)
    try:
        resp = client.invoke(
            FunctionName=settings.worker_lambda_function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        if resp.get("FunctionError"):
            log.error("worker_invoke_error", job_id=payload.get("job_id"),
                      function_error=resp["FunctionError"])
            return False
        return True
    except Exception as exc:
        log.error("worker_invoke_failed", job_id=payload.get("job_id"), error=str(exc))
        return False
