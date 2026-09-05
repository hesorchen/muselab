from __future__ import annotations

import asyncio
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from claude_agent_sdk import HookEventMessage, SystemMessage


def _hook(
    subtype: str,
    *,
    hook_id: str = "hook-1",
    event: str = "PreToolUse",
    **data,
):
    payload = {"hook_id": hook_id, "hook_event": event, **data}
    if subtype == "hook_progress":
        return SystemMessage(subtype=subtype, data=payload)
    return HookEventMessage(
        subtype=subtype,
        data=payload,
        hook_event_name=event,
        session_id="session-a",
        uuid=f"uuid-{subtype}",
    )


def test_started_and_response_persist_only_privacy_safe_projection(
    app_module,
    temp_root,
):
    from backend import hook_traces
    from backend import sessions as sess

    sid = "trace-session-a"
    started = hook_traces.observe(
        sid,
        _hook(
            "hook_started",
            command="cat /private/file",
            headers={"Authorization": "Bearer secret"},
        ),
        turn_id="turn-1",
        observed_at_ms=1_000,
    )
    finished = hook_traces.observe(
        sid,
        _hook(
            "hook_response",
            exit_code=7,
            outcome="failed",
            output="never persist this output",
            stdout="never persist this stdout",
            stderr="never persist this stderr",
        ),
        turn_id="turn-1",
        observed_at_ms=1_275,
    )

    assert started == {
        "trace_id": "hook-1",
        "hook_event": "PreToolUse",
        "status": "running",
        "exit_code": None,
        "started_at_ms": 1000,
        "finished_at_ms": None,
        "duration_ms": None,
        "updated_at_ms": 1000,
        "turn_id": "turn-1",
        "origin": "foreground",
    }
    assert finished["status"] == "failed"
    assert finished["exit_code"] == 7
    assert finished["duration_ms"] == 275

    path = sess.SESS_DIR / "hook-traces" / f"{sid}.json"
    raw = path.read_text(encoding="utf-8")
    for secret in (
        "cat /private/file",
        "Bearer secret",
        "never persist this output",
        "never persist this stdout",
        "never persist this stderr",
    ):
        assert secret not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_progress_updates_timing_without_persisting_output(app_module):
    from backend import hook_traces

    sid = "trace-session-progress"
    hook_traces.observe(
        sid,
        _hook("hook_started"),
        turn_id="turn-1",
        observed_at_ms=2_000,
    )
    progress = hook_traces.observe(
        sid,
        _hook("hook_progress", output="secret incremental output"),
        turn_id="turn-1",
        observed_at_ms=2_050,
    )

    assert progress["status"] == "running"
    assert progress["updated_at_ms"] == 2050
    assert "secret" not in json.dumps(hook_traces.list_traces(sid), ensure_ascii=False)


def test_progress_does_not_rewrite_durable_trace(app_module, monkeypatch):
    from backend import hook_traces

    sid = "trace-session-progress-writes"
    hook_traces.observe(
        sid,
        _hook("hook_started"),
        turn_id="turn-1",
        observed_at_ms=2_000,
    )
    writes = 0
    real_write = hook_traces._write_locked

    def counted_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return real_write(*args, **kwargs)

    monkeypatch.setattr(hook_traces, "_write_locked", counted_write)
    for offset in range(1, 101):
        progress = hook_traces.observe(
            sid,
            _hook("hook_progress"),
            turn_id="turn-1",
            observed_at_ms=2_000 + offset,
        )
        assert progress is not None

    assert writes == 0
    assert hook_traces.list_traces(sid)[0]["updated_at_ms"] == 2_100

    hook_traces.observe(
        sid,
        _hook("hook_response", exit_code=0),
        turn_id="turn-1",
        observed_at_ms=2_200,
    )
    assert writes == 1
    durable = json.loads(hook_traces._trace_path(sid).read_text(encoding="utf-8"))["traces"][0]
    assert durable["status"] == "succeeded"
    assert durable["updated_at_ms"] == 2_200
    assert durable["finished_at_ms"] == 2_200


