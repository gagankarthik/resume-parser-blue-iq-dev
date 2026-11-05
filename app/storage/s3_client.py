"""
S3 temporary file storage.

Files are uploaded for processing and deleted immediately after.
We never retain raw resume files.

Object keys are `temp/<job_id>/source<.ext>` - never the client's filename.
Resume filenames embed candidate names (PII that the key would carry into S3
access logs and our own logs), can exceed S3's 1024-byte key limit, and a "/" in
one would nest the key. The original filename travels in the job row and the
worker payload instead.
"""

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.file_validator import storage_extension
from app.core.logging import get_logger

log = get_logger(__name__)

# botocore defaults to 60s connect/read timeouts plus retries. Objects here are at
# most max_file_size_mb and move within the region, so a read that stalls this long
# is a dead connection, not a slow upload.
_S3_CONFIG = Config(
    connect_timeout=2,
    read_timeout=20,
    retries={"total_max_attempts": 3, "mode": "standard"},
)


@lru_cache(maxsize=8)
def _s3_client(region: str, endpoint_url: str):
    """Build (once per region/endpoint) and cache the boto3 S3 client.

    boto3 clients are thread-safe and expensive to construct (session/config/
    endpoint resolution). Caching mirrors the DynamoDB resource in db/_client.py
    and keeps client creation off the per-request hot path (every parse + every
    batch file previously built a fresh client).
    """
    kwargs: dict[str, Any] = {"region_name": region, "config": _S3_CONFIG}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **kwargs)


def _get_s3():
    settings = get_settings()
    return _s3_client(settings.aws_region, settings.s3_endpoint_url)


def _temp_key(job_id: str, filename: str) -> str:
    return f"temp/{job_id}/source{storage_extension(filename)}"


def upload_temp_file(job_id: str, filename: str, content: bytes) -> str:
    """Upload file to S3; returns the S3 key."""
    settings = get_settings()
    key = _temp_key(job_id, filename)
    _get_s3().put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        # Server-side encryption
        ServerSideEncryption="AES256",
    )
    log.info("s3_upload", job_id=job_id, extension=storage_extension(filename),
             size_bytes=len(content))
    return key


def create_presigned_upload(
    job_id: str,
    filename: str,
    max_bytes: int,
    expires_in: int = 900,
) -> dict:
    """Issue a presigned POST so the client can upload straight to S3.

    This bypasses the API's ~6 MB request cap (the Lambda Function URL limit) and
    lets clients send the full max file size. The ``content-length-range``
    condition makes S3 itself reject anything larger than ``max_bytes``, and
    server-side encryption is enforced via the policy.

    Returns ``{"key", "url", "fields"}`` - the client POSTs the file to ``url``
    with ``fields`` (plus the ``file`` field) as multipart form data.
    """
    settings = get_settings()
    key = _temp_key(job_id, filename)
    presigned = _get_s3().generate_presigned_post(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Fields={"x-amz-server-side-encryption": "AES256"},
        Conditions=[
            {"x-amz-server-side-encryption": "AES256"},
            ["content-length-range", 1, max_bytes],
        ],
        ExpiresIn=expires_in,
    )
    log.info("s3_presigned_upload", job_id=job_id, extension=storage_extension(filename),
             expires_in=expires_in)
    return {"key": key, "url": presigned["url"], "fields": presigned["fields"]}


def download_file(s3_key: str) -> bytes:
    settings = get_settings()
    resp = _get_s3().get_object(Bucket=settings.s3_bucket_name, Key=s3_key)
    return resp["Body"].read()


def delete_file(s3_key: str) -> None:
    """Guaranteed delete - called after processing regardless of outcome."""
    settings = get_settings()
    try:
        _get_s3().delete_object(Bucket=settings.s3_bucket_name, Key=s3_key)
    except ClientError:
        # Log but never raise - deletion failure must not block the response.
        # No key in the log: keys issued before the source<.ext> scheme embed a filename.
        log.exception("s3_delete_failed")
