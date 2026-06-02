"""
Digital PDF text extractor — PyMuPDF.

Column-aware reading order:
  1. Extract all text blocks with bounding boxes.
  2. Detect multi-column layout: if blocks cluster into ≥2 horizontal bands
     separated by a significant gap (>10% of page width), read column-by-column
     top-to-bottom rather than naively left-to-right.
  3. Concatenate all pages.

Handles: 1-column, 2-column (e.g. two-panel resumes), 3-column layouts.
"""

import fitz  # PyMuPDF

from app.core.exceptions import ExtractionError
from app.core.logging import get_logger

log = get_logger(__name__)

# Minimum horizontal gap (fraction of page width) to consider separate columns
_COLUMN_GAP_RATIO = 0.08


def extract(content: bytes) -> str:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        pages: list[str] = []
        for page in doc:
            pages.append(_extract_page(page))
        doc.close()
        return "\n\n".join(p for p in pages if p.strip())
    except Exception as exc:
        raise ExtractionError(f"PDF extraction failed: {exc}") from exc


def _extract_page(page: fitz.Page) -> str:
    raw_blocks = page.get_text("blocks")
    # Keep only text blocks (type 0) with non-empty content
    blocks = [
        (b[0], b[1], b[2], b[3], b[4].strip())
        for b in raw_blocks
        if b[6] == 0 and b[4].strip()
    ]
    if not blocks:
        return ""

    page_width = page.rect.width
    columns = _detect_columns(blocks, page_width)

    ordered: list[str] = []
    for col_blocks in columns:
        # Within each column: sort top-to-bottom
        col_sorted = sorted(col_blocks, key=lambda b: b[1])
        ordered.extend(b[4] for b in col_sorted)

    return "\n".join(ordered)


def _detect_columns(
    blocks: list[tuple],
    page_width: float,
) -> list[list[tuple]]:
    """
    Cluster blocks into reading columns.
    Returns a list of columns, each containing its blocks.
    Columns are ordered left-to-right.
    """
    if not blocks:
        return []

    # Collect all unique x-start positions (left edge of each block)
    xs = sorted(set(b[0] for b in blocks))

    # Find gaps: positions where the space between consecutive x-starts
    # exceeds the column-gap threshold
    gap_threshold = page_width * _COLUMN_GAP_RATIO
    column_starts = [xs[0]]
    for i in range(1, len(xs)):
        if xs[i] - xs[i - 1] > gap_threshold:
            column_starts.append(xs[i])

    if len(column_starts) < 2:
        # Single column — sort by y then x
        return [sorted(blocks, key=lambda b: (b[1], b[0]))]

    # Assign each block to the nearest column start
    columns: list[list[tuple]] = [[] for _ in column_starts]
    for block in blocks:
        bx = block[0]
        # Find the column whose start is closest to (and ≤) this block's x
        col_idx = 0
        for i, start in enumerate(column_starts):
            if bx >= start - 5:   # 5px tolerance for slight misalignment
                col_idx = i
        columns[col_idx].append(block)

    # Remove empty columns and return
    return [c for c in columns if c]
