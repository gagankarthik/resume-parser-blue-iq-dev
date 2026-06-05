"""
S3 temporary file storage.

Files are uploaded for processing and deleted immediately after.
We never retain raw resume files.
"""

from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _get_s3():
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return boto3.client("s3", **kwargs)


def upload_temp_file(job_id: str, filename: str, content: bytes) -> str:
    """Upload file to S3; returns the S3 key."""
    settings = get_settings()
    key = f"temp/{job_id}/{filename}"
    s3 = _get_s3()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        # Server-side encryption
        ServerSideEncryption="AES256",
    )
    log.info("s3_upload", job_id=job_id, key=key, size_bytes=len(content))
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

    Returns ``{"key", "url", "fields"}`` — the client POSTs the file to ``url``
    with ``fields`` (plus the ``file`` field) as multipart form data.
    """
    settings = get_settings()
    key = f"temp/{job_id}/{filename}"
    s3 = _get_s3()
    presigned = s3.generate_presigned_post(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Fields={"x-amz-server-side-encryption": "AES256"},
        Conditions=[
            {"x-amz-server-side-encryption": "AES256"},
            ["content-length-range", 1, max_bytes],
        ],
        ExpiresIn=expires_in,
    )
    log.info("s3_presigned_upload", job_id=job_id, key=key, expires_in=expires_in)
    return {"key": key, "url": presigned["url"], "fields": presigned["fields"]}


def download_file(s3_key: str) -> bytes:
    settings = get_settings()
    s3 = _get_s3()
    resp = s3.get_object(Bucket=settings.s3_bucket_name, Key=s3_key)
    return resp["Body"].read()


def delete_file(s3_key: str) -> None:
    """Guaranteed delete — called after processing regardless of outcome."""
    settings = get_settings()
    s3 = _get_s3()
    try:
        s3.delete_object(Bucket=settings.s3_bucket_name, Key=s3_key)
        log.info("s3_delete", key=s3_key)
    except ClientError as exc:
        # Log but never raise — deletion failure must not block the response
        log.error("s3_delete_failed", key=s3_key, error=str(exc))


def delete_job_files(job_id: str) -> None:
    """Delete all objects under temp/{job_id}/."""
    settings = get_settings()
    s3 = _get_s3()
    try:
        resp = s3.list_objects_v2(
            Bucket=settings.s3_bucket_name, Prefix=f"temp/{job_id}/"
        )
        objects = resp.get("Contents", [])
        if not objects:
            return
        s3.delete_objects(
            Bucket=settings.s3_bucket_name,
            Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
        )
        log.info("s3_delete_job_files", job_id=job_id, count=len(objects))
    except ClientError as exc:
        log.error("s3_delete_job_files_failed", job_id=job_id, error=str(exc))
