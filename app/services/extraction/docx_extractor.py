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


def _runs_text(element) -> str:
    """Text of a paragraph, preserving intra-paragraph structure: soft line breaks
    (w:br) become newlines and tabs (w:tab) become spaces. Without this a wrapped
    line ("Degree: July 21, 2018" then "Next Degree: …") collapses into one run-on
    string and the AI loses the dates / entry boundaries."""
    out: list[str] = []
    for node in element.iter():
        tag = node.tag.split("}")[-1]
        if tag == "t":
            out.append(node.text or "")
        elif tag == "tab":
            out.append(" ")
        elif tag == "br":
            out.append("\n")
    return "".join(out)


def _para_text(para_element) -> str:
    return _runs_text(para_element)


def _table_text(tbl_element) -> str:
    rows: list[str] = []
    for row in tbl_element.iter(qn("w:tr")):
        cells = []
        for cell in row.iter(qn("w:tc")):
            # Join each paragraph in the cell on its OWN line so a multi-line cell
            # (e.g. a school header + several degree/date lines) keeps its structure
            # instead of collapsing into one run-on string.
            cell_text = "\n".join(
                line for line in (
                    _runs_text(p).strip() for p in cell.iter(qn("w:p"))
                ) if line
            )
            if cell_text.strip():
                cells.append(cell_text)
        rows.append(" | ".join(cells))
    return "\n".join(r for r in rows if r)
