"""Shared DynamoDB resource/table access for the repository modules.

One cached boto3 resource per (region, endpoint); `_table()` resolves a Table by
name. The per-entity repositories (api_keys, companies, jobs, ...) build on these
instead of each re-creating the boto3 plumbing.
"""

from functools import lru_cache
from typing import Any

import boto3

from app.core.config import get_settings


@lru_cache(maxsize=4)
def _get_dynamodb_resource(region: str, endpoint_url: str) -> Any:
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.resource("dynamodb", **kwargs)


def _get_dynamodb(settings=None) -> Any:
    if settings is None:
        settings = get_settings()
    return _get_dynamodb_resource(settings.aws_region, settings.dynamodb_endpoint_url)


def _table(name: str) -> Any:
    """Return a Table by its (already-resolved) table name."""
    return _get_dynamodb().Table(name)
