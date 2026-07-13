"""
Extract text from RTF files using striprtf.

RTF is markup-over-plain-text; striprtf strips control words and groups while
preserving paragraph breaks (\\par -> newline) and decoding escaped characters.
"""

from striprtf.striprtf import rtf_to_text

from app.core.exceptions import ExtractionError
from app.core.logging import get_logger

log = get_logger(__name__)


def extract(content: bytes) -> str:
    try:
        # RTF is 7-bit ASCII by spec - non-ASCII characters appear as escapes
        # (\'xx, \uNNNN) that striprtf decodes itself. latin-1 maps every byte
        # 1:1 and never raises, so those escapes reach the decoder intact even
        # from sloppy writers that emit raw 8-bit bytes.
        text = rtf_to_text(content.decode("latin-1"), errors="ignore")
        return text.strip()
    except Exception as exc:
        raise ExtractionError(f"RTF extraction failed: {exc}") from exc
