import base64
import hashlib
import hmac
import json
import re
import secrets
import time

# API key must match this pattern - validated before any DynamoDB call
_API_KEY_PATTERN = re.compile(r"^rp_live_[A-Za-z0-9_\-]{32,}$")


def validate_key_format(raw_key: str) -> bool:
    """Return True if the key has the correct rp_live_... format."""
    return bool(_API_KEY_PATTERN.match(raw_key))


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, key_hash). Store only the hash - never the raw key."""
    raw_key = f"rp_live_{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_display_prefix(raw_key: str) -> str:
    """Return the first 12 chars + ellipsis for safe logging/display."""
    return raw_key[:12] + "..."


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


def sign_webhook_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 signature: sha256=<hex>  over '<timestamp>.<body>'."""
    message = f"{timestamp}.".encode() + body
    sig = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def verify_webhook_signature(
    secret: str, timestamp: str, body: bytes, signature: str
) -> bool:
    expected = sign_webhook_payload(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)


# -- Account passwords (self-serve signup) -------------------------------------

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 (stdlib). Format: pbkdf2_sha256$rounds$salt_b64$hash_b64."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt_b64), int(rounds_s))
        return hmac.compare_digest(dk, _unb64(hash_b64))
    except (ValueError, TypeError):
        return False


# -- Account session tokens (signed, stateless) --------------------------------

def create_account_token(company_id: str, secret: str, ttl_seconds: int = 60 * 60 * 24 * 7) -> str:
    """Signed token: base64url(payload).hmac. Payload carries company_id + exp."""
    payload = {"sub": company_id, "exp": int(time.time()) + ttl_seconds}
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _sign(raw, secret)
    return f"{raw}.{sig}"


def verify_account_token(token: str, secret: str) -> str | None:
    """Return the company_id if the token is valid and unexpired, else None."""
    try:
        raw, sig = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(raw, secret), sig):
        return None
    try:
        payload = json.loads(_unb64(raw))
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


def _sign(data: str, secret: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
