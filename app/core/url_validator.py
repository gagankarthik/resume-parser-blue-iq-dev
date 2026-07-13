"""
SSRF guard for user-supplied webhook URLs.

A webhook URL is attacker-controlled input that the server later makes outbound
requests to. Without validation a tenant could point a webhook at internal
infrastructure - the cloud metadata endpoint (169.254.169.254), private RFC1918
ranges, or loopback - and use our egress to reach it (SSRF).

This validates at registration time:
  * scheme must be http/https (https only in production)
  * the host must resolve only to public, routable addresses

Residual risk: DNS rebinding after registration. Delivery additionally re-checks
the host, so a record that later resolves to a private address is skipped.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import get_settings


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook URL is malformed or resolves to a disallowed address."""


def _is_public_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # covers 169.254.0.0/16 (cloud metadata)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_webhook_url(url: str) -> None:
    """Raise UnsafeWebhookURLError if the URL is unsafe to call from the server."""
    settings = get_settings()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURLError("Webhook URL must use http or https")
    if settings.is_production and parsed.scheme != "https":
        raise UnsafeWebhookURLError("Webhook URL must use HTTPS in production")
    if not parsed.hostname:
        raise UnsafeWebhookURLError("Webhook URL must include a valid host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeWebhookURLError(
            f"Webhook host could not be resolved: {parsed.hostname}"
        ) from exc

    # Every resolved address must be public - reject if ANY is internal.
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise UnsafeWebhookURLError(
                "Webhook URL resolves to a private or internal address"
            )
