"""
SSRF guard for user-supplied webhook URLs.

A webhook URL is attacker-controlled input that the server later makes outbound
requests to. Without validation a tenant could point a webhook at internal
infrastructure - the cloud metadata endpoint (169.254.169.254), private RFC1918
ranges, loopback, or shared CGNAT space - and use our egress to reach it (SSRF).

Checks:
  * scheme must be http/https (https only in production)
  * the host must resolve only to globally routable addresses

Registration validates the URL. Delivery resolves it again with
`resolve_webhook_url` and connects to exactly the address it validated (the Host
header and TLS SNI/certificate check still use the registered name), so a DNS
answer that changes between the check and the connection - rebinding - cannot
redirect the request to an internal address.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.config import get_settings


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook URL is malformed or resolves to a disallowed address."""


@dataclass(frozen=True)
class ResolvedWebhookURL:
    host: str  # the registered host name - for the Host header and TLS SNI
    ip: str    # the validated address to connect to


def _is_public_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback whatever the family
    # is_global excludes private, loopback, link-local (cloud metadata), reserved,
    # documentation and shared CGNAT (100.64.0.0/10) space. Multicast counts as
    # "global" but is never a webhook receiver.
    return ip.is_global and not ip.is_multicast


def resolve_webhook_url(url: str) -> ResolvedWebhookURL:
    """Validate `url` and return the address a delivery must connect to.

    Raises UnsafeWebhookURLError if the URL is unsafe to call from the server.
    Blocking (DNS) - call it off the event loop.
    """
    settings = get_settings()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURLError("Webhook URL must use http or https")
    if settings.is_production and parsed.scheme != "https":
        raise UnsafeWebhookURLError("Webhook URL must use HTTPS in production")
    if not parsed.hostname:
        raise UnsafeWebhookURLError("Webhook URL must include a valid host")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError) as exc:
        raise UnsafeWebhookURLError(
            f"Webhook host could not be resolved: {parsed.hostname}"
        ) from exc

    # Every resolved address must be public - reject if ANY is internal.
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or not all(_is_public_ip(ip) for ip in addresses):
        raise UnsafeWebhookURLError(
            "Webhook URL resolves to a private or internal address"
        )
    return ResolvedWebhookURL(host=parsed.hostname, ip=addresses[0])


def validate_webhook_url(url: str) -> None:
    """Raise UnsafeWebhookURLError if the URL is unsafe to call from the server."""
    resolve_webhook_url(url)
