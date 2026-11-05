"""Webhook delivery SSRF and log hygiene (M2, A5).

  * the URL was validated, then httpx resolved the host AGAIN to connect - a DNS
    answer that changed in between reached whatever it pointed at
  * a slow receiver could hold one attempt open indefinitely (the timeout was per
    read, not per attempt)
  * the full webhook URL, which can carry a token, was logged
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core import url_validator
from app.workers import webhook_sender


class _Server:
    def __init__(self, handler_cls):
        self.requests: list[dict] = []
        handler_cls.captured = self.requests
        self._server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _Ok(BaseHTTPRequestHandler):
    captured: list

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.captured.append({"headers": dict(self.headers), "body": self.rfile.read(length)})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_a):
        pass


class _Drip(_Ok):
    """Answers, but trickles its headers out so no single read ever times out."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.captured.append({"headers": dict(self.headers), "body": self.rfile.read(length)})
        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
        self.wfile.flush()
        for i in range(6):
            time.sleep(0.25)
            self.wfile.write(f"X-Drip-{i}: 1\r\n".encode())
            self.wfile.flush()
        self.wfile.write(b"Content-Length: 0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    webhook_sender._circuit.clear()
    yield
    webhook_sender._circuit.clear()


def _register(monkeypatch, *hooks):
    monkeypatch.setattr(webhook_sender.db, "get_active_webhooks_for_event", lambda c, e: list(hooks))


async def test_delivery_connects_to_the_address_that_was_validated(monkeypatch):
    """Resolve once: the host name is looked up by the validator only, and the
    connection goes to that exact address with the original Host header."""
    lookups: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def _resolve(host, port, *a, **kw):
        lookups.append(host)
        if host != "hooks.example.test":
            return real_getaddrinfo(host, port, *a, **kw)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(url_validator.socket, "getaddrinfo", _resolve)
    monkeypatch.setattr(url_validator, "_is_public_ip", lambda ip: True)

    with _Server(_Ok) as srv:
        url = f"http://hooks.example.test:{srv.port}/hooks"
        _register(monkeypatch, {"webhook_id": "wh1", "url": url, "hmac_secret": "s" * 64})
        await webhook_sender.deliver_event("acme-1", "parse.completed", {"job_id": "j1"})

    assert len(srv.requests) == 1
    assert srv.requests[0]["headers"]["Host"] == f"hooks.example.test:{srv.port}"
    assert lookups.count("hooks.example.test") == 1  # the validator's lookup, and no second one


async def test_an_attempt_has_an_overall_deadline(monkeypatch):
    monkeypatch.setattr(url_validator, "_is_public_ip", lambda ip: True)
    monkeypatch.setattr(webhook_sender, "_RETRY_DELAYS", [])
    monkeypatch.setattr(webhook_sender, "_HTTP_TIMEOUT", 0.6)

    with _Server(_Drip) as srv:
        url = f"http://127.0.0.1:{srv.port}/hooks"
        _register(monkeypatch, {"webhook_id": "wh1", "url": url, "hmac_secret": "s" * 64})
        started = time.monotonic()
        await webhook_sender.deliver_event("acme-1", "parse.completed", {"job_id": "j1"})
        elapsed = time.monotonic() - started

    assert elapsed < 1.2, f"one attempt ran {elapsed:.2f}s past its 0.6s deadline"
    assert webhook_sender._circuit.get(url, (0, 0.0))[0] == 1


async def test_webhook_url_is_never_logged(monkeypatch):
    logged: list = []

    class _Log:
        def __getattr__(self, _level):
            return lambda event, **kw: logged.append((event, kw))

    monkeypatch.setattr(webhook_sender, "log", _Log())
    monkeypatch.setattr(url_validator, "_is_public_ip", lambda ip: True)
    monkeypatch.setattr(webhook_sender, "_RETRY_DELAYS", [0])

    class _Fail(_Ok):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(500)
            self.end_headers()

    with _Server(_Fail) as srv:
        url = f"http://127.0.0.1:{srv.port}/hooks?token=s3cr3t-token"
        _register(monkeypatch, {"webhook_id": "wh1", "url": url, "hmac_secret": "s" * 64})
        await webhook_sender.deliver_event("acme-1", "parse.completed", {"job_id": "j1"})

    assert logged
    assert "s3cr3t" not in repr(logged)
    assert "wh1" in repr(logged)
