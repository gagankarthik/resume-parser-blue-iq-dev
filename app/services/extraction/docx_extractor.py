"""
Extract text from DOCX files using python-docx.

Preserves paragraph structure; extracts table cell text inline.
"""

import io

from docx import Document
from docx.oxml.ns import qn

from app.core.exceptions import ExtractionError
from app.core.logging import get_logger

log = get_logger(__name__)


def extract(content: bytes) -> str:
    try:
        doc = Document(io.BytesIO(content))
        parts: list[str] = []

        for element in doc.element.body:
            tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
            if tag == "p":
                text = _para_text(element)
                if text.strip():
                    parts.append(text.strip())
            elif tag == "tbl":
                parts.append(_table_text(element))

        return "\n".join(parts)
    except Exception as exc:
        raise ExtractionError(f"DOCX extraction failed: {exc}") from exc


def _para_text(para_element) -> str:
    return "".join(r.text for r in para_element.iter(qn("w:t")))


def _table_text(tbl_element) -> str:
    rows: list[str] = []
    for row in tbl_element.iter(qn("w:tr")):
        cells = [
            "".join(r.text for r in cell.iter(qn("w:t")))
            for cell in row.iter(qn("w:tc"))
        ]
        rows.append(" | ".join(c.strip() for c in cells if c.strip()))
    return "\n".join(r for r in rows if r)
