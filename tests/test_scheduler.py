"""Tests for scheduler state, execution lifecycle, and schedule math."""
from __future__ import annotations

import asyncio
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def _sched_mod(app_module):
    """Pull the scheduler module out of the reloaded backend.* tree.

    conftest's `app_module` fixture reloads everything under `backend.`
    against a tmp ROOT, so importing `backend.scheduler` here resolves
    against the test-isolated state file (~/tmp/.muselab/scheduler.json),
    not the dev's real one."""
    from backend import scheduler as sched
    # Each test gets a fresh in-memory state — the fixture creates a new
    # temp ROOT every time, so _STATE_FILE doesn't exist on disk yet, but
    # the module-global `_state` may carry over from a prior test in the
    # same process. Reset explicitly for isolation.
    sched._state = {"tasks": {}, "history": [], "unread_count": 0}
    return sched


def _daily_at(hour: int = 9, minute: int = 0) -> dict:
    return {"kind": "daily", "hour": hour, "minute": minute,
            "tz_offset_minutes": 480}


# ---- create_task ----

def test_create_task_fresh_does_not_preallocate_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("daily-news", "summarize today",
                          _daily_at(9, 0), session_mode="fresh")
    assert t["session_mode"] == "fresh"
    # Fresh: session_id must be empty so the first run mints one.
    assert t["session_id"] == ""


def test_create_task_reuse_preallocates_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("daily-log", "continue the log",
                          _daily_at(9, 0), session_mode="reuse")
    assert t["session_mode"] == "reuse"
    # Reuse: session_id is set to the bound session at creation time.
    assert t["session_id"]
    # And the session actually exists on disk.
    from backend import sessions as sess
    listing = sess.list_sessions()
    assert any(s["id"] == t["session_id"] for s in listing)


def test_create_task_rejects_invalid_session_mode(app_module):
    sched = _sched_mod(app_module)
    import pytest
    with pytest.raises(ValueError):
        sched.create_task("x", "p", _daily_at(), session_mode="bogus")


def test_create_task_default_is_fresh(app_module):
    """No session_mode kwarg → fresh, per 2026-05-28 design choice."""
    sched = _sched_mod(app_module)
    t = sched.create_task("t", "p", _daily_at())
    assert t["session_mode"] == "fresh"
    assert t["session_id"] == ""


# ---- _effective_session_mode ----

def test_effective_session_mode_falls_back_to_reuse_for_legacy(app_module):
    """Tasks that predate the field (no session_mode key) must fall back
    to 'reuse' so their bound-session behavior is preserved."""
    sched = _sched_mod(app_module)
    legacy_task = {
        "id": "old-task",
        "name": "legacy",
        "prompt": "p",
        "session_id": "sess-abc",
        "schedule": _daily_at(),
        "enabled": True,
    }
    # No "session_mode" key at all — this is the migration scenario.
    assert sched._effective_session_mode(legacy_task) == "reuse"


def test_effective_session_mode_respects_explicit_value(app_module):
    sched = _sched_mod(app_module)
    assert sched._effective_session_mode(
        {"session_mode": "fresh"}) == "fresh"
    assert sched._effective_session_mode(
        {"session_mode": "reuse"}) == "reuse"


