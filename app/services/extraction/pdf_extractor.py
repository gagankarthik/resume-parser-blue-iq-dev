"""
Digital PDF text extractor - PyMuPDF.

Column-aware reading order:
  1. Extract all text blocks with bounding boxes.
  2. Detect multi-column layout: if blocks cluster into >=2 horizontal bands
     separated by a significant gap (>8% of page width), read column-by-column
     top-to-bottom rather than naively left-to-right.
  3. But guard against the common resume case where a "column" is really a strip
     of right-aligned annotations (e.g. employment / graduation dates sitting on
     the same line as each entry). Those must be read row-by-row so each date
     stays attached to its entry instead of being detached into a block at the
     end. See `_is_row_annotated`.
  4. Concatenate all pages.

Handles: 1-column, 2-column (e.g. two-panel resumes), 3-column layouts, and
single-column flows with a right-aligned date column.
"""

import re

import fitz  # PyMuPDF

from app.core.exceptions import ExtractionError
from app.core.logging import get_logger

log = get_logger(__name__)

# Minimum horizontal gap (fraction of page width) to consider separate columns
_COLUMN_GAP_RATIO = 0.08

# Two blocks belong to the same row when their vertical extents overlap by at
# least this fraction of the shorter block's height.
_ROW_OVERLAP_RATIO = 0.4
# Fraction of non-leftmost-column blocks that must sit on a leftmost-column row
# for the layout to be treated as row-annotated (dates beside each entry) rather
# than as independent newspaper-style columns.
_ROW_PAIR_RATIO = 0.6
# A row-annotation strip carries far less text than the main column; a genuine
# second column carries comparable volume. Right blocks must number below this
# fraction of the left column to qualify as annotations.
_ANNOTATION_SPARSITY = 0.6
# Fraction of right blocks that must read as annotations (short tokens or dates)
# rather than prose for the strip to qualify. This is what stops a genuine - but
# sparse and coincidentally row-aligned - text column from being misread.
_ANNOTATION_CONTENT_RATIO = 0.8
# A block counts as a short token (not prose) below this many characters/words.
_ANNOTATION_MAX_CHARS = 35
_ANNOTATION_MAX_WORDS = 5
# A 4-digit year (19xx/20xx) - the hallmark of a date strip; dates beside entries
# are annotations regardless of length ("January, 2020 - December, 2022").
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


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

    # A right-aligned date strip masquerades as a column. If every "right column"
    # block actually sits on a left-column row, read row-by-row so each date
    # stays next to its entry instead of being collected at the page bottom.
    if len(columns) >= 2 and _is_row_annotated(columns):
        return _read_by_rows(blocks)

    ordered: list[str] = []
    for col_blocks in columns:
        # Within each column: sort top-to-bottom
        col_sorted = sorted(col_blocks, key=lambda b: b[1])
        ordered.extend(b[4] for b in col_sorted)

    return "\n".join(ordered)


def _cluster_rows(items: list[tuple]) -> list[dict]:
    """Group blocks into horizontal rows by vertical overlap.

    `items` is a list of (col_idx, block) tuples; the col_idx tags which detected
    column a block came from and is carried through so callers can tell whether a
    row mixes columns. Returns rows (each a dict with its y-band and items),
    ordered top-to-bottom.
    """
    rows: list[dict] = []
    for col_idx, b in sorted(items, key=lambda t: t[1][1]):  # by y0
        height = b[3] - b[1]
        for row in rows:
            overlap = min(b[3], row["y1"]) - max(b[1], row["y0"])
            if overlap > _ROW_OVERLAP_RATIO * min(height, row["y1"] - row["y0"]):
                row["items"].append((col_idx, b))
                row["y0"] = min(row["y0"], b[1])
                row["y1"] = max(row["y1"], b[3])
                break
        else:
            rows.append({"y0": b[1], "y1": b[3], "items": [(col_idx, b)]})
    rows.sort(key=lambda r: r["y0"])
    return rows


def _is_annotation_text(text: str) -> bool:
    """A date or short label (annotation), as opposed to a line of prose.

    Date-like text qualifies regardless of length; otherwise the block must be a
    short, few-word token. A long multi-word block with no year is prose and
    disqualifies the strip - that is what protects a genuine sparse text column.
    """
    if _YEAR_RE.search(text):
        return True
    collapsed = " ".join(text.split())
    return len(collapsed) <= _ANNOTATION_MAX_CHARS and len(collapsed.split()) <= _ANNOTATION_MAX_WORDS


def _is_row_annotated(columns: list[list[tuple]]) -> bool:
    """True when the non-leftmost columns are a sparse strip of annotations that
    each line up with a leftmost-column row (e.g. dates beside resume entries),
    rather than an independent text column.

    Three conditions must all hold:
      * pairing  - most right blocks share a row with a left-column block;
      * sparsity - the right strip carries far fewer blocks than the left column;
      * content  - the right blocks read as annotations (dates / short tokens),
                   not prose, so a genuine but sparse column is not misread.
    """
    items = [(ci, b) for ci, col in enumerate(columns) for b in col]
    right = [b for ci, b in items if ci > 0]
    left_n = sum(1 for ci, _ in items if ci == 0)
    if not right or not left_n:
        return False

    rows = _cluster_rows(items)
    paired = sum(
        sum(1 for ci, _ in row["items"] if ci > 0)
        for row in rows
        if any(ci == 0 for ci, _ in row["items"])
    )
    pairing_ratio = paired / len(right)
    sparse = len(right) < _ANNOTATION_SPARSITY * left_n
    annotation_ratio = sum(_is_annotation_text(b[4]) for b in right) / len(right)
    return (
        pairing_ratio >= _ROW_PAIR_RATIO
        and sparse
        and annotation_ratio >= _ANNOTATION_CONTENT_RATIO
    )


def _read_by_rows(blocks: list[tuple]) -> str:
    """Read blocks row-by-row, left-to-right within each row, top-to-bottom."""
    rows = _cluster_rows([(0, b) for b in blocks])
    lines: list[str] = []
    for row in rows:
        row_blocks = sorted((b for _, b in row["items"]), key=lambda b: b[0])
        lines.extend(b[4] for b in row_blocks)
    return "\n".join(lines)


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
        # Single column - sort by y then x
        return [sorted(blocks, key=lambda b: (b[1], b[0]))]

    # Assign each block to the nearest column start
    columns: list[list[tuple]] = [[] for _ in column_starts]
    for block in blocks:
        bx = block[0]
        # Find the column whose start is closest to (and <=) this block's x
        col_idx = 0
        for i, start in enumerate(column_starts):
            if bx >= start - 5:   # 5px tolerance for slight misalignment
                col_idx = i
        columns[col_idx].append(block)

    # Remove empty columns and return
    return [c for c in columns if c]
