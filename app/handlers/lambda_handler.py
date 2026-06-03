"""
Unified AWS Lambda entry point — a single function serves both roles:

  • HTTP API     — Lambda Function URL events → FastAPI (via Mangum)
  • Async worker — self-invoked events (InvocationType="Event") → OCR pipeline

Deploy this file as the Lambda handler: app.handlers.lambda_handler.handler

Routing: Function URL / API Gateway events carry "rawPath"/"requestContext".
Async worker events are plain job dicts (job_id, s3_key, …), so anything that
isn't an HTTP event is handed to the worker pipeline.

Environment variables (set in Terraform):
  AWS_REGION, OPENAI_API_KEY, S3_BUCKET_NAME,
  DYNAMODB_TABLE_*, WORKER_LAMBDA_FUNCTION_NAME (= this function's own name),
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

# lifespan="off" — Lambda has no persistent process, skip startup/shutdown events
_http_handler = Mangum(app, lifespan="off")


def handler(event: Any, context: Any) -> Any:
    """Route HTTP events to FastAPI and async job events to the worker pipeline."""
    if isinstance(event, dict) and ("rawPath" in event or "requestContext" in event):
        return _http_handler(event, context)
    return _worker_handler(event, context)
