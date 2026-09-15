"""Regression tests for the API/core hardening pass: public error mapping, auth
token comparison, rate-limit memory bound, upload validation, SSRF address checks,
S3 key construction, hints and production config warnings.

Every test here pins a failure that was reachable in production before the fix.
"""

import io
import zipfile

import pytest
from botocore.exceptions import ClientError

from app.core import file_validator, rate_limit, url_validator
from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, get_hint, public_error
from app.core.exceptions import (
    ExtractionError,
    FileValidationError,
    NotAResumeError,
    OCRError,
    UnsupportedFileTypeError,
)
from app.core.security import create_account_token, verify_account_token

# -- A6: one public mapping from an exception to what a client may see -----------

@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (NotAResumeError("The document is empty."), 422, ErrorCode.NOT_A_RESUME),
        (OCRError("scan unreadable"), 422, ErrorCode.OCR_FAILED),
        (ExtractionError("encrypted pdf"), 422, ErrorCode.EXTRACTION_FAILED),
        (UnsupportedFileTypeError("bad magic"), 415, ErrorCode.UNSUPPORTED_FILE_TYPE),
        (FileValidationError("bad"), 415, ErrorCode.UNSUPPORTED_FILE_TYPE),
        (TimeoutError(), 504, ErrorCode.PARSE_TIMEOUT),
    ],
)
def test_public_error_maps_domain_errors(exc, status, code):
    pe = public_error(exc)
    assert (pe.status_code, pe.error_code) == (status, code)


def test_public_error_keeps_client_facing_messages():
    assert public_error(NotAResumeError("The document is empty.")).detail == "The document is empty."


def test_public_error_never_leaks_internal_text():
    """botocore / OpenAI messages carry bucket names, ARNs and request ids."""
    exc = ClientError(
        {"Error": {"Code": "AccessDenied",
                   "Message": "arn:aws:iam::123456789012:role/x is not authorized on resume-parser-blue-iq-temp"}},
        "GetObject",
    )
    pe = public_error(exc)
    assert pe.error_code == ErrorCode.PARSE_FAILED
    assert pe.status_code == 500
    assert "arn:aws" not in pe.detail and "resume-parser" not in pe.detail
    assert "ClientError" not in pe.detail


def test_not_a_resume_error_code_is_the_registry_member():
    assert NotAResumeError.error_code is ErrorCode.NOT_A_RESUME


# -- A8: non-ASCII tokens are a 401, never a TypeError/500 -------------------------

def test_account_token_with_non_ascii_signature_is_rejected_not_raised():
    token = create_account_token("acme", "secret")
    raw = token.split(".")[0]
    assert verify_account_token(f"{raw}.ééé", "secret") is None


# -- M3: the limiter's memory stays bounded inside one window ---------------------

def test_rate_limit_map_is_bounded_when_every_entry_is_current(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 100)
    monkeypatch.setattr(rate_limit, "_MAX_TRACKED", 5)

    for i in range(50):
        rate_limit.check(f"key-{i}")

    assert len(rate_limit._WINDOWS) <= 5


