"""
End-to-end webhook delivery: the real sender against a real HTTP receiver.

`app/workers/webhook_sender.py` had no test cover at all, which is why a signature
mismatch at a client could only be argued about from logs. These tests run the
actual `deliver_event` against a live socket and verify the delivery with the
verifier snippet published in `docs/CLIENT_INTEGRATION_GUIDE.md` §6 - VERBATIM.
If the guide and the sender ever drift apart, this fails.

They also pin the two failure modes that have cost us real integration time:
  * a receiver that verifies a RE-SERIALIZED body instead of the raw bytes
  * more than one active registration, each signing with its own secret

The SSRF guard in `validate_webhook_url` legitimately refuses to POST to loopback,
so it is stubbed here. That guard is exercised in its own tests; what is under test
here is the signing and retry contract.
"""

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.workers import webhook_sender

# -- The published verifier, copied verbatim from CLIENT_INTEGRATION_GUIDE.md §6 ----
# Do not "improve" this. Its whole value is being byte-for-byte what we tell clients
# to run. If our sender stops satisfying it, the guide is wrong and clients break.

def verify(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:      # reject replays > 5 min
        return False
    message  = f"{timestamp}.".encode() + raw_body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# -- A real receiver on a real socket ----------------------------------------------

class _Receiver:
    """Captures raw bodies + headers, and replies with a scripted status code."""

    def __init__(self, statuses: list[int] | None = None):
        self.requests: list[dict] = []
        self._statuses = list(statuses or [])
        captured = self.requests
        statuses_ref = self._statuses

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                captured.append({
                    # The RAW bytes off the wire. Everything hinges on these.
                    "raw_body": self.rfile.read(length),
                    "headers": dict(self.headers),
                })
                status = statuses_ref.pop(0) if statuses_ref else 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args) -> None:
                pass  # keep pytest output clean

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/hooks/resume"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Receiver":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _allow_loopback_and_reset(monkeypatch):
    monkeypatch.setattr(webhook_sender, "validate_webhook_url", lambda url: None)
    # The circuit breaker is process-global; a previous test's failures must not
    # silently skip a later test's delivery.
    webhook_sender._circuit.clear()
    yield
    webhook_sender._circuit.clear()


def _register(monkeypatch, *hooks: dict) -> None:
    monkeypatch.setattr(
        webhook_sender.db, "get_active_webhooks_for_event",
        lambda company_id, event: list(hooks),
    )


SECRET = "a" * 64        # same shape as generate_webhook_secret() -> token_hex(32)
OTHER_SECRET = "b" * 64

PAYLOAD = {
    "job_id": "01M07ZG3R9M1NQX9C1N722DH41",
    "data": {"personal_info": {"full_name": "Dr James Mitchell", "email": "j@example.com"}},
    "partial": False,
    "warnings": [],
}


# -- The contract ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_verifies_with_the_published_verifier(monkeypatch):
    """THE contract test: what we send satisfies what we documented."""
    with _Receiver() as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    assert len(rx.requests) == 1
    got = rx.requests[0]
    assert verify(
        SECRET,
        got["headers"]["X-Timestamp"],
        got["raw_body"],
        got["headers"]["X-Signature"],
    )


@pytest.mark.asyncio
async def test_delivery_carries_the_documented_headers_and_envelope(monkeypatch):
    with _Receiver() as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    got = rx.requests[0]
    assert got["headers"]["X-Event"] == "parse.completed"
    assert got["headers"]["Content-Type"] == "application/json"
    assert got["headers"]["X-Signature"].startswith("sha256=")
    # Fresh enough to pass a receiver's 5-minute replay window.
    assert abs(time.time() - int(got["headers"]["X-Timestamp"])) < 60
    # The event name rides in the body as well as the header.
    body = json.loads(got["raw_body"])
    assert body["event"] == "parse.completed"
    assert body["job_id"] == PAYLOAD["job_id"]


@pytest.mark.asyncio
async def test_each_registration_is_signed_with_its_own_secret(monkeypatch):
    """Two active registrations: each delivery must verify under ITS OWN secret and
    NOT under the other's. This is the shape of the GigHealth UAT mismatch - the
    sender is correct, but a receiver holding the other registration's secret
    rejects 100% of deliveries."""
    with _Receiver() as rx_a, _Receiver() as rx_b:
        _register(
            monkeypatch,
            {"url": rx_a.url, "hmac_secret": SECRET},
            {"url": rx_b.url, "hmac_secret": OTHER_SECRET},
        )
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

        a, b = rx_a.requests[0], rx_b.requests[0]

    assert verify(SECRET, a["headers"]["X-Timestamp"], a["raw_body"], a["headers"]["X-Signature"])
    assert verify(OTHER_SECRET, b["headers"]["X-Timestamp"], b["raw_body"], b["headers"]["X-Signature"])
    # The cross-check: the wrong secret fails, which is what a client sees as a 401.
    assert not verify(OTHER_SECRET, a["headers"]["X-Timestamp"], a["raw_body"], a["headers"]["X-Signature"])