def test_sdk_turn_rejects_session_with_interactive_owner(
    app_module, monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat

    sid = "busy-session"
    chat._active_turns[sid] = type("Busy", (), {"done": False})()

    async def should_not_get_client(*_args, **_kwargs):
        raise AssertionError("scheduler must not touch an interactive runtime")

    monkeypatch.setattr(chat, "get_client", should_not_get_client)
    try:
        with pytest.raises(RuntimeError, match="interactive turn"):
            asyncio.run(sched._run_sdk_task_turn(sid, "model", "prompt"))
    finally:
        chat._active_turns.pop(sid, None)
        chat._session_runtime_locks.pop(sid, None)


def test_sdk_turn_rejects_session_with_background_watcher(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat

    sid = "busy-background-session"

    async def should_not_get_client(*_args, **_kwargs):
        raise AssertionError("scheduler must not steal the background pump")

    async def run():
        watcher = asyncio.create_task(asyncio.Event().wait())
        chat._task_watchers[sid] = watcher
        monkeypatch.setattr(chat, "get_client", should_not_get_client)
        try:
            with pytest.raises(RuntimeError, match="background task watcher"):
                await sched._run_sdk_task_turn(sid, "model", "prompt")
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            chat._task_watchers.pop(sid, None)
            chat._session_runtime_locks.pop(sid, None)

    asyncio.run(run())


def test_sdk_turn_preserves_session_effort_and_service_tier(
    app_module, monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat, sessions

    sid = "scheduled-fast-session"
    observed = {}

    class FakeClient:
        async def query(self, prompt):
            observed["prompt"] = prompt

        async def receive_response(self):
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=sid,
                total_cost_usd=0.0,
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    async def fake_get_client(**kwargs):
        observed.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(
        sessions, "get_session",
        lambda _sid: {"effort": "ultra", "service_tier": "fast"},
    )
    monkeypatch.setattr(chat, "get_client", fake_get_client)
    try:
        reply, error = asyncio.run(
            sched._run_sdk_task_turn(sid, "codex:gpt-5.6-sol", "prompt"))
    finally:
        chat._session_runtime_locks.pop(sid, None)

    assert (reply, error) == ("", None)
    assert observed["effort"] == "ultra"
    assert observed["service_tier"] == "fast"
    assert observed["permission"] == "bypassPermissions"
    assert observed["prompt"] == "prompt"


@pytest.mark.asyncio
async def test_sdk_turn_reads_through_pooled_stream_pump(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat, sessions

    class FakeStream:
        queue = None
        detached = False
        parked = False

        def attach_turn(self):
            self.queue = asyncio.Queue()
            return self.queue

        def detach_turn(self, queue):
            assert queue is self.queue
            self.detached = True

        def park_unconsumed(self, queue):
            assert queue is self.queue
            self.parked = True

    stream = FakeStream()

    class FakeClient:
        async def query(self, _prompt):
            assert stream.queue is not None
            stream.queue.put_nowait(AssistantMessage(
                content=[TextBlock(text="scheduled reply")],
                model="claude-sonnet-4-6",
            ))
            stream.queue.put_nowait(ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="scheduled-pump-session",
                total_cost_usd=0.0,
                usage={"input_tokens": 1, "output_tokens": 1},
            ))

        async def receive_response(self):
            raise AssertionError("pooled client must not open a second reader")
            yield

    async def fake_get_client(**_kwargs):
        return FakeClient()

    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda _sid: {"effort": "auto", "service_tier": ""},
    )
    monkeypatch.setattr(chat, "get_client", fake_get_client)
    monkeypatch.setattr(chat, "_stream_for", lambda _client: stream)

    try:
        reply, error = await sched._run_sdk_task_turn(
            "scheduled-pump-session", "claude-sonnet-4-6", "prompt")
    finally:
        chat._session_runtime_locks.pop("scheduled-pump-session", None)

    assert (reply, error) == ("scheduled reply", None)
    assert stream.detached is True
    assert stream.parked is True


@pytest.mark.asyncio
async def test_sdk_turn_rejects_pooled_eof_without_result(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat, sessions

    class FailedStream:
        _failure = RuntimeError("scheduler stream failed")

        def attach_turn(self):
            self.queue = asyncio.Queue()
            return self.queue

        def detach_turn(self, _queue):
            return None

        def park_unconsumed(self, _queue):
            return None

    stream = FailedStream()

    class FakeClient:
        async def query(self, _prompt):
            stream.queue.put_nowait(chat._STREAM_EOF)

    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda _sid: {"effort": "auto", "service_tier": ""},
    )
    monkeypatch.setattr(
        chat,
        "get_client",
        lambda **_kwargs: _async_value(FakeClient()),
    )
    monkeypatch.setattr(chat, "_stream_for", lambda _client: stream)

    try:
        with pytest.raises(RuntimeError, match="scheduler stream failed"):
            await sched._run_sdk_task_turn(
                "scheduled-eof-session", "claude-sonnet-4-6", "prompt")
    finally:
        chat._session_runtime_locks.pop("scheduled-eof-session", None)


@pytest.mark.asyncio
async def test_sdk_turn_skips_replayed_result_before_current_result(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import chat, sessions

    class FakeStream:
        _failure = None

        def attach_turn(self):
            self.queue = asyncio.Queue()
            return self.queue

        def detach_turn(self, _queue):
            return None

        def park_unconsumed(self, _queue):
            return None

    stream = FakeStream()

    def result(uuid):
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="scheduled-boundary-session",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
            uuid=uuid,
        )

    class FakeClient:
        async def query(self, _prompt):
            stream.queue.put_nowait(result("old-result"))
            stream.queue.put_nowait(AssistantMessage(
                content=[TextBlock(text="current reply")],
                model="claude-sonnet-4-6",
                uuid="current-assistant",
            ))
            stream.queue.put_nowait(result("current-result"))

    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda _sid: {"effort": "auto", "service_tier": ""},
    )
    monkeypatch.setattr(
        chat,
        "get_client",
        lambda **_kwargs: _async_value(FakeClient()),
    )
    monkeypatch.setattr(chat, "_stream_for", lambda _client: stream)
    monkeypatch.setattr(
        chat,
        "_session_message_uuids",
        lambda _sid, _model: frozenset({"old-result"}),
    )

    try:
        reply, error = await sched._run_sdk_task_turn(
            "scheduled-boundary-session", "claude-sonnet-4-6", "prompt")
    finally:
        chat._session_runtime_locks.pop("scheduled-boundary-session", None)

    assert (reply, error) == ("current reply", None)


async def _async_value(value):
    return value


def _execution_task(tid: str = "exec-task") -> dict:
    return {
        "id": tid,
        "name": "Execution task",
        "prompt": "produce a result",
        "session_id": f"session-{tid}",
        "session_mode": "reuse",
        "model": "",
    }


@pytest.mark.asyncio
async def test_execute_task_publishes_activity_and_success_history(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import activity as activity_module
    transitions = []

    async def run_turn(sid, _model, _prompt, **kwargs):
        activity_module.activity.start(
            sid,
            summary=kwargs["activity_summary"],
            kind="scheduled",
            source_id=kwargs["activity_source_id"],
            owner_id=kwargs["activity_owner_id"],
        )
        return "finished", None

    monkeypatch.setattr(sched, "_run_sdk_task_turn", run_turn)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda sid, **kwargs: transitions.append(("start", sid, kwargs)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda sid, status, **kwargs: transitions.append(
            ("finish", sid, status, kwargs)),
    )
    from backend import presence
    monkeypatch.setattr(presence, "recently_active", lambda: True)
    monkeypatch.setattr(presence, "last_seen_age", lambda: 0.0)

    task = _execution_task()
    await sched._execute_task(task)

    assert sched._state["history"][-1]["ok"] is True
    assert sched._state["history"][-1]["reply_preview"] == "finished"
    assert transitions[0] == (
        "start",
        task["session_id"],
        {
            "summary": task["name"],
            "kind": "scheduled",
            "source_id": task["id"],
            "owner_id": transitions[0][2]["owner_id"],
        },
    )
    assert transitions[-1][:3] == (
        "finish", task["session_id"], "completed")
    assert transitions[-1][3]["owner_id"] == transitions[0][2]["owner_id"]


@pytest.mark.asyncio
async def test_execute_task_cancellation_is_not_recorded_as_success(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import activity as activity_module
    entered = asyncio.Event()
    transitions = []

    async def blocked(sid, _model, _prompt, **kwargs):
        activity_module.activity.start(
            sid,
            summary=kwargs["activity_summary"],
            kind="scheduled",
            source_id=kwargs["activity_source_id"],
            owner_id=kwargs["activity_owner_id"],
        )
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sched, "_run_sdk_task_turn", blocked)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda sid, **kwargs: transitions.append(("start", sid, kwargs)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda sid, status, **kwargs: transitions.append(
            ("finish", sid, status, kwargs)),
    )

    task = _execution_task("cancel-task")
    running = asyncio.create_task(sched._execute_task(task))
    await asyncio.wait_for(entered.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    history = sched._state["history"]
    assert len(history) == 1
    assert history[0]["ok"] is False
    assert "cancelled" in history[0]["error"]
    assert transitions[-1][:3] == (
        "finish", task["session_id"], "cancelled")
    assert transitions[-1][3]["owner_id"] == transitions[0][2]["owner_id"]


@pytest.mark.asyncio
async def test_execute_task_has_bounded_unattended_runtime(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)

    async def blocked(*_args, **_kwargs):
        await asyncio.Event().wait()

    original_env_int = sched.env_int
    monkeypatch.setattr(sched, "_run_sdk_task_turn", blocked)
    monkeypatch.setattr(
        sched,
        "env_int",
        lambda name, default, **kwargs: (
            0.01 if name == "MUSELAB_SCHEDULER_TIMEOUT_S"
            else original_env_int(name, default, **kwargs)
        ),
    )
    from backend import presence
    monkeypatch.setattr(presence, "recently_active", lambda: True)
    monkeypatch.setattr(presence, "last_seen_age", lambda: 0.0)

    await sched._execute_task(_execution_task("timeout-task"))

    row = sched._state["history"][-1]
    assert row["ok"] is False
    assert "timed out" in row["error"]


# ---- delete_task ----

@pytest.mark.asyncio
async def test_delete_task_endpoint_joins_running_owner_before_purge(
    app_module,
    monkeypatch,
):
    sched = _sched_mod(app_module)
    from backend import activity as activity_module
    from backend import api_scheduler, chat

    task = _execution_task("delete-running-task")
    sched._state["tasks"][task["id"]] = task
    entered = asyncio.Event()
    transitions = []

    async def blocked_turn(sid, _model, _prompt, **kwargs):
        sched._mark_current_run_activity_started()
        activity_module.activity.start(
            sid,
            summary=kwargs["activity_summary"],
            kind="scheduled",
            source_id=kwargs["activity_source_id"],
            owner_id=kwargs["activity_owner_id"],
        )
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sched, "_run_sdk_task_turn", blocked_turn)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda sid, **_kwargs: transitions.append(("start", sid)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda sid, status, **_kwargs:
            transitions.append(("finish", sid, status)),
    )

    running = sched._track_task(
        asyncio.create_task(sched._execute_task(task)),
        task_id=task["id"],
        session_id=task["session_id"],
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    async def purge_after_join(sid):
        assert sid == task["session_id"]
        assert running.done()
        assert sched._state["history"] == []
        assert sched._state["unread_count"] == 0
        transitions.append(("purge", sid))
        return True

    monkeypatch.setattr(chat, "purge_session_storage_async", purge_after_join)

    assert await api_scheduler.delete_task_endpoint(task["id"]) == {
        "deleted": task["id"]
    }
    assert running.cancelled()
    assert task["id"] not in sched._state["tasks"]
    assert transitions == [
        ("start", task["session_id"]),
        ("finish", task["session_id"], "cancelled"),
        ("purge", task["session_id"]),
    ]


def test_delete_task_reuse_removes_bound_session(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("rt", "p", _daily_at(), session_mode="reuse")
    sid = t["session_id"]
    from backend import sessions as sess
    assert any(s["id"] == sid for s in sess.list_sessions())
    assert sched.delete_task(t["id"]) is True
    # Bound session gone after delete.
    assert all(s["id"] != sid for s in sess.list_sessions())


def test_delete_task_fresh_keeps_all_sessions(app_module):
    """fresh-mode tasks may have minted N independent run sessions.
    Deleting the task must NOT cascade-delete them (per user spec
    2026-05-28: 'past runs may be valuable history snapshots')."""
    sched = _sched_mod(app_module)
    t = sched.create_task("ft", "p", _daily_at(), session_mode="fresh")
    # Simulate a couple of fresh runs having minted their own sessions.
    from backend import sessions as sess
    s1 = sess.create_session(name="[定时] ft · 05-28 09:00", model="")
    s2 = sess.create_session(name="[定时] ft · 05-29 09:00", model="")
    t["session_id"] = s2["id"]  # latest run
    sched._save_state()
    assert sched.delete_task(t["id"]) is True
    # Both fresh-mode sessions still on disk.
    surviving = {s["id"] for s in sess.list_sessions()}
    assert s1["id"] in surviving
    assert s2["id"] in surviving


def test_delete_task_legacy_no_mode_removes_session(app_module):
    """Legacy task with no session_mode field acts like reuse — bound
    session DOES get deleted. Guards against the migration silently
    flipping these to fresh and leaving orphans."""
    sched = _sched_mod(app_module)
    from backend import sessions as sess
    s = sess.create_session(name="[定时] legacy", model="")
    sched._state["tasks"]["legacy"] = {
        "id": "legacy",
        "name": "legacy",
        "prompt": "p",
        "session_id": s["id"],
        "schedule": _daily_at(),
        "enabled": True,
        # NB: no session_mode field — exercises the fallback path.
    }
    assert sched.delete_task("legacy") is True
    assert all(x["id"] != s["id"] for x in sess.list_sessions())


# ---- list_task_history ----

def test_list_task_history_filters_by_tid(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "A", "ts": 100, "ok": True, "session_id": "s1"},
        {"task_id": "B", "ts": 110, "ok": True, "session_id": "s2"},
        {"task_id": "A", "ts": 120, "ok": False, "session_id": "s3",
         "error": "boom"},
        {"task_id": "C", "ts": 130, "ok": True, "session_id": "s4"},
    ]
    out = sched.list_task_history("A")
    assert len(out) == 2
    # Newest first.
    assert out[0]["ts"] == 120
    assert out[1]["ts"] == 100


def test_list_task_history_respects_limit(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "X", "ts": i, "ok": True, "session_id": f"s{i}"}
        for i in range(50)
    ]
    out = sched.list_task_history("X", limit=5)
    assert len(out) == 5
    # Newest 5 by ts.
    assert [e["ts"] for e in out] == [49, 48, 47, 46, 45]


def test_list_task_history_empty_for_unknown_task(app_module):
    sched = _sched_mod(app_module)
    sched._state["history"] = [
        {"task_id": "A", "ts": 100, "ok": True, "session_id": "s1"},
    ]
    assert sched.list_task_history("NEVER-EXISTED") == []


# ---- update_task: mode transitions ----

def test_update_task_fresh_to_reuse_seeds_session(app_module):
    """Switching fresh → reuse on a task with no prior runs should mint
    a bound session so the next run has somewhere to land."""
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at(), session_mode="fresh")
    assert t["session_id"] == ""
    updated = sched.update_task(t["id"], session_mode="reuse")
    assert updated["session_mode"] == "reuse"
    assert updated["session_id"]   # was seeded
    from backend import sessions as sess
    assert any(s["id"] == updated["session_id"] for s in sess.list_sessions())


def test_update_task_reuse_to_fresh_keeps_session(app_module):
    """reuse → fresh: the old bound session stays as the 'most recent
    run' pointer; not deleted (it has the user's prior conversation)."""
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at(), session_mode="reuse")
    old_sid = t["session_id"]
    updated = sched.update_task(t["id"], session_mode="fresh")
    assert updated["session_mode"] == "fresh"
    # session_id retained as the "latest run" anchor.
    assert updated["session_id"] == old_sid
    from backend import sessions as sess
    assert any(s["id"] == old_sid for s in sess.list_sessions())


