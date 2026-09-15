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
from dataclasses import dataclass

from app.core.errors import ErrorCode
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

# Client-supplied filenames are stored on the job row and shown back to the caller.
MAX_FILENAME_LENGTH = 255

# A DOCX is a ZIP that python-docx inflates part by part into memory, so a 10 MB
# upload can declare gigabytes. Real resumes unpack to a few MB; the ratio check
# only applies above a floor so a small, highly compressible XML-only document
# is never rejected.
_MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_DOCX_RATIO = 100
_MAX_DOCX_RATIO_FLOOR_BYTES = 10 * 1024 * 1024


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
    # of plausible size by reading the central directory (no decompression).
    if declared_type == "docx":
        problem = _docx_problem(content)
        if problem:
            raise UnsupportedFileTypeError(problem)

    return declared_type


def _docx_problem(content: bytes) -> str | None:
    """Why `content` is not an acceptable .docx, or None when it is.

    Reads only the ZIP central directory (infolist does not decompress). The sizes
    there are declared by the file, but zipfile stops inflating a member at its
    declared size, so the declared totals bound what python-docx can expand.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = zf.infolist()
    except (zipfile.BadZipFile, OSError):
        members = []
    names = {m.filename for m in members}
    if "[Content_Types].xml" not in names or not any(n.startswith("word/") for n in names):
        return "File is not a valid DOCX document (missing Word/OOXML structure)."

    unpacked = sum(m.file_size for m in members)
    packed = max(1, sum(m.compress_size for m in members))
    if unpacked > _MAX_DOCX_UNCOMPRESSED_BYTES or (
        unpacked > _MAX_DOCX_RATIO_FLOOR_BYTES and unpacked / packed > _MAX_DOCX_RATIO
    ):
        return (
            f"File is not a valid DOCX document (it expands to {unpacked // (1024 * 1024)} MB "
            "when unpacked, far beyond any resume)."
        )
    return None


@dataclass(frozen=True)
class Rejection:
    """Why an upload was refused, in the shape every endpoint reports it."""

    status_code: int
    error_code: ErrorCode
    detail: str


def check_upload(filename: str, content: bytes, max_bytes: int) -> Rejection | None:
    """Empty, size and type checks shared by every upload path. None means accepted.

    An empty (or whitespace-only) file is NOT_A_RESUME - the code the worker uses
    for an empty or non-resume document - rather than falling through to the
    magic-byte check and reading as a wrong file type.
    """
    # isspace() scans in place; strip() would copy up to max_bytes just to test emptiness.
    if not content or content.isspace():
        return Rejection(422, ErrorCode.NOT_A_RESUME, "The uploaded file is empty.")
    if len(content) > max_bytes:
        return Rejection(
            413, ErrorCode.FILE_TOO_LARGE,
            f"File size {len(content) // 1024} KB exceeds the {max_bytes // (1024 * 1024)} MB limit",
        )
    try:
        validate_file(filename, content)
    except UnsupportedFileTypeError as exc:
        return Rejection(415, ErrorCode.UNSUPPORTED_FILE_TYPE, str(exc))
    return None


def clamp_filename(filename: str) -> str:
    """Bound a client-supplied filename, keeping its extension so type checks still see it."""
    if len(filename) <= MAX_FILENAME_LENGTH:
        return filename
    ext = _get_ext(filename)
    if len(ext) > 16:
        ext = ""
    return filename[: MAX_FILENAME_LENGTH - len(ext)] + ext


def storage_extension(filename: str) -> str:
    """The filename's extension when it is a supported type, else "" - safe for a storage key."""
    ext = _get_ext(filename)
    return ext if ext in _EXT_TO_TYPE else ""


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
