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
from functools import lru_cache
from typing import Any, Literal

import boto3
from botocore.config import Config
from fastapi import APIRouter

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import HealthResponse

router = APIRouter()
log = get_logger(__name__)

_PROBE_TIMEOUT_SECONDS = 3.0

# A probe must give up on its own inside _PROBE_TIMEOUT_SECONDS. asyncio.wait_for
# stops *waiting* at the timeout but cannot stop the boto3 call on its thread, which
# with botocore's defaults (60s connect, 60s read, retries) would keep going long
# after the response - one stranded thread per health check against a dead endpoint.
_PROBE_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 1})


@lru_cache(maxsize=4)
def _client(service: Literal["dynamodb", "s3"], region: str, endpoint_url: str) -> Any:
    """Probe clients, built once per (service, region, endpoint) - not per check."""
    kwargs: dict[str, Any] = {"region_name": region, "config": _PROBE_CONFIG}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client(service, **kwargs)


async def _probe(dependency: str, fn: Callable[[], Any]) -> str:
    """Run a blocking boto3 probe off the event loop; never raise.

    Returns "ok" or "unreachable". The real exception is logged (not exposed in
    the response) so a degraded health check is diagnosable from CloudWatch
    rather than a silent "unreachable".
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(fn), timeout=_PROBE_TIMEOUT_SECONDS)
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
        db = _client("dynamodb", settings.aws_region, settings.dynamodb_endpoint_url)
        # DescribeTable (not ListTables) - covered by the least-privilege role
        # and validates the table the app actually depends on.
        db.describe_table(TableName=settings.dynamodb_table_api_keys)

    return await _probe("dynamodb", fn)


async def _check_s3(settings) -> str:
    def fn() -> None:
        s3 = _client("s3", settings.aws_region, settings.s3_endpoint_url)
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

    # Which dispatch path async parses actually take. "in-process" means
    # WORKER_QUEUE_URL is unset and parsing runs on the request thread, holding the
    # HTTP response open for the whole parse - correct for local dev, an outage of
    # the async contract anywhere else. Surfaced here so the mode is verifiable from
    # outside instead of only inferable from response latency.
    worker_status = "queue" if settings.use_queue_worker else "in-process"

    all_ok = (
        dynamodb_status == "ok"
        and s3_status == "ok"
        and (settings.use_queue_worker or not settings.is_production)
    )
    latency = int((time.monotonic() - start) * 1000)

    return HealthResponse(
        status="ok" if all_ok else "degraded",
        version=settings.app_version,
        environment=settings.environment,
        latency_ms=latency,
        dependencies={
            "dynamodb": dynamodb_status,
            "s3": s3_status,
            "worker": worker_status,
        },
    )
