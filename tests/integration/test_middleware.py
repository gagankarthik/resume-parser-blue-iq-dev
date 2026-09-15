"""Request middleware (A9 / L8).

The two BaseHTTPMiddleware classes ran in the reverse of the documented order, so:
every request log had request_id=""; a 413 and an unhandled exception produced no
request log line; a 500 carried no X-Request-ID (while its body told the user to
quote it) and no CORS headers. A client-supplied X-Request-ID was echoed unchecked.
"""

import structlog
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import get_settings


class _RecordingLog:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []

    def _record(self, level):
        def _log(event, **kw):
            self.calls.append((level, event, kw, dict(structlog.contextvars.get_contextvars())))
        return _log

    def __getattr__(self, level):
        return self._record(level)

    def request_lines(self):
        return [(kw, ctx) for level, event, kw, ctx in self.calls if event == "request"]


def _app_with_boom(monkeypatch):
    recorder = _RecordingLog()
    monkeypatch.setattr(main, "log", recorder)
    monkeypatch.setattr(get_settings(), "cors_allowed_origins", "")  # dev default: "*"
    app = main.create_app()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("database password is hunter2")

    return TestClient(app, raise_server_exceptions=False), recorder


def test_unhandled_error_carries_request_id_and_cors(monkeypatch):
    client, recorder = _app_with_boom(monkeypatch)

    resp = client.get("/boom", headers={"Origin": "https://ui.example"})

    assert resp.status_code == 500
    assert resp.headers["X-Request-ID"]
    assert resp.json()["error"]["request_id"] == resp.headers["X-Request-ID"]
    assert resp.json()["error"]["error_code"] == "INTERNAL_ERROR"
    assert "hunter2" not in resp.text
    assert resp.headers.get("access-control-allow-origin")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # Logged with a traceback, and the request line is still written.
    assert any(level == "exception" for level, *_ in recorder.calls)
    lines = recorder.request_lines()
    assert len(lines) == 1 and lines[0][0]["status"] == 500


def test_request_log_carries_the_request_id(monkeypatch):
    client, recorder = _app_with_boom(monkeypatch)

    resp = client.get("/api/v1/resume/job/x")  # 401, no auth

    (kw, ctx), = recorder.request_lines()
    assert kw["status"] == 401
    assert "request_id" not in kw or kw["request_id"]
    assert ctx.get("request_id") == resp.headers["X-Request-ID"]


def test_oversized_request_is_logged(monkeypatch):
    client, recorder = _app_with_boom(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    monkeypatch.setattr(settings, "max_request_overhead_bytes", 100)

    resp = client.post("/api/v1/resume/upload-url", content=b"x" * 500,
                       headers={"Content-Type": "application/json"})

    assert resp.status_code == 413
    assert resp.headers["X-Request-ID"]
    assert [kw["status"] for kw, _ in recorder.request_lines()] == [413]


def test_client_request_id_is_echoed_only_when_safe(monkeypatch):
    client, _ = _app_with_boom(monkeypatch)

    good = client.get("/api/v1/health", headers={"X-Request-ID": "abc-123_DEF.9"})
    assert good.headers["X-Request-ID"] == "abc-123_DEF.9"

    for bad in ("x" * 500, "has space", "<script>"):
        resp = client.get("/api/v1/health", headers={"X-Request-ID": bad})
        assert resp.headers["X-Request-ID"] != bad
        assert 0 < len(resp.headers["X-Request-ID"]) <= 128