def test_update_task_rejects_invalid_mode(app_module):
    sched = _sched_mod(app_module)
    t = sched.create_task("u", "p", _daily_at())
    import pytest
    with pytest.raises(ValueError):
        sched.update_task(t["id"], session_mode="bogus")


# ---- API surface ----

def test_api_create_task_with_session_mode(client, auth, app_module):
    """End-to-end: POST /api/scheduler/tasks honors session_mode."""
    _sched_mod(app_module)  # reset module state
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-fresh",
        "prompt": "do a thing",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "model": "",
        "session_mode": "fresh",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_mode"] == "fresh"
    assert body["session_id"] == ""


def test_api_create_task_rejects_bogus_mode(client, auth, app_module):
    """Pydantic pattern validator returns 422 for non-{fresh,reuse}."""
    _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-bad",
        "prompt": "do a thing",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "session_mode": "weird",
    })
    assert r.status_code == 422


def test_api_create_task_default_is_fresh(client, auth, app_module):
    """Omitting session_mode → server applies the Pydantic default
    'fresh'. Mirrors the in-memory create_task default."""
    _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "api-default",
        "prompt": "p",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
    })
    assert r.status_code == 200, r.text
    assert r.json()["session_mode"] == "fresh"


def test_api_task_history_endpoint(client, auth, app_module):
    sched = _sched_mod(app_module)
    r = client.post("/api/scheduler/tasks", headers=auth, json={
        "name": "withhist",
        "prompt": "p",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0,
                     "tz_offset_minutes": 480},
        "session_mode": "fresh",
    })
    tid = r.json()["id"]
    # Inject synthetic history directly (bypassing _execute_task).
    sched._state["history"] = [
        {"task_id": tid, "ts": 100, "ok": True, "session_id": "s1",
         "reply_preview": "first"},
        {"task_id": tid, "ts": 200, "ok": True, "session_id": "s2",
         "reply_preview": "second"},
        {"task_id": "OTHER", "ts": 150, "ok": True, "session_id": "x",
         "reply_preview": "unrelated"},
    ]
    r = client.get(f"/api/scheduler/tasks/{tid}/history", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["history"]) == 2
    # Newest first; "OTHER" task excluded.
    assert body["history"][0]["reply_preview"] == "second"
    assert body["history"][1]["reply_preview"] == "first"


