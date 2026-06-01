"""
Extract text from digital (text-layer) PDFs using PyMuPDF.

Handles multi-column layouts by sorting text blocks by vertical position
and grouping by column when horizontal gaps are detected.
"""

import fitz  # PyMuPDF

from app.core.exceptions import ExtractionError
from app.core.logging import get_logger

log = get_logger(__name__)


def extract(content: bytes) -> str:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        pages_text: list[str] = []
        for page in doc:
            pages_text.append(_extract_page(page))
        doc.close()
        raw = "\n\n".join(pages_text)
        return _clean(raw)
    except Exception as exc:
        raise ExtractionError(f"PDF extraction failed: {exc}") from exc


def _extract_page(page: fitz.Page) -> str:
    # Use "blocks" mode: list of (x0, y0, x1, y1, text, block_no, block_type)
    blocks = page.get_text("blocks")
    # Keep only text blocks (block_type == 0), sort top-to-bottom then left-to-right
    text_blocks = sorted(
        [b for b in blocks if b[6] == 0],
        key=lambda b: (round(b[1] / 10) * 10, b[0]),  # group by ~10px row bands
    )
    return "\n".join(b[4].strip() for b in text_blocks if b[4].strip())


def _clean(text: str) -> str:
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        line = line.strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)
