"""Company identity and API-key issuance - shared by self-serve and admin routes."""

import re
import secrets
from typing import TypedDict

from app.core.security import generate_api_key, key_display_prefix
from app.db import dynamodb as db


class IssuedKey(TypedDict):
    api_key: str      # the raw key: returned to the caller once, never stored
    key_prefix: str
    created_at: str


def new_company_id(name: str) -> str:
    """A readable, collision-resistant company id: a slug of the name plus 6 hex chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{(slug or 'company')[:40]}-{secrets.token_hex(3)}"


def issue_api_key(company_id: str, name: str = "") -> IssuedKey:
    """Mint a key for `company_id` and store only its hash.

    The raw key exists only in the return value; callers show it once and drop it.
    """
    raw_key, key_hash = generate_api_key()
    prefix = key_display_prefix(raw_key)
    stored = db.create_api_key(key_hash=key_hash, key_prefix=prefix, company_id=company_id, name=name)
    return {"api_key": raw_key, "key_prefix": prefix, "created_at": stored["created_at"]}