def test_api_task_history_404_for_unknown_task(client, auth, app_module):
    _sched_mod(app_module)
    r = client.get("/api/scheduler/tasks/never-existed/history",
                   headers=auth)
    assert r.status_code == 404


# ---- _compute_next_run: schedule math ----------------------------------
# Every case injects a fixed `ref_ts` (the function's test seam) so the
# assertions are deterministic regardless of when the suite runs. Exact
# epoch checks for the deterministic kinds (daily / once); property checks
# (weekday-in-set, hour==slot, soonest within window) for weekly/monthly so
# we don't reimplement the SUT's calendar walk in the assertion.

from datetime import datetime, timedelta, timezone  # noqa: E402

# UTC+8, the same offset the rest of this file passes as tz_offset_minutes=480.
_BJ = timezone(timedelta(minutes=480))


def _bj(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_BJ)


@pytest.mark.parametrize("ref, expected", [
    # well before today's 09:00 → fires today 09:00
    (_bj(2026, 3, 10, 8, 0), _bj(2026, 3, 10, 9, 0)),
    # exactly 09:00 → the strictly-after rule pushes to tomorrow
    (_bj(2026, 3, 10, 9, 0), _bj(2026, 3, 11, 9, 0)),
    # past today's slot → tomorrow 09:00
    (_bj(2026, 3, 10, 9, 30), _bj(2026, 3, 11, 9, 0)),
])
def test_compute_next_run_daily_single(app_module, ref, expected):
    sched = _sched_mod(app_module)
    sch = {"kind": "daily", "hour": 9, "minute": 0, "tz_offset_minutes": 480}
    got = sched._compute_next_run(sch, ref_ts=ref.timestamp())
    assert got == pytest.approx(expected.timestamp())


