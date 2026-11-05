import io
import zipfile

import fitz
import pytest

from app.core.exceptions import UnsupportedFileTypeError
from app.services.extraction.classifier import ExtractionStrategy, classify


def _minimal_docx() -> bytes:
    """A minimal but structurally-valid .docx (OOXML zip with the required members)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
    return buf.getvalue()


def _pdf(*pages: str) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_docx_classified_as_docx():
    assert classify("resume.docx", _minimal_docx()) == ExtractionStrategy.DOCX


def test_fake_docx_zip_rejected():
    # A generic/zip-bomb file renamed .docx (valid ZIP header, no OOXML structure)
    # must be rejected, not classified as DOCX.
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.docx", b"PK\x03\x04not-a-real-ooxml-zip")


def test_rtf_classified_as_rtf():
    assert classify("resume.rtf", b"{\\rtf1\\ansi Hello}") == ExtractionStrategy.RTF


def test_rtf_extension_with_wrong_content_raises():
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.rtf", b"%PDF-1.7 not actually rtf")


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileTypeError):
        classify("resume.txt", b"some content")


def test_image_classified_as_ocr():
    assert classify("resume.png", b"\x89PNG\r\n\x1a\n") == ExtractionStrategy.OCR


def test_pdf_with_a_text_layer_is_digital_even_when_split_across_pages():
    line = "Registered Nurse, Intensive Care Unit, Mercy Hospital"  # ~55 chars per page
    assert classify("resume.pdf", _pdf(line)) == ExtractionStrategy.OCR
    assert classify("resume.pdf", _pdf(line, line)) == ExtractionStrategy.PDF


def test_pdf_without_a_text_layer_goes_to_ocr():
    assert classify("resume.pdf", _pdf("", "")) == ExtractionStrategy.OCR
