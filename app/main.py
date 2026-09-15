"""
FastAPI application factory.

Middleware stack (outermost -> innermost):
  1. ServerErrorMiddleware    - Starlette's own last resort
  2. CORSMiddleware
  3. RequestContextMiddleware - request id, 413 guard, security headers,
                                request log, 500 for unhandled exceptions
  4. ExceptionMiddleware      - Starlette's; runs the handlers registered below

RequestContextMiddleware sits inside CORS on purpose: everything it sends - a 413,
the 500 for an unhandled exception - passes back out through CORS and carries the
Access-Control-* headers, as it did before. CORS answers preflight requests itself,
so those are neither logged nor given security headers (also as before).

`add_middleware` inserts at the front of the stack, so the LAST one added is the
outermost; the order of the calls in create_app() is the reverse of the list above.

Error handlers:
  All exceptions -> consistent {"error": {"status_code", "error_code", "detail"}} envelope.
"""

import re
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.router import router
from app.core.config import get_settings
from app.core.errors import ErrorCode, get_hint, public_error
from app.core.exceptions import ResumeParserError
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


# -- Middleware ----------------------------------------------------------------

# A client-supplied X-Request-ID is echoed into response headers and every log line
# of the request, so only a short, plain value is trusted; anything else is replaced.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _request_id(supplied: str | None) -> str:
    if supplied and _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """Request id, body-size guard, security headers, request log, last-resort 500.

    A plain ASGI middleware rather than BaseHTTPMiddleware: it wraps `send`, so the
    headers land on every response it passes through - including the 500 it builds
    itself - and its `finally` logs every request, whatever happened inside.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = _request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id   # read by the error envelope
        structlog.contextvars.bind_contextvars(request_id=request_id)
        settings = get_settings()
        started = time.monotonic()
        status = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"]           = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"]        = "DENY"
                headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
                if settings.is_production:
                    # Docs (Swagger UI, which needs a CDN) are disabled in production, so a
                    # lock-down CSP is safe here and would only break the dev docs page.
                    headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        try:
            oversized = _content_length_exceeds(request, _max_body_bytes(request, settings))
            if oversized is not None:
                await oversized(scope, receive, send_with_headers)
            else:
                await self.app(scope, receive, send_with_headers)
        except Exception:
            log.exception("unhandled_error")
            if response_started:
                raise   # too late to send an error response; the server closes the connection
            response = JSONResponse(
                status_code=500,
                content=_error_body(
                    500, str(ErrorCode.INTERNAL_ERROR), "An unexpected error occurred", request,
                ),
            )
            await response(scope, receive, send_with_headers)
        finally:
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                # Redact query params that might carry sensitive data
                has_query=bool(scope.get("query_string")),
            )
            structlog.contextvars.unbind_contextvars("request_id")


def _max_body_bytes(request: Request, settings) -> int:
    # The batch route carries many files, so it gets a larger ceiling than the
    # single-file cap (which would otherwise 413 a valid multi-file batch).
    if request.method == "POST" and request.url.path.rstrip("/").endswith("/resume/batch"):
        return settings.max_batch_request_bytes
    return settings.max_request_bytes


def _content_length_exceeds(request: Request, max_bytes: int) -> JSONResponse | None:
    """Return a 413 response when the request's Content-Length exceeds `max_bytes`.

    Rejects an over-sized body from its declared length BEFORE it is read into
    memory - defense-in-depth above each endpoint's file-size check. Returns None
    when there is no Content-Length (e.g. chunked) or it is within the limit -
    those bodies are still bounded by each endpoint's own size check.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        declared = int(raw)
    except ValueError:
        return None
    if declared <= max_bytes:
        return None
    return JSONResponse(
        status_code=413,
        content=_error_body(
            413, str(ErrorCode.REQUEST_TOO_LARGE),
            f"Request body of {declared // 1024} KB exceeds the "
            f"{max_bytes // (1024 * 1024)} MB limit",
            request,
        ),
    )


# -- Error envelope ------------------------------------------------------------

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
            "request_id":  getattr(request.state, "request_id", ""),
        }
    }


# -- App factory ---------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    settings = get_settings()
    # Fail closed before serving a single request if a prod deploy is missing
    # critical secrets (e.g. the account-token signing key).
    settings.assert_production_ready()
    # Degraded-but-serving config is logged, never raised - see
    # Settings.production_config_warnings().
    for warning in settings.production_config_warnings():
        log.error("production_config_degraded", detail=warning)
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
            "**Resume Parser API** - converts PDF, DOCX, and image resumes into structured JSON "
            "ready for healthcare staffing profile fields.\n\n"
            "Specialised for nursing and allied health: normalises 350+ clinical specialties, "
            "credentials (RN, LPN, CRT, RRT, OT, PT, SLP...), and certifications (BLS, ACLS, CCRN...).\n\n"
            "All responses include `X-Request-ID` for tracing."
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

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

    # Added innermost-first (see the module docstring): CORS ends up outside the
    # request-context middleware.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "Authorization", "X-Request-ID", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(router)

    # -- Unified error handlers ------------------------------------------------
    # Unhandled exceptions are turned into the 500 envelope by
    # RequestContextMiddleware, so the response still carries X-Request-ID and CORS.

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            # Structured error from api_error() factory - preserves the hint
            body = _error_body(
                exc.status_code,
                exc.detail["error_code"],
                exc.detail["detail"],
                request,
                hint=exc.detail.get("hint"),
            )
        else:
            body = _error_body(exc.status_code, "HTTP_ERROR", str(exc.detail), request)
        # Propagate any headers set on the exception (e.g. Retry-After on a 429).
        return JSONResponse(
            status_code=exc.status_code, content=body,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Build a clear validation message: "field 'X' - message"
        errors = exc.errors()
        if errors:
            err  = errors[0]
            loc  = " -> ".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
            detail = f"{loc}: {err.get('msg', 'invalid value')}" if loc else err.get("msg", "Validation failed")
        else:
            detail = "Validation failed"
        return JSONResponse(
            status_code=422,
            content=_error_body(422, str(ErrorCode.VALIDATION_ERROR), detail, request),
        )

    @app.exception_handler(ResumeParserError)
    async def domain_error_handler(request: Request, exc: ResumeParserError) -> JSONResponse:
        # Client-input problems (an unreadable/encrypted PDF, a scan OCR can't
        # decode, a spoofed type) surface as 4xx so callers know retrying the same
        # file won't help. The mapping - and what text is safe to return - lives in
        # public_error(), shared with the worker's failed jobs and parse.failed.
        failure = public_error(exc)
        if failure.status_code >= 500:
            log.error("domain_error", error_type=type(exc).__name__, exc_info=exc)
        else:
            log.warning("domain_error", error_type=type(exc).__name__,
                        error_code=failure.error_code.value)
        return JSONResponse(
            status_code=failure.status_code,
            content=_error_body(failure.status_code, failure.error_code.value, failure.detail, request),
        )

    return app


app = create_app()
