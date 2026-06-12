"""
Detect file type and choose the extraction strategy.

Validation order:
  1. Extension check
  2. Magic bytes check (prevents type-spoofing)
  3. PDF text density check (digital vs scanned)

Strategy:
  PDF with extractable text  → pdf_extractor (sync)
  PDF with no/little text    → ocr_extractor (async — Tesseract → Textract)
  DOCX                       → docx_extractor (sync)
  RTF                        → rtf_extractor (sync)
  Image (PNG/JPG/TIFF/WEBP)  → ocr_extractor (async)
"""

from enum import Enum

import fitz  # PyMuPDF

from app.core.file_validator import validate_file

# Minimum extractable text chars to classify a PDF as digital (not scanned)
_PDF_TEXT_THRESHOLD = 100


class ExtractionStrategy(str, Enum):
    PDF  = "pdf"
    DOCX = "docx"
    RTF  = "rtf"
    OCR  = "ocr"


def classify(filename: str, content: bytes) -> tuple[ExtractionStrategy, bool]:
    """
    Validate and classify the file.
    Returns (strategy, needs_async).
    Raises UnsupportedFileTypeError for unknown/spoofed files.
    """
    file_type = validate_file(filename, content)   # raises on invalid

    if file_type == "docx":
        return ExtractionStrategy.DOCX, False

    if file_type == "rtf":
        return ExtractionStrategy.RTF, False

    if file_type == "pdf":
        return _classify_pdf(content)

    # png / jpeg / tiff / webp → OCR, requires async
    return ExtractionStrategy.OCR, True


def _classify_pdf(content: bytes) -> tuple[ExtractionStrategy, bool]:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        total_text = "".join(page.get_text() for page in doc)
        doc.close()
        if len(total_text.strip()) >= _PDF_TEXT_THRESHOLD:
            return ExtractionStrategy.PDF, False
        return ExtractionStrategy.OCR, True
    except Exception:
        return ExtractionStrategy.OCR, True