def test_rate_limit_still_counts_within_the_bound(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    rate_limit.check("k")
    rate_limit.check("k")
    with pytest.raises(Exception) as ei:
        rate_limit.check("k")
    assert getattr(ei.value, "status_code", None) == 429


# -- M1: a DOCX zip bomb is rejected from the central directory -------------------

def _docx(document_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def test_docx_that_expands_past_the_cap_is_rejected(monkeypatch):
    monkeypatch.setattr(file_validator, "_MAX_DOCX_UNCOMPRESSED_BYTES", 1024 * 1024)
    bomb = _docx(b"\x00" * (4 * 1024 * 1024))
    assert len(bomb) < 64 * 1024  # tiny on the wire
    with pytest.raises(UnsupportedFileTypeError, match="expands"):
        file_validator.validate_file("resume.docx", bomb)


def test_docx_with_an_extreme_compression_ratio_is_rejected(monkeypatch):
    monkeypatch.setattr(file_validator, "_MAX_DOCX_RATIO_FLOOR_BYTES", 1024 * 1024)
    bomb = _docx(b"\x00" * (3 * 1024 * 1024))
    with pytest.raises(UnsupportedFileTypeError, match="expands"):
        file_validator.validate_file("resume.docx", bomb)


def test_ordinary_docx_still_validates():
    assert file_validator.validate_file("resume.docx", _docx(b"<w:document>Jane RN</w:document>")) == "docx"


# -- C1: one upload check for every endpoint ---------------------------------------

def test_check_upload_rejections():
    empty = file_validator.check_upload("cv.pdf", b"  \n", 1024)
    assert empty is not None and (empty.status_code, empty.error_code) == (422, ErrorCode.NOT_A_RESUME)

    big = file_validator.check_upload("cv.pdf", b"%PDF" + b"x" * 2048, 1024)
    assert big is not None and (big.status_code, big.error_code) == (413, ErrorCode.FILE_TOO_LARGE)

    wrong = file_validator.check_upload("cv.txt", b"hello", 1024)
    assert wrong is not None and (wrong.status_code, wrong.error_code) == (415, ErrorCode.UNSUPPORTED_FILE_TYPE)

    assert file_validator.check_upload("cv.pdf", b"%PDF-1.4", 1024) is None


# -- L1/L2: filenames are bounded and never become part of an S3 key --------------

def test_clamp_filename_keeps_the_extension():
    name = file_validator.clamp_filename("a" * 400 + ".pdf")
    assert len(name) <= file_validator.MAX_FILENAME_LENGTH
    assert name.endswith(".pdf")


def test_s3_key_never_contains_the_filename(monkeypatch):
    from app.storage import s3_client

    puts: list[dict] = []
    logged: list[dict] = []

    class _S3:
        def put_object(self, **kw):
            puts.append(kw)

        def generate_presigned_post(self, **kw):
            return {"url": "https://bucket/", "fields": {"key": kw["Key"]}}

    class _Log:
        def info(self, event, **kw):
            logged.append(kw)

        error = warning = info

    monkeypatch.setattr(s3_client, "_get_s3", lambda: _S3())
    monkeypatch.setattr(s3_client, "log", _Log())

    key = s3_client.upload_temp_file("J1", "Jane Doe RN/../résumé.PDF", b"%PDF")
    presigned = s3_client.create_presigned_upload("J2", "Jane Doe RN.docx", 1024)

    assert key == "temp/J1/source.pdf"
    assert puts[0]["Key"] == "temp/J1/source.pdf"
    assert presigned["key"] == "temp/J2/source.docx"
    assert "Jane" not in repr(logged)


# -- M2(b): shared address space (CGNAT) is not public ----------------------------

@pytest.mark.parametrize("ip", ["100.64.0.1", "::ffff:127.0.0.1", "192.0.2.10", "224.0.0.1"])
def test_non_global_addresses_are_not_public(ip):
    assert url_validator._is_public_ip(ip) is False


def test_public_address_is_public():
    assert url_validator._is_public_ip("93.184.216.34") is True


def test_cgnat_webhook_url_is_rejected():
    with pytest.raises(url_validator.UnsafeWebhookURLError):
        url_validator.validate_webhook_url("http://100.64.1.1/hook")


# -- C7: hints quote the configured limits, not hardcoded numbers -----------------

def test_hints_follow_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_file_size_mb", 25)
    monkeypatch.setattr(settings, "max_batch_size", 50)
    monkeypatch.setattr(settings, "presigned_upload_expiry_seconds", 600)
    assert "25 MB" in get_hint(ErrorCode.FILE_TOO_LARGE)
    assert "50 files" in get_hint(ErrorCode.BATCH_TOO_LARGE)
    assert "10 minutes" in get_hint(ErrorCode.UPLOAD_NOT_FOUND)
    # A literal path placeholder in another hint is left alone.
    assert "{job_id}" in get_hint(ErrorCode.PARSE_FAILED)


# -- L4: a weak admin token is reported, never fatal ------------------------------

def test_short_admin_token_is_a_production_warning():
    s = Settings(
        environment="production",
        auth_secret="0f3c-strong-random-secret-value",
        worker_queue_url="https://sqs.us-east-2.amazonaws.com/1/q",
        admin_api_token="short",
    )
    s.assert_production_ready()  # must not raise
    assert any("ADMIN_API_TOKEN" in w for w in s.production_config_warnings())


def test_strong_admin_token_is_not_a_warning():
    s = Settings(
        environment="production",
        auth_secret="0f3c-strong-random-secret-value",
        worker_queue_url="https://sqs.us-east-2.amazonaws.com/1/q",
        admin_api_token="x" * 48,
    )
    assert s.production_config_warnings() == []


# -- Shared AWS clients fail fast instead of botocore's 60s defaults ------------

def test_shared_dynamodb_and_s3_clients_have_short_timeouts(monkeypatch):
    from app.db import _client
    from app.storage import s3_client

    # Building a client resolves credentials; keep that off the developer's profile.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    _client._get_dynamodb_resource.cache_clear()
    s3_client._s3_client.cache_clear()
    try:
        ddb = _client._get_dynamodb_resource("us-east-2", "").meta.client.meta.config
        s3 = s3_client._s3_client("us-east-2", "").meta.config
        for config in (ddb, s3):
            assert config.connect_timeout <= 5
            assert config.read_timeout <= 30
            assert config.retries["total_max_attempts"] <= 3
    finally:
        _client._get_dynamodb_resource.cache_clear()
        s3_client._s3_client.cache_clear()


def test_not_a_resume_error_declares_tokens():
    assert NotAResumeError("x").tokens == 0


# -- A1: a lost batch-counter write is an error, not a silent "not done" ----------

def test_increment_batch_counter_propagates_client_errors(monkeypatch):
    from app.db import batches

    class _Table:
        def update_item(self, **kw):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
                              "UpdateItem")

    monkeypatch.setattr(batches, "_batch_table", lambda settings=None: _Table())
    with pytest.raises(ClientError):
        batches.increment_batch_counter("B1", "failed")
