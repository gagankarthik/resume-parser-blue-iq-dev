import pytest
from app.services.extraction.classifier import ExtractionStrategy, classify
from app.core.exceptions import UnsupportedFileTypeError


def test_docx_classified_as_docx():
    strategy, needs_async = classify("resume.docx", b"PK\x03\x04fake-docx-content")
    assert strategy == ExtractionStrategy.DOCX
    assert needs_async is False


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.txt", b"some content")


def test_image_classified_as_ocr():
    strategy, needs_async = classify("resume.png", b"\x89PNG\r\n\x1a\n")
    assert strategy == ExtractionStrategy.OCR
    assert needs_async is True
