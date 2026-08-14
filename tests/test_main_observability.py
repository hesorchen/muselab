"""Privacy and timing boundaries for the top-level ASGI observability layer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _scope(*, route: str | None = "/api/items/{item_id}") -> dict:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/private/123e4567-e89b-42d3-a456-426614174000/report.md",
        "raw_path": b"/private/123e4567-e89b-42d3-a456-426614174000/report.md",
        "query_string": b"prompt=private-content&path=secret.md",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    if route is not None:
        scope["route"] = SimpleNamespace(path=route)
    return scope


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_slow_http_event_uses_route_template_not_concrete_target(
    app_module, monkeypatch,
):
    events = []
    sent = []

    async def downstream(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{",
                    "more_body": True})
        await send({"type": "http.response.body", "body": b"}"})

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: 1.0)
    monkeypatch.setattr(app_module, "_perf_elapsed_ms", lambda _started: 750)
    monkeypatch.setattr(app_module, "_perf_is_slow", lambda value: value >= 500)
    monkeypatch.setattr(
        app_module, "_emit_http_perf", lambda **fields: events.append(fields))

    middleware = app_module._RequestPerformanceMiddleware(downstream)
    await middleware(_scope(), _receive, capture)

    assert events == [{
        "method": "GET",
        "route": "/api/items/{item_id}",
        "status_code": 200,
        "duration_ms": 750,
        "headers_ms": 750,
        "response_bytes": 2,
        "phase": "complete",
        "error_kind": None,
    }]
    rendered = repr(events)
    assert "private-content" not in rendered
    assert "secret.md" not in rendered
    assert "123e4567" not in rendered
    assert "report.md" not in rendered


@pytest.mark.asyncio
async def test_fast_success_is_silent_but_fast_error_is_logged(
    app_module, monkeypatch,
):
    events = []
    status = 200

    async def downstream(_scope, _receive, send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: 1.0)
    monkeypatch.setattr(app_module, "_perf_elapsed_ms", lambda _started: 12)
    monkeypatch.setattr(app_module, "_perf_is_slow", lambda _value: False)
    monkeypatch.setattr(
        app_module, "_emit_http_perf", lambda **fields: events.append(fields))
    middleware = app_module._RequestPerformanceMiddleware(downstream)

    await middleware(_scope(), _receive, lambda _message: asyncio.sleep(0))
    assert events == []

    status = 503
    await middleware(_scope(), _receive, lambda _message: asyncio.sleep(0))
    assert len(events) == 1
    assert events[0]["status_code"] == 503
    assert events[0]["phase"] == "complete"
    assert events[0]["headers_ms"] == 12
    assert events[0]["response_bytes"] == 2


@pytest.mark.asyncio
async def test_sse_records_handshake_once_not_stream_lifetime(
    app_module, monkeypatch,
):
    events = []
    elapsed_calls = 0

    def elapsed(_started):
        nonlocal elapsed_calls
        elapsed_calls += 1
        return 800 if elapsed_calls == 1 else 60_000

    async def downstream(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: x\n\n",
                    "more_body": True})
        await send({"type": "http.response.body", "body": b"",
                    "more_body": False})

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: 1.0)
    monkeypatch.setattr(app_module, "_perf_elapsed_ms", elapsed)
    monkeypatch.setattr(app_module, "_perf_is_slow", lambda value: value >= 500)
    monkeypatch.setattr(
        app_module, "_emit_http_perf", lambda **fields: events.append(fields))

    middleware = app_module._RequestPerformanceMiddleware(downstream)
    await middleware(_scope(route="/api/chat/stream"), _receive,
                     lambda _message: asyncio.sleep(0))

    assert elapsed_calls == 1
    assert len(events) == 1
    assert events[0]["route"] == "/api/chat/stream"
    assert events[0]["phase"] == "handshake"
    assert events[0]["duration_ms"] == 800
    assert events[0]["headers_ms"] == 800
    assert events[0]["response_bytes"] == 0


@pytest.mark.asyncio
async def test_unmatched_error_never_logs_concrete_path(
    app_module, monkeypatch,
):
    events = []

    async def downstream(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 404,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"not found"})

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: 1.0)
    monkeypatch.setattr(app_module, "_perf_elapsed_ms", lambda _started: 3)
    monkeypatch.setattr(app_module, "_perf_is_slow", lambda _value: False)
    monkeypatch.setattr(
        app_module, "_emit_http_perf", lambda **fields: events.append(fields))

    middleware = app_module._RequestPerformanceMiddleware(downstream)
    await middleware(_scope(route=None), _receive,
                     lambda _message: asyncio.sleep(0))

    assert events[0]["route"] == "<unmatched>"
    assert "private" not in repr(events)
    assert "report.md" not in repr(events)


@pytest.mark.asyncio
async def test_event_loop_monitor_emits_only_threshold_crossing(
    app_module, monkeypatch,
):
    events = []
    timestamps = iter((0.0, 1.3))
    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: next(timestamps))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        app_module, "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )

    with pytest.raises(asyncio.CancelledError):
        await app_module._monitor_event_loop_lag()

    assert events == [("runtime.loop_lag", {"lag_ms": 300})]