@pytest.mark.parametrize("ref, expected", [
    (_bj(2026, 3, 10, 7, 0), _bj(2026, 3, 10, 8, 0)),    # before first slot
    (_bj(2026, 3, 10, 15, 0), _bj(2026, 3, 10, 22, 0)),  # mid-day → later slot today
    (_bj(2026, 3, 10, 23, 0), _bj(2026, 3, 11, 8, 0)),   # after last → tomorrow's first
])
def test_compute_next_run_daily_multi_time(app_module, ref, expected):
    """schedule['times'] non-empty → fire at EACH slot per day; the next fire
    can still be later TODAY even if the first slot already passed."""
    sched = _sched_mod(app_module)
    sch = {"kind": "daily", "hour": 0, "minute": 0, "tz_offset_minutes": 480,
           "times": [{"hour": 8, "minute": 0}, {"hour": 14, "minute": 0},
                     {"hour": 22, "minute": 0}]}
    got = sched._compute_next_run(sch, ref_ts=ref.timestamp())
    assert got == pytest.approx(expected.timestamp())


def test_compute_next_run_weekly_picks_soonest_listed_day(app_module):
    sched = _sched_mod(app_module)
    sch = {"kind": "weekly", "weekdays": [0, 3], "hour": 9, "minute": 0,
           "tz_offset_minutes": 480}   # Mon & Thu at 09:00
    ref = _bj(2026, 3, 10, 12, 0)
    res = datetime.fromtimestamp(
        sched._compute_next_run(sch, ref_ts=ref.timestamp()), tz=_BJ)
    assert res.weekday() in (0, 3)
    assert (res.hour, res.minute) == (9, 0)
    assert res > ref
    assert (res - ref) <= timedelta(days=7)


