"""Scheduler ownership and shutdown regressions, with a real SessionStream."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TaskNotificationMessage,
    TaskStartedMessage, TextBlock,
)
from tests.test_scheduler import _sched_mod, _daily_at


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def terminal(sid, **overrides):
    data = dict(subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id=sid, usage={})
    data.update(overrides)
    return ResultMessage(**data)


class StreamClient:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.queries = []
        self.disconnected = 0

    async def receive_messages(self):
        while True:
            yield await self.queue.get()

    async def query(self, prompt):
        self.queries.append(prompt)

    async def disconnect(self):
        self.disconnected += 1


def install_runtime(monkeypatch, sessions, chat):
    sid = sessions.create_session(name="scheduled fixture", model="claude-sonnet-4-6")["id"]
    client = StreamClient()

    async def get_client(**kwargs):
        return client

    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat, "_session_message_uuids", lambda *_: set())
    key = (sid, "claude-sonnet-4-6", "", "")
    chat._clients[key] = client
    stream = chat._ensure_session_stream(key, client)
    return sid, client, stream


def start_background(client, sid):
    client.queue.put_nowait(TaskStartedMessage(
        subtype="task_started", data={}, task_id="scheduled-child",
        description="synthetic child", uuid="start-1", session_id=sid,
        tool_use_id="tool-1"))
    client.queue.put_nowait(AssistantMessage(
        content=[TextBlock(text="started background work")],
        model="claude-sonnet-4-6", uuid="first-answer"))
    client.queue.put_nowait(terminal(sid))


def finish_background(client, sid, reason=""):
    client.queue.put_nowait(TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="scheduled-child",
        status="completed", output_file="", summary="synthetic result",
        uuid="settle-1", session_id=sid, tool_use_id="tool-1"))
    client.queue.put_nowait(AssistantMessage(
        content=[TextBlock(text="final original scheduled answer")],
        model="claude-sonnet-4-6", uuid="final-answer"))
    client.queue.put_nowait(terminal(sid, terminal_reason=reason))


@pytest.mark.asyncio
async def test_scheduler_handoff_waits_for_original_answer_before_reuse(app_module, monkeypatch):
    sched = _sched_mod(app_module)
    from backend import chat, sessions
    sid, client, stream = install_runtime(monkeypatch, sessions, chat)
    drain_locks = []

    async def drain(_sid):
        # Acquiring the runtime mutex would deadlock if called directly by a
        # watcher that the scheduler still awaits while holding that mutex.
        async with chat._session_runtime_lock_for(sid):
            drain_locks.append(False)

    monkeypatch.setattr(chat, "_maybe_drain_queue", drain)
    first = second = None
    try:
        first = asyncio.create_task(sched._run_sdk_task_turn(sid, "claude-sonnet-4-6", "first"))
        await until(lambda: len(client.queries) == 1)
        start_background(client, sid)
        await until(lambda: chat._session_has_live_watcher(sid))
        assert sid in chat._sessions_with_inflight_tasks
        assert not first.done()
        second = asyncio.create_task(sched._run_sdk_task_turn(sid, "claude-sonnet-4-6", "second"))
        await asyncio.sleep(0.02)
        assert len(client.queries) == 1
        finish_background(client, sid)
        result = await asyncio.wait_for(first, 3)
        assert result == ("final original scheduled answer", None)
        assert result.status == "completed"
        await until(lambda: len(client.queries) == 2)
        client.queue.put_nowait(AssistantMessage(
            content=[TextBlock(text="second answer")], model="claude-sonnet-4-6", uuid="second-answer"))
        client.queue.put_nowait(terminal(sid))
        assert await asyncio.wait_for(second, 3) == ("second answer", None)
        await until(lambda: bool(drain_locks))
        assert not any(drain_locks)
        assert not chat._session_has_live_watcher(sid)
        assert sid not in chat._sessions_with_inflight_tasks
    finally:
        for task in (first, second):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["cancel", "timeout"])
async def test_scheduler_cancel_joins_background_owner(app_module, monkeypatch, cancel_mode):
    sched = _sched_mod(app_module)
    from backend import chat, sessions
    sid, client, stream = install_runtime(monkeypatch, sessions, chat)
    run = asyncio.create_task(sched._run_sdk_task_turn(sid, "claude-sonnet-4-6", "first"))
    try:
        await until(lambda: len(client.queries) == 1)
        start_background(client, sid)
        await until(lambda: chat._session_has_live_watcher(sid))
        owner = chat._task_watchers[sid]
        if cancel_mode == "timeout":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(run, 0.01)
        else:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        assert owner.done()
        assert client.disconnected == 1
        assert sid not in chat._sessions_with_inflight_tasks
        assert not chat._session_runtime_lock_for(sid).locked()
        assert not chat._session_has_live_watcher(sid)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "once"])
async def test_stop_during_due_commit_restores_unlaunched_trigger(app_module, monkeypatch, kind):
    sched = _sched_mod(app_module)
    entered, release = threading.Event(), threading.Event()
    spawned = []
    real_save = sched._save_state
    now = time.time()
    schedule = _daily_at() if kind == "daily" else {"kind": "once", "at": now - 30}
    task = {"id": "due-fixture", "name": "synthetic", "prompt": "synthetic",
            "schedule": schedule, "next_run": now - 30, "enabled": True,
            "session_mode": "fresh", "session_id": ""}
    sched._state["tasks"][task["id"]] = task
    sched._save_state()
    first_save = True

    def save():
        nonlocal first_save
        if first_save:
            first_save = False
            entered.set()
            assert release.wait(3)
        real_save()

    async def execute(current):
        spawned.append(current["id"])

    monkeypatch.setattr(sched, "_save_state", save)
    monkeypatch.setattr(sched, "_execute_task", execute)
    sched._scheduler_task = asyncio.create_task(sched._scheduler_loop())
    try:
        await until(entered.is_set)
        stopper = asyncio.create_task(sched.stop_scheduler())
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.wait_for(stopper, 3)
        assert not spawned
        assert not sched._RUN_TASKS
        restored = json.loads(sched._STATE_FILE.read_text())["tasks"][task["id"]]
        assert restored["next_run"] == now - 30
        assert restored["enabled"] is True
        assert not await sched.run_task_now(task["id"])
        # Actual startup loads the restored disk state and runs once, even for
        # a one-shot trigger whose first advancement had disabled it.
        await sched.start_scheduler()
        await until(lambda: len(spawned) == 1)
        await sched.stop_scheduler()
        assert spawned == [task["id"]]
    finally:
        release.set()
        await sched.stop_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,is_error,status", [
    ("", False, "completed"), ("", True, "failed"),
    ("aborted_streaming", False, "cancelled"),
    ("aborted_tools", False, "cancelled"), ("max_turns", False, "stopped"),
])
async def test_scheduler_and_delivery_share_terminal_status(app_module, monkeypatch, tmp_path,
                                                           reason, is_error, status):
    sched = _sched_mod(app_module)
    from backend import chat, task_delivery
    from backend.activity import activity
    task = sched.create_task("terminal", "synthetic", _daily_at(), session_mode="reuse")
    sid = task["session_id"]

    class Client:
        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text="synthetic partial")],
                                   model="claude-sonnet-4-6", uuid="partial")
            yield terminal(sid, terminal_reason=reason, is_error=is_error)

    async def get_client(**kwargs):
        return Client()

    finishes = []
    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat, "_session_message_uuids", lambda *_: set())
    monkeypatch.setattr(activity, "finish", lambda _sid, value, **kw: finishes.append(value))
    await sched._execute_task(task)
    row = sched._state["history"][-1]
    assert row["status"] == status
    assert row["ok"] is (status == "completed")
    assert row["terminal_reason"] == reason
    assert finishes == [status]
    task_delivery.save(sid, {"schema": 1, "turns": [{"id": "turn", "status": "running",
                        "tools": [], "workspace": str(tmp_path)}]})
    task_delivery.observe_message(sid, "turn", terminal(sid, terminal_reason=reason, is_error=is_error))
    assert task_delivery.load(sid)["turns"][0]["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status", [("", "completed"), ("aborted_tools", "cancelled"),
                                          ("max_turns", "stopped")])
async def test_scheduled_history_waits_for_background_terminal(app_module, monkeypatch, reason, status):
    sched = _sched_mod(app_module)
    from backend import chat, sessions
    sid, client, stream = install_runtime(monkeypatch, sessions, chat)
    task = {"id": "history-background", "name": "fixture", "prompt": "synthetic",
            "session_mode": "reuse", "session_id": sid, "model": "claude-sonnet-4-6"}
    sched._state["tasks"][task["id"]] = task
    run = asyncio.create_task(sched._execute_task(task))
    try:
        await until(lambda: len(client.queries) == 1)
        start_background(client, sid)
        await until(lambda: chat._session_has_live_watcher(sid))
        assert not sched._state["history"]
        finish_background(client, sid, reason)
        await asyncio.wait_for(run, 3)
        history = sched._state["history"][-1]
        assert sid not in chat._background_origin_turn_id
        assert sid not in chat._background_turn_started_at
        assert history["session_id"] == sid
        assert history["task_id"] == task["id"]
        assert history["status"] == status
        assert history["terminal_reason"] == reason
        assert history["ok"] is (status == "completed")
        if status == "completed":
            assert history["reply_preview"] == "final original scheduled answer"
        else:
            assert history["error"]
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_scheduler_failed_parent_stops_its_new_background_task(app_module, monkeypatch):
    sched = _sched_mod(app_module)
    from backend import chat, sessions
    sid, client, stream = install_runtime(monkeypatch, sessions, chat)
    run = asyncio.create_task(sched._run_sdk_task_turn(sid, "claude-sonnet-4-6", "synthetic"))
    try:
        await until(lambda: len(client.queries) == 1)
        client.queue.put_nowait(TaskStartedMessage(
            subtype="task_started", data={}, task_id="scheduled-child",
            description="synthetic child", uuid="start-1", session_id=sid,
            tool_use_id="tool-1"))
        client.queue.put_nowait(terminal(sid, terminal_reason="aborted_streaming"))
        result = await asyncio.wait_for(run, 3)
        assert result.status == "cancelled"
        assert result[1]
        assert client.disconnected == 1
        assert sid not in chat._sessions_with_inflight_tasks
        assert not chat._session_has_live_watcher(sid)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_run_now_read_cannot_launch_after_shutdown(app_module, monkeypatch):
    sched = _sched_mod(app_module)
    task = sched.create_task("late-click", "synthetic", _daily_at())
    entered, release = asyncio.Event(), asyncio.Event()
    original_io = sched.obs.to_thread_io
    launched = []

    async def delayed_read(operation, *args, **kwargs):
        result = await original_io(operation, *args, **kwargs)
        if operation == "scheduler.task_read":
            entered.set()
            await release.wait()
        return result

    async def execute(_task):
        launched.append(True)

    monkeypatch.setattr(sched.obs, "to_thread_io", delayed_read)
    monkeypatch.setattr(sched, "_execute_task", execute)
    click = asyncio.create_task(sched.run_task_now(task["id"]))
    await entered.wait()
    await sched.stop_scheduler()
    release.set()
    assert await click is False
    assert not launched
    assert not sched._RUN_TASKS


@pytest.mark.asyncio
async def test_shutdown_rollback_failure_does_not_restart_tick(app_module, monkeypatch):
    sched = _sched_mod(app_module)
    task = sched.create_task("io-failure", "synthetic", _daily_at())
    sched._state["tasks"][task["id"]]["next_run"] = time.time() - 10
    sched._save_state()
    entered, release = threading.Event(), threading.Event()
    write = sched.atomic_write_text
    writes = 0

    def interrupted_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            assert release.wait(3)
            return write(*args, **kwargs)
        raise OSError("synthetic rollback disk failure")

    monkeypatch.setattr(sched, "atomic_write_text", interrupted_write)
    tick = asyncio.create_task(sched._scheduler_loop())
    sched._scheduler_task = tick
    await until(entered.is_set)
    stop = asyncio.create_task(sched.stop_scheduler())
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.wait_for(stop, 3)
    assert tick.done()
    assert not sched._RUN_TASKS
    assert not sched.persistence_status()["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "once"])
@pytest.mark.parametrize("edit", ["none", "unrelated", "schedule", "delete"])
async def test_cancel_startup_cleanup_preserves_unlaunched_catchup_and_new_edits(
    app_module, monkeypatch, kind, edit,
):
    sched = _sched_mod(app_module)
    now = time.time()
    past = datetime.fromtimestamp(now - 30, timezone.utc)
    schedule = _daily_at() if kind == "daily" else {
        "kind": "once", "year": past.year, "month": past.month, "day": past.day,
        "hour": past.hour, "minute": past.minute, "tz_offset_minutes": 0,
    }
    task = {"id": "startup-catchup", "name": "synthetic", "prompt": "synthetic",
            "schedule": schedule, "next_run": now - 30, "enabled": True,
            "session_mode": "fresh", "session_id": ""}
    sched._state["tasks"][task["id"]] = task
    sched._save_state()
    entered, release = asyncio.Event(), asyncio.Event()
    spawned = []

    async def cleanup():
        entered.set()
        await release.wait()

    async def execute(current):
        spawned.append(current["id"])

    monkeypatch.setattr(sched, "_resume_pending_task_cleanups", cleanup)
    monkeypatch.setattr(sched, "_execute_task", execute)
    startup = asyncio.create_task(sched.start_scheduler())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        extra = None
        if edit == "unrelated":
            extra = sched.create_task("new task", "synthetic", _daily_at())
            sched._state["unread_count"] = 7
            sched._save_state()
        elif edit == "schedule":
            future = datetime.fromtimestamp(now + 3600, timezone.utc)
            updated = sched.update_task(task["id"], schedule={
                "kind": "once", "year": future.year, "month": future.month,
                "day": future.day, "hour": future.hour, "minute": future.minute,
                "tz_offset_minutes": 0,
            })
        elif edit == "delete":
            assert sched.delete_task(task["id"], purge_bound_session=False)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert sched._scheduler_task is None
        assert not spawned
        persisted = json.loads(sched._STATE_FILE.read_text())
        if edit == "delete":
            assert task["id"] not in persisted["tasks"]
            assert task["id"] in persisted["cleanup_pending"]
        elif edit == "schedule":
            assert persisted["tasks"][task["id"]]["schedule"] == updated["schedule"]
            assert persisted["tasks"][task["id"]]["next_run"] == updated["next_run"]
        else:
            restored = persisted["tasks"][task["id"]]
            assert restored["next_run"] == now - 30
            assert restored["enabled"] is True
            if extra:
                assert extra["id"] in persisted["tasks"]
                assert persisted["unread_count"] == 7
            release.set()
            await sched.start_scheduler()
            await until(lambda: bool(spawned))
            await sched.stop_scheduler()
            assert spawned == [task["id"]]
    finally:
        release.set()
        if not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        await sched.stop_scheduler()
