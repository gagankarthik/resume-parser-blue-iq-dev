"""
Health check endpoint with dependency probes.

GET /api/v1/health
  No authentication required.
  Checks DynamoDB and S3 reachability in < 3 seconds.
  Returns degraded status if any dependency is unreachable.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import boto3
from fastapi import APIRouter

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import HealthResponse

router = APIRouter()
log = get_logger(__name__)

_PROBE_TIMEOUT_SECONDS = 3.0


async def _probe(dependency: str, fn: Callable[[], Any]) -> str:
    """Run a blocking boto3 probe off the event loop; never raise.

    Returns "ok" or "unreachable". The real exception is logged (not exposed in
    the response) so a degraded health check is diagnosable from CloudWatch
    rather than a silent "unreachable".
    """
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, fn), timeout=_PROBE_TIMEOUT_SECONDS
        )
        return "ok"
    except Exception as exc:
        log.warning(
            "health_dependency_unreachable",
            dependency=dependency,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return "unreachable"


async def _check_dynamodb(settings) -> str:
    def fn() -> None:
        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.dynamodb_endpoint_url:
            kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
        db = boto3.client("dynamodb", **kwargs)
        # DescribeTable (not ListTables) - covered by the least-privilege role
        # and validates the table the app actually depends on.
        db.describe_table(TableName=settings.dynamodb_table_api_keys)

    return await _probe("dynamodb", fn)


async def _check_s3(settings) -> str:
    def fn() -> None:
        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url
        s3 = boto3.client("s3", **kwargs)
        s3.head_bucket(Bucket=settings.s3_bucket_name)

    return await _probe("s3", fn)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns service health and dependency status. No authentication required.",
    tags=["System"],
)
async def health_check() -> HealthResponse:
    settings = get_settings()
    start = time.monotonic()

    dynamodb_status, s3_status = await asyncio.gather(
        _check_dynamodb(settings),
        _check_s3(settings),
    )

    all_ok  = dynamodb_status == "ok" and s3_status == "ok"
    latency = int((time.monotonic() - start) * 1000)

    return HealthResponse(
        status="ok" if all_ok else "degraded",
        version=settings.app_version,
        environment=settings.environment,
        latency_ms=latency,
        dependencies={"dynamodb": dynamodb_status, "s3": s3_status},
    )
