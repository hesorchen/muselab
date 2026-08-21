"""Privacy and timing boundaries for the top-level ASGI observability layer."""

from __future__ import annotations

import asyncio
import inspect
import threading
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


def test_event_loop_monitor_emits_only_threshold_crossing(
    app_module, monkeypatch,
):
    events = []
    timestamps = iter((0.0, 1.3))
    sleeps = 0

    class FakeWatchdog:
        def __init__(self, *_args, **_kwargs):
            self.started = False
            self.stopped = False
            self.heartbeats = []

        def start(self):
            self.started = True

        def heartbeat(self, observed_at):
            self.heartbeats.append(observed_at)

        def stop(self):
            self.stopped = True

    watchdog = FakeWatchdog()

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "_perf_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_perf_monotonic", lambda: next(timestamps))
    monkeypatch.setattr(app_module, "_EventLoopStallWatchdog",
                        lambda *_args, **_kwargs: watchdog)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        app_module, "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_module._monitor_event_loop_lag())

    assert events == [(
        "runtime.loop_lag",
        {
            "site": "event_loop",
            "session": "none",
            "duration_ms": 300,
            "file_size": 0,
            "lag_ms": 300,
        },
    )]
    assert watchdog.started is True
    assert watchdog.heartbeats == [1.3]
    assert watchdog.stopped is True


def test_backfill_loads_messages_and_updates_counts_off_loop(
    app_module, monkeypatch,
):
    from backend import chat, sessions
    import claude_agent_sdk

    loop_thread_id = threading.get_ident()
    worker_calls = []
    updates = []

    def assert_worker(name):
        worker_calls.append((name, threading.get_ident()))
        assert threading.get_ident() != loop_thread_id

    def list_sessions():
        assert_worker("list")
        return [{"id": "session-1", "turn_count": 0}]

    def load_messages(sid, *, directory):
        assert_worker("load")
        assert sid == "session-1"
        assert directory
        return [True, False, True]

    def bump_session(sid, **counts):
        assert_worker("update")
        updates.append((sid, counts))

    monkeypatch.setattr(sessions, "list_sessions", list_sessions)
    monkeypatch.setattr(sessions, "session_workspace", lambda _sid: "/workspace")
    monkeypatch.setattr(sessions, "bump_session", bump_session)
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", load_messages)
    monkeypatch.setattr(chat, "_is_real_user_prompt", bool)

    asyncio.run(app_module._backfill_turn_counts())

    assert [name for name, _thread_id in worker_calls] == ["list", "load", "update"]
    assert updates == [("session-1", {"message_count": 3, "turn_count": 2})]
    assert (sessions.SESS_DIR / ".backfill_done").exists()


def test_severe_loop_watchdog_attributes_privacy_safe_site_and_rate_limits(
    app_module, monkeypatch,
):
    events = []
    monkeypatch.setattr(
        app_module, "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )

    loop_thread_id = threading.get_ident()
    blocked_frame = inspect.currentframe()
    assert blocked_frame is not None
    monkeypatch.setattr(
        app_module.sys, "_current_frames", lambda: {loop_thread_id: blocked_frame}
    )
    watchdog = app_module._EventLoopStallWatchdog(
        loop_thread_id,
        10.0,
        threshold_s=5.0,
        rate_limit_s=60.0,
        poll_s=1.0,
    )
    watchdog._check_once(14.9)
    watchdog._check_once(15.0)
    watchdog._check_once(40.0)
    watchdog._check_once(75.0)

    assert len(events) == 2
    assert events[0][0] == "runtime.loop_stall"
    assert events[0][1]["lag_ms"] == 5000
    assert events[0][1]["site"].startswith(
        "tests.test_main_observability:test_severe_loop_watchdog_"
    )
    assert "/home/" not in repr(events)
    assert "prompt" not in repr(events)
    assert events[1][1]["lag_ms"] == 65000
