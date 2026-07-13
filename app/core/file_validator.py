"""
Magic-bytes file validation.

Extension checks alone are trivially bypassed - an attacker can rename any file.
This validates the actual binary signature of the file content before processing.

Supported types and their signatures:
  PDF  : %PDF  (0x25 50 44 46)
  DOCX : PK\x03\x04  (ZIP archive - DOCX/XLSX/PPTX are ZIP containers)
  RTF  : {\rtf
  PNG  : \x89PNG\r\n\x1a\n
  JPEG : \xFF\xD8\xFF
  TIFF : II (little-endian) or MM (big-endian)
  WEBP : RIFF....WEBP
"""

import io
import zipfile

from app.core.exceptions import UnsupportedFileTypeError

_SIGNATURES: dict[str, list[bytes]] = {
    "pdf":  [b"%PDF"],
    "docx": [b"PK\x03\x04"],   # ZIP container
    "rtf":  [b"{\\rtf"],
    "png":  [b"\x89PNG\r\n\x1a\n"],
    "jpeg": [b"\xff\xd8\xff"],
    "tiff": [b"II*\x00", b"MM\x00*"],
    "webp": [b"RIFF"],          # Full check: bytes[8:12] == b"WEBP"
}

_EXT_TO_TYPE: dict[str, str] = {
    ".pdf":  "pdf",
    ".docx": "docx",
    ".rtf":  "rtf",
    ".png":  "png",
    ".jpg":  "jpeg",
    ".jpeg": "jpeg",
    ".tiff": "tiff",
    ".tif":  "tiff",
    ".webp": "webp",
}

_MAX_SNIFF = 12  # bytes needed to read any signature


def validate_file(filename: str, content: bytes) -> str:
    """
    Validate file by both extension and magic bytes.
    Returns the detected file type string.
    Raises UnsupportedFileTypeError if the file is invalid or type-spoofed.
    """
    ext = _get_ext(filename)
    if ext not in _EXT_TO_TYPE:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension '{ext}'. "
            "Accepted: .pdf, .docx, .rtf, .png, .jpg, .jpeg, .tiff, .webp"
        )

    declared_type = _EXT_TO_TYPE[ext]
    header = content[:_MAX_SNIFF]

    if not _matches_signature(header, declared_type):
        raise UnsupportedFileTypeError(
            f"File content does not match declared extension '{ext}'. "
            "Ensure the file is a valid, uncorrupted document."
        )

    # Extra WEBP check: bytes 8-12 must be "WEBP"
    if declared_type == "webp" and content[8:12] != b"WEBP":
        raise UnsupportedFileTypeError("File is not a valid WEBP image")

    # A .docx signature is just the generic ZIP header, so any ZIP (or a zip bomb)
    # renamed .docx passes the magic check. Confirm it's actually an OOXML package
    # by reading the central directory (no decompression) for the required members.
    if declared_type == "docx" and not _is_ooxml_docx(content):
        raise UnsupportedFileTypeError(
            "File is not a valid DOCX document (missing Word/OOXML structure)."
        )

    return declared_type


def _is_ooxml_docx(content: bytes) -> bool:
    """True when `content` is a ZIP holding the members a real .docx must have.

    Reads only the ZIP central directory (namelist does not decompress), so a
    malicious/huge archive can't be expanded here.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "[Content_Types].xml" in names and any(n.startswith("word/") for n in names)


def is_supported_extension(filename: str) -> bool:
    """True if the filename has a supported extension.

    Extension-only check (no content) - used by the presigned-upload endpoint to
    reject obviously-wrong files before issuing an upload URL. Magic-byte
    validation still runs on the downloaded content before parsing.
    """
    return _get_ext(filename) in _EXT_TO_TYPE


def _get_ext(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx != -1 else ""


def _matches_signature(header: bytes, file_type: str) -> bool:
    sigs = _SIGNATURES.get(file_type, [])
    return any(header.startswith(sig) for sig in sigs)
