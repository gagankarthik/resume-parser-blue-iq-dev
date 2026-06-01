from fastapi import HTTPException, status


class ResumeParserError(Exception):
    pass


class FileValidationError(ResumeParserError):
    pass


class UnsupportedFileTypeError(FileValidationError):
    pass


class FileTooLargeError(FileValidationError):
    pass


class ExtractionError(ResumeParserError):
    pass


class OCRError(ExtractionError):
    pass


class ParsingError(ResumeParserError):
    pass


class AIParsingError(ParsingError):
    pass


class JobNotFoundError(ResumeParserError):
    pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_401(detail: str = "Invalid or missing API key") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def http_403(detail: str = "API key revoked") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def http_404(detail: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def http_413(detail: str = "File too large") -> HTTPException:
    return HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=detail)


def http_415(detail: str = "Unsupported file type") -> HTTPException:
    return HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=detail)


def http_429(detail: str = "Rate limit exceeded") -> HTTPException:
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def http_422(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
