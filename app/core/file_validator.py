"""
Magic-bytes file validation.

Extension checks alone are trivially bypassed — an attacker can rename any file.
This validates the actual binary signature of the file content before processing.

Supported types and their signatures:
  PDF  : %PDF  (0x25 50 44 46)
  DOCX : PK\x03\x04  (ZIP archive — DOCX/XLSX/PPTX are ZIP containers)
  RTF  : {\rtf
  PNG  : \x89PNG\r\n\x1a\n
  JPEG : \xFF\xD8\xFF
  TIFF : II (little-endian) or MM (big-endian)
  WEBP : RIFF....WEBP
"""

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

    return declared_type


def is_supported_extension(filename: str) -> bool:
    """True if the filename has a supported extension.

    Extension-only check (no content) — used by the presigned-upload endpoint to
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
