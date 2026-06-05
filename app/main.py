"""
FastAPI application factory.

Middleware stack (outermost → innermost):
  1. RequestLoggingMiddleware  — logs every request with duration + request_id
  2. SecurityHeadersMiddleware — X-Request-ID, security headers, HSTS
  3. CORSMiddleware

Error handlers:
  All exceptions → consistent {"error": {"status_code", "error_code", "detail"}} envelope.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import router
from app.core.config import get_settings
from app.core.errors import ErrorCode, get_hint
from app.core.exceptions import ResumeParserError
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


# ── Middleware ────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id   # accessible to error handlers
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)

        response.headers["X-Request-ID"]           = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"

        if get_settings().is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        structlog.contextvars.unbind_contextvars("request_id")
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request: method, path, status, duration — no body content."""

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            request_id=response.headers.get("X-Request-ID", ""),
            # Redact query params that might carry sensitive data
            has_query=bool(request.url.query),
        )
        return response


# ── Error envelope ────────────────────────────────────────────────────────────

def _error_body(
    status_code: int,
    error_code: str,
    detail: str,
    request: Request,
    hint: str | None = None,
) -> dict:
    """
    Standard error envelope sent to clients.
    Always includes a user-facing hint and the request_id for support tickets.
    """
    return {
        "error": {
            "status_code": status_code,
            "error_code":  error_code,
            "detail":      detail,
            "hint":        hint or get_hint(error_code),
            "request_id":  getattr(request.state, "request_id", None)
                           or request.headers.get("X-Request-ID", ""),
        }
    }


# ── App factory ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    settings = get_settings()
    log.info(
        "startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "**Resume Parser API** — converts PDF, DOCX, and image resumes into structured JSON "
            "ready for healthcare staffing profile fields.\n\n"
            "Specialised for nursing and allied health: normalises 350+ clinical specialties, "
            "credentials (RN, LPN, CRT, RRT, OT, PT, SLP…), and certifications (BLS, ACLS, CCRN…).\n\n"
            "All responses include `X-Request-ID` for tracing."
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # Middleware — outermost first.
    # CORS is handled here (single source of truth); the Lambda Function URL does
    # NOT also set CORS, which would double the Access-Control-* headers.
    #
    # Default posture: deny cross-origin in production unless explicitly allow-listed
    # via CORS_ALLOWED_ORIGINS; development falls back to "*" for convenience.
    if settings.cors_origins_list:
        cors_origins = settings.cors_origins_list
    elif not settings.is_production:
        cors_origins = ["*"]
    else:
        cors_origins = []

    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(router)

    # ── Unified error handlers ────────────────────────────────────────────────

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            # Structured error from api_error() factory — preserves the hint
            body = _error_body(
                exc.status_code,
                exc.detail["error_code"],
                exc.detail["detail"],
                request,
                hint=exc.detail.get("hint"),
            )
        else:
            body = _error_body(exc.status_code, "HTTP_ERROR", str(exc.detail), request)
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Build a clear validation message: "field 'X' — message"
        errors = exc.errors()
        if errors:
            err  = errors[0]
            loc  = " → ".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
            detail = f"{loc}: {err.get('msg', 'invalid value')}" if loc else err.get("msg", "Validation failed")
        else:
            detail = "Validation failed"
        return JSONResponse(
            status_code=422,
            content=_error_body(422, str(ErrorCode.VALIDATION_ERROR), detail, request),
        )

    @app.exception_handler(ResumeParserError)
    async def domain_error_handler(request: Request, exc: ResumeParserError) -> JSONResponse:
        log.warning("domain_error", error_type=type(exc).__name__, error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_error_body(
                500, type(exc).__name__,
                "Resume processing failed. See the X-Request-ID header for support tickets.",
                request,
                hint=get_hint(str(ErrorCode.PARSE_FAILED)),
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", error_type=type(exc).__name__, error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_error_body(
                500, str(ErrorCode.INTERNAL_ERROR),
                "An unexpected error occurred",
                request,
            ),
        )

    return app


app = create_app()
