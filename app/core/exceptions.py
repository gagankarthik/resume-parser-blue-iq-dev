"""
Domain exception hierarchy.

All application errors derive from ``ResumeParserError``. What a client is told
about any exception - status, error code, message - is decided in one place,
``app.core.errors.public_error``; HTTP-status responses are built via
``app.core.errors.api_error`` (which attaches the user-facing hint), not raised
directly here.
"""

from app.core.errors import ErrorCode


class ResumeParserError(Exception):
    pass


class FileValidationError(ResumeParserError):
    pass


class UnsupportedFileTypeError(FileValidationError):
    pass


class NotAResumeError(ResumeParserError):
    """The document is empty or is not a resume.

    Surfaces as ``NOT_A_RESUME`` (HTTP 422) on the request path, and as the
    ``error_code`` of a failed async job and its ``parse.failed`` webhook. Kept out
    of the FileValidationError branch on purpose: that one maps to 415.

    The resume gate may set a ``tokens`` attribute (the classifier's spend) so the
    audit record still bills it when the document is rejected.
    """

    error_code = ErrorCode.NOT_A_RESUME
    tokens: int = 0


class ExtractionError(ResumeParserError):
    pass


class OCRError(ExtractionError):
    pass


class ParsingError(ResumeParserError):
    pass


class AIParsingError(ParsingError):
    pass
