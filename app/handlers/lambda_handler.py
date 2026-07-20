"""
API Lambda entry point (also the fallback worker router).

Primary role: serve the HTTP API - Lambda Function URL events -> FastAPI (Mangum).
Async parse work runs on a *separate* Worker Lambda that drains the SQS queue
(`app.handlers.worker_lambda.handler`); the API function only enqueues.

Deploy this file as the API Lambda handler: app.handlers.lambda_handler.handler

Routing: Function URL / API Gateway events carry "rawPath"/"requestContext" and go
to FastAPI. Anything else (an SQS batch event, or a plain job dict) is handed to the
worker pipeline - so this handler still works if it is ever wired to the queue, but
in the normal topology only the dedicated Worker Lambda receives those events.

Environment variables (set in Terraform):
  AWS_REGION, OPENAI_API_KEY, S3_BUCKET_NAME,
  DYNAMODB_TABLE_*, WORKER_QUEUE_URL (SQS queue the API enqueues onto),
  ENVIRONMENT=production
"""

from typing import Any

from app.core.logging import configure_logging, get_logger

# Configure logging at cold-start (lifespan="off" skips the FastAPI startup event)
configure_logging()
log = get_logger(__name__)

from mangum import Mangum  # noqa: E402

from app.handlers.worker_lambda import handler as _worker_handler  # noqa: E402
from app.main import app  # noqa: E402

# lifespan="off" - Lambda has no persistent process, skip startup/shutdown events
_http_handler = Mangum(app, lifespan="off")


def handler(event: Any, context: Any) -> Any:
    """Route HTTP events to FastAPI and async job/SQS events to the worker pipeline."""
    if isinstance(event, dict) and ("rawPath" in event or "requestContext" in event):
        return _http_handler(event, context)
    return _worker_handler(event, context)
