"""
Detect file type and choose the extraction strategy.

Validation order:
  1. Extension check
  2. Magic bytes check (prevents type-spoofing)
  3. PDF text density check (digital vs scanned)

Strategy:
  PDF with extractable text  -> pdf_extractor
  PDF with no/little text    -> ocr_extractor (Tesseract -> Textract)
  DOCX                       -> docx_extractor
  RTF                        -> rtf_extractor
  Image (PNG/JPG/TIFF/WEBP)  -> ocr_extractor

`classify` blocks (it opens a PDF to measure its text layer): call it off the event loop.
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


def classify(filename: str, content: bytes) -> ExtractionStrategy:
    """
    Validate and classify the file.
    Raises UnsupportedFileTypeError for unknown/spoofed files.
    """
    file_type = validate_file(filename, content)   # raises on invalid

    if file_type == "docx":
        return ExtractionStrategy.DOCX

    if file_type == "rtf":
        return ExtractionStrategy.RTF

    if file_type == "pdf":
        return _classify_pdf(content)

    # png / jpeg / tiff / webp -> OCR
    return ExtractionStrategy.OCR


def _classify_pdf(content: bytes) -> ExtractionStrategy:
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            # Only whether the text layer clears the threshold matters, so stop at the
            # first page that gets it there - pdf_extractor reads every page next.
            text = ""
            for page in doc:
                text += page.get_text()
                if len(text.strip()) >= _PDF_TEXT_THRESHOLD:
                    return ExtractionStrategy.PDF
        return ExtractionStrategy.OCR
    except Exception:  # noqa: BLE001 - a PDF PyMuPDF cannot open or read may still be a readable scan; let OCR try
        return ExtractionStrategy.OCR
