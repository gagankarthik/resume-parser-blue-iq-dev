import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import router
from app.core.config import get_settings
from app.core.exceptions import ResumeParserError
from app.core.logging import configure_logging, get_logger
import structlog

log = get_logger(__name__)


# ── Security + tracing middleware ─────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers and X-Request-ID to every response."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        # Bind request_id to structlog context for this request
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        settings = get_settings()
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        structlog.contextvars.unbind_contextvars("request_id")
        return response


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


def _error_body(status_code: int, error_code: str, detail: str) -> dict:
    return {
        "error": {
            "status_code": status_code,
            "error_code": error_code,
            "detail": detail,
        }
    }


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Enterprise Resume Parsing API — "
            "converts PDF/DOCX/image resumes into structured JSON. "
            "Built for healthcare staffing: nursing and allied health professions."
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(router)

    # ── Unified error handlers ────────────────────────────────────────────────

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body(
                422,
                "VALIDATION_ERROR",
                str(exc.errors()),
            ),
        )

    @app.exception_handler(ResumeParserError)
    async def resume_parser_error_handler(
        request: Request, exc: ResumeParserError
    ) -> JSONResponse:
        log.warning(
            "domain_error",
            error_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=_error_body(500, type(exc).__name__, "Resume processing failed"),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        log.error(
            "unhandled_error",
            error_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=_error_body(500, "INTERNAL_ERROR", "An unexpected error occurred"),
        )

    return app


app = create_app()