def test_compute_next_run_weekly_empty_returns_none(app_module):
    sched = _sched_mod(app_module)
    sch = {"kind": "weekly", "weekdays": [], "hour": 9, "minute": 0,
           "tz_offset_minutes": 480}
    assert sched._compute_next_run(
        sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp()) is None


def test_compute_next_run_monthly_same_month(app_module):
    sched = _sched_mod(app_module)
    sch = {"kind": "monthly", "day": 15, "hour": 9, "minute": 0,
           "tz_offset_minutes": 480}
    got = datetime.fromtimestamp(
        sched._compute_next_run(sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp()),
        tz=_BJ)
    assert (got.year, got.month, got.day, got.hour) == (2026, 3, 15, 9)


def test_compute_next_run_monthly_rolls_to_next_month(app_module):
    sched = _sched_mod(app_module)
    sch = {"kind": "monthly", "day": 5, "hour": 9, "minute": 0,
           "tz_offset_minutes": 480}
    # Day 5 already passed in March → next valid fire is April 5.
    got = datetime.fromtimestamp(
        sched._compute_next_run(sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp()),
        tz=_BJ)
    assert (got.month, got.day) == (4, 5)


@pytest.mark.parametrize("ref_year, exp_day", [
    (2027, 28),   # non-leap → Feb caps at 28
    (2028, 29),   # leap     → Feb 29 exists
])
def test_compute_next_run_monthly_day31_feb_fallback(app_module, ref_year, exp_day):
    """day=31 in February falls back to that month's last valid day."""
    sched = _sched_mod(app_module)
    sch = {"kind": "monthly", "day": 31, "hour": 9, "minute": 0,
           "tz_offset_minutes": 480}
    got = datetime.fromtimestamp(
        sched._compute_next_run(
            sch, ref_ts=_bj(ref_year, 2, 1, 8, 0).timestamp()),
        tz=_BJ)
    assert (got.month, got.day) == (2, exp_day)


