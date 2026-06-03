"""
Self-serve account auth (public).

  POST /api/v1/auth/signup   create an account + company, return a session token
  POST /api/v1/auth/login    verify credentials, return a session token
  GET  /api/v1/auth/me       current account (Bearer token)

Accounts are stored in the companies table (one account == one company).
The session token is a signed, stateless bearer token (see security.py).
"""

import re
import secrets

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app.api.dependencies import get_current_account
from app.core.config import get_settings
from app.core.errors import ErrorCode, api_error
from app.core.logging import get_logger
from app.core.security import create_account_token, hash_password, verify_password
from app.db import dynamodb as db

router = APIRouter(prefix="/auth", tags=["Account"])
log = get_logger(__name__)


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "company")[:40]


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
async def signup(payload: SignupRequest) -> dict:
    if db.get_company_by_email(str(payload.email)):
        raise api_error(409, ErrorCode.INVALID_REQUEST, "An account with this email already exists")

    company_id = f"{_slug(payload.name)}-{secrets.token_hex(3)}"
    db.create_company(
        company_id=company_id,
        name=payload.name,
        email=str(payload.email),
        plan="free",
        password_hash=hash_password(payload.password),
    )
    log.info("account_created", company_id=company_id)
    token = create_account_token(company_id, get_settings().auth_secret)
    company = db.get_company(company_id) or {}
    return {"token": token, "account": _public(company)}


@router.post("/login", summary="Sign in")
async def login(payload: LoginRequest) -> dict:
    company = db.get_company_by_email(str(payload.email))
    pw_hash = company.get("password_hash") if company else None
    # Always run a verify to reduce timing oracle on account existence.
    if not company or not pw_hash or not verify_password(payload.password, pw_hash):
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
