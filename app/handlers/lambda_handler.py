"""
AWS Lambda entry point — API Lambda.

Mangum wraps the FastAPI ASGI app to handle API Gateway HTTP events.
Deploy this file as the Lambda handler: app.handlers.lambda_handler.handler

Environment variables required (set in Lambda console or SAM/CDK):
  AWS_REGION, OPENAI_API_KEY, S3_BUCKET_NAME,
  DYNAMODB_TABLE_API_KEYS, DYNAMODB_TABLE_RATE_LIMITS,
  DYNAMODB_TABLE_JOBS, DYNAMODB_TABLE_WEBHOOKS, DYNAMODB_TABLE_AUDIT_LOGS,
  WORKER_LAMBDA_FUNCTION_NAME, ENVIRONMENT=production
"""

from app.core.logging import configure_logging

# Configure logging at cold-start (lifespan="off" skips the FastAPI startup event)
configure_logging()

from mangum import Mangum  # noqa: E402

from app.main import app  # noqa: E402

# lifespan="off" — Lambda has no persistent process, skip startup/shutdown events
handler = Mangum(app, lifespan="off")