def test_compute_next_run_once_future(app_module):
    sched = _sched_mod(app_module)
    sch = {"kind": "once", "year": 2026, "month": 12, "day": 25,
           "hour": 9, "minute": 0, "tz_offset_minutes": 480}
    got = sched._compute_next_run(
        sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp())
    assert got == pytest.approx(_bj(2026, 12, 25, 9, 0).timestamp())


def test_compute_next_run_once_past_returns_none(app_module):
    """A `once` whose datetime is already behind ref returns None so the
    loop stops retrying it."""
    sched = _sched_mod(app_module)
    sch = {"kind": "once", "year": 2020, "month": 1, "day": 1,
           "hour": 9, "minute": 0, "tz_offset_minutes": 480}
    assert sched._compute_next_run(
        sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp()) is None


@pytest.mark.parametrize("h, m", [(24, 0), (-1, 0), (0, 60), (9, -1)])
def test_compute_next_run_invalid_time_returns_none(app_module, h, m):
    sched = _sched_mod(app_module)
    sch = {"kind": "daily", "hour": h, "minute": m, "tz_offset_minutes": 480}
    assert sched._compute_next_run(
        sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp()) is None


def test_compute_next_run_unknown_kind_returns_none(app_module):
    sched = _sched_mod(app_module)
    assert sched._compute_next_run(
        {"kind": "hourly", "hour": 9, "minute": 0,
         "tz_offset_minutes": 480}) is None


def test_compute_next_run_dst_keeps_wall_clock_iana(app_module):
    """IANA tz: a 09:00 daily fires at 09:00 LOCAL on both sides of the US
    spring-forward, instead of drifting by the DST hour."""
    from zoneinfo import ZoneInfo
    sched = _sched_mod(app_module)
    ny = ZoneInfo("America/New_York")
    sch = {"kind": "daily", "hour": 9, "minute": 0, "tz": "America/New_York"}
    # US DST 2026 begins Sun 2026-03-08. Ref the day before (EST, UTC-5).
    ref = datetime(2026, 3, 7, 8, 0, tzinfo=ny)
    got = datetime.fromtimestamp(
        sched._compute_next_run(sch, ref_ts=ref.timestamp()), tz=ny)
    assert (got.month, got.day, got.hour, got.minute) == (3, 7, 9, 0)
    # Ref after spring-forward (EDT, UTC-4): still 09:00 wall-clock.
    ref2 = datetime(2026, 3, 9, 8, 0, tzinfo=ny)
    got2 = datetime.fromtimestamp(
        sched._compute_next_run(sch, ref_ts=ref2.timestamp()), tz=ny)
    assert (got2.month, got2.day, got2.hour, got2.minute) == (3, 9, 9, 0)


def test_compute_next_run_unknown_iana_falls_back_to_offset(app_module):
    """A garbage IANA name falls through to tz_offset_minutes (legacy), not
    a crash."""
    sched = _sched_mod(app_module)
    sch = {"kind": "daily", "hour": 9, "minute": 0,
           "tz": "Mars/Olympus", "tz_offset_minutes": 480}
    got = sched._compute_next_run(
        sch, ref_ts=_bj(2026, 3, 10, 8, 0).timestamp())
    assert got == pytest.approx(_bj(2026, 3, 10, 9, 0).timestamp())
