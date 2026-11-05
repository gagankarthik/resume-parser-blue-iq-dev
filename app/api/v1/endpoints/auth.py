"""
Self-serve account auth (public).

  POST /api/v1/auth/signup   create an account + company, return a session token
  POST /api/v1/auth/login    verify credentials, return a session token
  GET  /api/v1/auth/me       current account (Bearer token)

Accounts are stored in the companies table (one account == one company).
The session token is a signed, stateless bearer token (see security.py).
"""

import asyncio
import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import get_current_account
from app.core import rate_limit
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.security import create_account_token, hash_password, verify_password
from app.db import dynamodb as db
from app.services.application.companies import new_company_id

router = APIRouter(prefix="/auth", tags=["Account"])
log = get_logger(__name__)

# Precomputed once at import so a login for a non-existent email still runs a full
# PBKDF2 verify (against this unknowable hash) instead of short-circuiting - which
# would leak account existence via response timing.
_DECOY_PW_HASH = hash_password(secrets.token_hex(16))


def _client_ip(request: Request) -> str:
    """The address that opened the connection, for per-IP auth throttling.

    Deliberately NOT X-Forwarded-For. Each proxy appends to that header, so every
    hop left of the proxies we trust is whatever the client sent - and the API's
    Lambda Function URL is public (authorization NONE), so a request can arrive
    with no proxy in front at all. Keying on the first hop let a client rotate it
    and never be throttled.

    Topology assumption: client -> CloudFront -> Lambda Function URL -> Mangum.
    Mangum sets request.client from requestContext.http.sourceIp, the immediate
    TCP peer, which the client cannot forge. Behind CloudFront that peer is the
    edge node, so the limit is per edge address: it can over-throttle clients that
    share an edge, never under-throttle one that lies. A per-viewer limit belongs
    in front of the origin (a CloudFront / WAF rate-based rule).
    """
    return request.client.host if request.client else "unknown"


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _public(company: dict) -> dict:
    return {
        "company_id": company.get("company_id"),
        "name": company.get("name"),
        "email": company.get("email"),
        "plan": company.get("plan", "free"),
        "status": company.get("status", "active"),
        "created_at": company.get("created_at"),
    }


@router.post("/signup", status_code=201, summary="Create an account")
async def signup(payload: SignupRequest, request: Request) -> dict:
    rate_limit.check_auth(_client_ip(request))
    if db.get_company_by_email(str(payload.email)):
        raise api_error(409, ErrorCode.INVALID_REQUEST, "An account with this email already exists")

    company_id = new_company_id(payload.name)
    # ~200k PBKDF2 rounds: CPU-bound, so off the event loop.
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    db.create_company(
        company_id=company_id,
        name=payload.name,
        email=str(payload.email),
        plan="free",
        password_hash=password_hash,
    )
    log.info("account_created", company_id=company_id)
    token = create_account_token(company_id, get_settings().auth_secret)
    company = db.get_company(company_id) or {}
    return {"token": token, "account": _public(company)}


@router.post("/login", summary="Sign in")
async def login(payload: LoginRequest, request: Request) -> dict:
    rate_limit.check_auth(_client_ip(request))
    company = db.get_company_by_email(str(payload.email))
    pw_hash = company.get("password_hash") if company else None
    # Always run a full verify (against a decoy hash when the account/hash is
    # missing) so an unknown email takes the same ~200k-round PBKDF2 time as a
    # known one - closes the timing oracle on account existence. CPU-bound, so
    # off the event loop.
    ok = await asyncio.to_thread(verify_password, payload.password, pw_hash or _DECOY_PW_HASH)
    if not company or not pw_hash or not ok:
        raise api_error(401, ErrorCode.INVALID_API_KEY, "Invalid email or password")
    if company.get("status") != "active":
        raise api_error(403, ErrorCode.REVOKED_API_KEY, "This account is disabled")

    token = create_account_token(company["company_id"], get_settings().auth_secret)
    return {"token": token, "account": _public(company)}


@router.get("/me", summary="Current account")
async def me(company_id: str = Depends(get_current_account)) -> dict:
    company = db.get_company(company_id)
    if not company:
        raise api_error(404, ErrorCode.INVALID_REQUEST, "Account not found")
    return _public(company)
