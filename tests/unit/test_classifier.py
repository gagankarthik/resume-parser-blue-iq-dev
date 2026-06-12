import pytest

from app.core.exceptions import UnsupportedFileTypeError
from app.services.extraction.classifier import ExtractionStrategy, classify


def test_docx_classified_as_docx():
    strategy, needs_async = classify("resume.docx", b"PK\x03\x04fake-docx-content")
    assert strategy == ExtractionStrategy.DOCX
    assert needs_async is False


def test_rtf_classified_as_rtf():
    strategy, needs_async = classify("resume.rtf", b"{\\rtf1\\ansi Hello}")
    assert strategy == ExtractionStrategy.RTF
    assert needs_async is False


def test_rtf_extension_with_wrong_content_raises():
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.rtf", b"%PDF-1.7 not actually rtf")


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.txt", b"some content")


def test_image_classified_as_ocr():
    strategy, needs_async = classify("resume.png", b"\x89PNG\r\n\x1a\n")
    assert strategy == ExtractionStrategy.OCR
    assert needs_async is True