def test_trace_writes_for_distinct_sessions_do_not_share_io_lock(
    app_module,
    monkeypatch,
):
    from backend import hook_traces

    blocked = threading.Event()
    release = threading.Event()
    real_write = hook_traces.write_private_bytes
    slow_sid = "trace-session-slow"
    fast_sid = next(
        f"trace-session-fast-{index}"
        for index in range(256)
        if hook_traces._trace_lock(f"trace-session-fast-{index}")
        is not hook_traces._trace_lock(slow_sid)
    )

    def controlled_write(path, data):
        if path.name == f"{slow_sid}.json":
            blocked.set()
            assert release.wait(timeout=2)
        return real_write(path, data)

    monkeypatch.setattr(hook_traces, "write_private_bytes", controlled_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(
            hook_traces.observe,
            slow_sid,
            _hook("hook_started", hook_id="hook-slow"),
        )
        assert blocked.wait(timeout=1)
        fast = pool.submit(
            hook_traces.observe,
            fast_sid,
            _hook("hook_started", hook_id="hook-fast"),
        )
        try:
            assert fast.result(timeout=1)["trace_id"] == "hook-fast"
        finally:
            release.set()
        assert slow.result(timeout=1)["trace_id"] == "hook-slow"


def test_progress_without_stable_started_identity_is_not_guessed(app_module):
    from backend import hook_traces

    sid = "trace-session-no-guess"
    hook_traces.observe(
        sid,
        _hook("hook_started", hook_id="hook-a"),
        turn_id="turn-1",
        observed_at_ms=3_000,
    )

    assert (
        hook_traces.observe(
            sid,
            _hook("hook_progress", hook_id="hook-b", output="unbound"),
            turn_id="turn-1",
            observed_at_ms=3_010,
        )
        is None
    )
    assert len(hook_traces.list_traces(sid)) == 1


def test_trace_store_is_bounded(app_module, monkeypatch):
    from backend import hook_traces

    monkeypatch.setattr(hook_traces, "_MAX_TRACES", 3)
    sid = "trace-session-bounded"
    for index in range(5):
        hook_traces.observe(
            sid,
            _hook("hook_started", hook_id=f"hook-{index}"),
            turn_id="turn-1",
            observed_at_ms=4_000 + index,
        )

    assert [row["trace_id"] for row in hook_traces.list_traces(sid)] == [
        "hook-2",
        "hook-3",
        "hook-4",
    ]


def test_authenticated_trace_endpoint_supports_turn_filter(
    app_module,
    client,
    auth,
):
    from backend import hook_traces
    from backend import sessions as sess

    session = sess.create_session("hook trace api")
    sid = session["id"]
    hook_traces.observe(
        sid,
        _hook("hook_started", hook_id="hook-api"),
        turn_id="turn-api",
        observed_at_ms=5_000,
    )
    path = f"/api/chat/sessions/{sid}/hook-traces"

    assert client.get(path).status_code == 401
    response = client.get(path, headers=auth)
    filtered = client.get(path, params={"turn_id": "other"}, headers=auth)

    assert response.status_code == 200, response.text
    assert [row["trace_id"] for row in response.json()["traces"]] == ["hook-api"]
    assert filtered.status_code == 200
    assert filtered.json()["traces"] == []


@pytest.mark.asyncio
async def test_stream_observer_publishes_safe_trace_to_active_turn(
    app_module,
    monkeypatch,
):
    from backend import chat as chat_mod

    sid = "observer-session"
    broadcast = chat_mod.TurnBroadcast(sid, model="claude-sonnet-4-6")
    chat_mod._active_turns[sid] = broadcast
    safe = {
        "trace_id": "hook-observer",
        "hook_event": "Stop",
        "status": "succeeded",
        "exit_code": 0,
        "started_at_ms": 10,
        "finished_at_ms": 20,
        "duration_ms": 10,
        "updated_at_ms": 20,
        "turn_id": broadcast.turn_id,
        "origin": "foreground",
    }
    monkeypatch.setattr(chat_mod.hook_traces, "observe", lambda *a, **k: safe)
    try:
        await chat_mod._observe_sdk_stream_message(
            (sid, "model", "auto", ""),
            _hook("hook_response", event="Stop", exit_code=0),
        )
        await asyncio.to_thread(chat_mod._hook_diagnostic_worker.close)
        await asyncio.sleep(0)
    finally:
        chat_mod._active_turns.pop(sid, None)

    events = list(broadcast.replay_events())
    assert len(events) == 1
    assert events[0]["event"] == "hook_trace"
    payload = json.loads(events[0]["data"])
    assert payload["trace_id"] == "hook-observer"
    assert payload["turn_id"] == broadcast.turn_id