@pytest.mark.asyncio
async def test_verifying_a_reserialized_body_fails(monkeypatch):
    """The most common client-side cause of "invalid signature".

    A receiver that runs its JSON body-parser first and then signs
    `json.dumps(parsed)` re-encodes the bytes - different separators, different
    key order - and every delivery fails verification even with the right secret.
    Pinned here so the guide's "verify against the RAW body" is a tested claim.
    """
    with _Receiver() as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    got = rx.requests[0]
    reserialized = json.dumps(json.loads(got["raw_body"]), separators=(",", ":")).encode()

    assert reserialized != got["raw_body"]
    assert not verify(SECRET, got["headers"]["X-Timestamp"], reserialized,
                      got["headers"]["X-Signature"])
    # ...and the raw bytes do verify, so the secret was never the problem.
    assert verify(SECRET, got["headers"]["X-Timestamp"], got["raw_body"],
                  got["headers"]["X-Signature"])


# -- Retry / delivery semantics ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_rejected_delivery_is_not_retried(monkeypatch):
    """A 401 (the signature-mismatch case) is a 4xx: delivered once, never retried,
    event permanently dropped. Documented behaviour - pinned so the cost of a
    misconfigured secret stays visible."""
    with _Receiver(statuses=[401]) as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    assert len(rx.requests) == 1


@pytest.mark.asyncio
async def test_a_5xx_is_retried_until_it_succeeds(monkeypatch):
    monkeypatch.setattr(webhook_sender, "_RETRY_DELAYS", [0, 0, 0])

    with _Receiver(statuses=[500, 503, 200]) as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    assert len(rx.requests) == 3
    # Retries reuse one timestamp+signature, so a receiver's replay cache keys
    # cleanly on the signature instead of seeing three distinct-looking events.
    sigs = {r["headers"]["X-Signature"] for r in rx.requests}
    assert len(sigs) == 1
    last = rx.requests[-1]
    assert verify(SECRET, last["headers"]["X-Timestamp"], last["raw_body"],
                  last["headers"]["X-Signature"])


@pytest.mark.asyncio
async def test_the_retry_ladder_fits_inside_the_callers_timeout():
    """The worker wraps delivery in `asyncio.wait_for(..., _WEBHOOK_TIMEOUT)`.

    Regression: the ladder slept 2+5+10=17s while that outer budget was 15s, so the
    final attempt was cancelled mid-ladder AND `_record_failure` never ran - which
    left the circuit breaker permanently shut, unable to open no matter how many
    deliveries failed. The outer budget must cover the whole ladder.
    """
    from app.workers.background import _WEBHOOK_TIMEOUT

    sleeps = sum(webhook_sender._RETRY_DELAYS)
    attempts = len(webhook_sender._RETRY_DELAYS) + 1
    worst_case = sleeps + attempts * webhook_sender._HTTP_TIMEOUT

    assert _WEBHOOK_TIMEOUT >= worst_case, (
        f"delivery can take up to {worst_case}s but the caller cancels at "
        f"{_WEBHOOK_TIMEOUT}s - the last attempts are unreachable"
    )


@pytest.mark.asyncio
async def test_sustained_failure_opens_the_circuit(monkeypatch):
    """After CIRCUIT_OPEN_AFTER exhausted deliveries a dead URL is skipped, so one
    broken receiver cannot keep costing every later parse its full retry ladder."""
    monkeypatch.setattr(webhook_sender, "_RETRY_DELAYS", [0, 0, 0])

    with _Receiver(statuses=[500] * 100) as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        for _ in range(webhook_sender.CIRCUIT_OPEN_AFTER):
            await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

        assert webhook_sender._circuit_open(rx.url)
        before = len(rx.requests)
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)
        assert len(rx.requests) == before, "circuit was open; nothing should be sent"


@pytest.mark.asyncio
async def test_a_success_closes_the_circuit(monkeypatch):
    monkeypatch.setattr(webhook_sender, "_RETRY_DELAYS", [0, 0, 0])

    with _Receiver(statuses=[500, 500, 500, 500, 200]) as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)
        assert webhook_sender._circuit.get(rx.url, (0, 0.0))[0] == 1
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    assert rx.url not in webhook_sender._circuit


@pytest.mark.asyncio
async def test_no_registration_is_a_no_op(monkeypatch):
    _register(monkeypatch)  # nothing registered
    await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)


@pytest.mark.asyncio
async def test_an_unsafe_url_is_skipped_not_delivered(monkeypatch):
    """The SSRF re-check at delivery time must drop the hook, not raise into the
    worker's finally-block bookkeeping."""
    def _reject(url: str) -> None:
        raise webhook_sender.UnsafeWebhookURLError("resolves to a private address")

    monkeypatch.setattr(webhook_sender, "validate_webhook_url", _reject)

    with _Receiver() as rx:
        _register(monkeypatch, {"url": rx.url, "hmac_secret": SECRET})
        await webhook_sender.deliver_event("acme-1", "parse.completed", PAYLOAD)

    assert rx.requests == []
