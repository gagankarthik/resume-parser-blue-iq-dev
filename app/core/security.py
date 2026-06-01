import hashlib
import hmac
import re
import secrets

# API key must match this pattern — validated before any DynamoDB call
_API_KEY_PATTERN = re.compile(r"^rp_live_[A-Za-z0-9_\-]{32,}$")


def validate_key_format(raw_key: str) -> bool:
    """Return True if the key has the correct rp_live_... format."""
    return bool(_API_KEY_PATTERN.match(raw_key))


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, key_hash). Store only the hash — never the raw key."""
    raw_key = f"rp_live_{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_display_prefix(raw_key: str) -> str:
    """Return the first 12 chars + ellipsis for safe logging/display."""
    return raw_key[:12] + "…"


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
