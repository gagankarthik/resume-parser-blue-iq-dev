"""
GigHealth Partner API contract - envelope, documented error codes, and 429 backoff.

Pins the behaviour the partner guide specifies:
  * x-api-key auth; 401 = bad/revoked key, 403 = permission not granted for the endpoint
  * 429 = per-second burst OR monthly quota; "back off and retry; do not loop tightly"
  * every response uses {success, message, data, errors} - including error bodies

The point of classifying these rather than swallowing them: a missing key, a missing
permission and an exhausted quota need three completely different fixes, and used to
be one indistinguishable empty list.
"""

import httpx
import pytest

from app.services.normalization import gig_api


def _envelope(data, success=True, message="ok"):
    return {"success": success, "message": message, "data": data, "errors": []}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- Envelope -----------------------------------------------------------------

def test_unwrap_returns_data_rows():
    assert gig_api.unwrap(_envelope([{"id": 1}, {"id": 2}])) == [{"id": 1}, {"id": 2}]


def test_unwrap_rejects_success_false_even_on_a_200():
    """The guide says error bodies use the SAME envelope. A 200 carrying success:false
    must not be read as 'no data' - that is how a real failure becomes a silent null."""
    with pytest.raises(gig_api.GigApiError) as exc:
        gig_api.unwrap(_envelope([], success=False, message="not authorized"))
    assert "not authorized" in exc.value.message


def test_unwrap_rejects_a_missing_data_array():
    with pytest.raises(gig_api.GigApiError):
        gig_api.unwrap({"success": True, "message": "ok"})


# -- Documented status codes --------------------------------------------------

@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "auth"), (403, "forbidden"), (429, "rate_limited"), (500, "server"), (400, "malformed")],
)
def test_classify_maps_the_documented_statuses(status, kind):
    assert gig_api.classify(status) == kind


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (403, "forbidden")])
async def test_auth_failures_are_raised_with_the_api_message(status, kind):
    def handler(request):
        return httpx.Response(
            status, json=_envelope([], success=False,
                                   message="Your API key is not authorized to access this resource"),
        )

    async with _client(handler) as c:
        with pytest.raises(gig_api.GigApiError) as exc:
            await gig_api.get_async(c, "http://x/cities", "k")
    assert exc.value.kind == kind
    assert exc.value.status == status
    assert "not authorized" in exc.value.message


# -- 429 backoff --------------------------------------------------------------

async def test_429_is_retried_then_succeeds(monkeypatch):
    """'On a 429, back off and retry; do not loop tightly.'"""
    slept: list[float] = []

    async def _no_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(gig_api.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json=_envelope([], success=False, message="rate limited"))
        return httpx.Response(200, json=_envelope([{"id": 3512, "city": "Buffalo"}]))

    async with _client(handler) as c:
        rows = await gig_api.get_async(c, "http://x/cities", "k")

    assert calls["n"] == 2, "the 429 should have been retried once"
    assert slept, "the retry must back off, not loop tightly"
    assert rows == [{"id": 3512, "city": "Buffalo"}]


async def test_429_that_never_clears_gives_up_as_rate_limited(monkeypatch):
    """A monthly-quota 429 will keep 429ing - there is nothing to wait for until the
    1st. It must give up (bounded retries) and report itself as rate_limited, not spin."""
    async def _no_sleep(sec):
        return None

    monkeypatch.setattr(gig_api.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json=_envelope([], success=False, message="quota exceeded"))

    async with _client(handler) as c:
        with pytest.raises(gig_api.GigApiError) as exc:
            await gig_api.get_async(c, "http://x/cities", "k")

    assert exc.value.kind == "rate_limited"
    assert calls["n"] == gig_api._MAX_RETRIES + 1, "retries must be bounded"


async def test_retry_after_header_is_honoured(monkeypatch):
    slept: list[float] = []

    async def _no_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(gig_api.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json=_envelope([]))
        return httpx.Response(200, json=_envelope([]))

    async with _client(handler) as c:
        await gig_api.get_async(c, "http://x/cities", "k")

    assert slept == [2.0]


# -- Auth header --------------------------------------------------------------

async def test_sends_the_x_api_key_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=_envelope([]))

    async with _client(handler) as c:
        await gig_api.get_async(c, "http://x/cities", "secret-key")

    assert seen["key"] == "secret-key"


async def test_transport_error_is_classified_not_leaked():
    def handler(request):
        raise httpx.ConnectError("dns exploded")

    async with _client(handler) as c:
        with pytest.raises(gig_api.GigApiError) as exc:
            await gig_api.get_async(c, "http://x/cities", "k")
    assert exc.value.kind == "transport"
