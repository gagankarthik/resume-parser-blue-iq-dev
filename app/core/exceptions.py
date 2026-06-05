"""
Domain exception hierarchy.

All application errors derive from ``ResumeParserError`` so the global handler in
``app/main.py`` can map them to the standard error envelope. HTTP-status responses
are built via ``app.core.errors.api_error`` (which attaches the user-facing hint),
not raised directly here.
"""


class ResumeParserError(Exception):
    pass


class FileValidationError(ResumeParserError):
    pass


class UnsupportedFileTypeError(FileValidationError):
    pass


class ExtractionError(ResumeParserError):
    pass


class OCRError(ExtractionError):
    pass


class ParsingError(ResumeParserError):
    pass


class AIParsingError(ParsingError):
    pass
