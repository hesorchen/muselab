"""Edits during an admitted scheduler run preserve its durable run metadata."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from tests.test_scheduler import _daily_at, _sched_mod


@pytest.fixture
def scheduled_fixture(app_module, monkeypatch):
    from backend import api_scheduler, chat, presence, sessions

    sched = _sched_mod(app_module)
    clock = SimpleNamespace(now=datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())
    clock.time = lambda: clock.now
    monkeypatch.setattr(sched, "time", clock)
    monkeypatch.setattr(presence, "recently_active", lambda: True)
    monkeypatch.setattr(presence, "last_seen_age", lambda: 0.0)

    async def disconnect(_sid):
        return None  # No SDK is ever constructed by these synthetic turns.

    monkeypatch.setattr(chat, "disconnect_client", disconnect)
    return SimpleNamespace(sched=sched, api=api_scheduler, sessions=sessions, clock=clock)


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def _disk_task(fixture, tid):
    state = json.loads(fixture.sched._STATE_FILE.read_text(encoding="utf-8"))
    return state, state["tasks"].get(tid)


def _start_due(fixture, tid):
    sched = fixture.sched
    with sched._STATE_LOCK:
        sched._state["tasks"][tid]["next_run"] = fixture.clock.now - 30
        sched._save_state()
    sched._scheduler_task = asyncio.create_task(sched._scheduler_loop())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fresh", "reuse"])
async def test_due_run_completion_updates_current_row_after_edit(scheduled_fixture, monkeypatch, mode):
    fixture = scheduled_fixture
    sched = fixture.sched
    started, release = asyncio.Event(), asyncio.Event()
    observed = []
    owner = None

    async def run_turn(sid, model, prompt, **_kwargs):
        nonlocal owner
        owner = asyncio.current_task()
        observed.append((sid, model, prompt))
        started.set()
        await release.wait()
        return "synthetic completed", None

    monkeypatch.setattr(sched, "_run_sdk_task_turn", run_turn)
    task = sched.create_task("synthetic original", "original prompt", _daily_at(),
                             model="synthetic-first-model", session_mode=mode)
    _start_due(fixture, task["id"])
    try:
        await asyncio.wait_for(started.wait(), 3)
        # This is the real PATCH handler and disk transaction on its normal
        # worker thread, while a due execution holds its original task snapshot.
        edited = await asyncio.to_thread(
            fixture.api.patch_task_endpoint, task["id"],
            fixture.api.TaskPatch(name="synthetic edited", prompt="next prompt",
                                  model="synthetic-next-model", enabled=False,
                                  schedule=fixture.api.ScheduleIn(**_daily_at(14))),
        )
        fixture.clock.now += 60
        release.set()
        await asyncio.wait_for(owner, 3)
        state, durable = _disk_task(fixture, task["id"])
        assert durable["last_run"] == fixture.clock.now
        assert sched.get_task(task["id"]) == durable
        assert durable["session_id"] == observed[0][0]
        for key in ("name", "prompt", "model", "schedule", "enabled", "next_run"):
            assert durable[key] == edited[key]
        assert observed == [(durable["session_id"], "synthetic-first-model", "original prompt")]
        assert len(state["history"]) == state["unread_count"] == 1
        assert state["history"][0]["ts"] == durable["last_run"]
        assert state["history"][0]["task_name"] == "synthetic original"
    finally:
        release.set()
        await sched.stop_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_mode", ["fresh", "reuse"])
async def test_fresh_session_commit_after_edit_preserves_current_binding(scheduled_fixture, monkeypatch, next_mode):
    fixture = scheduled_fixture
    sched = fixture.sched
    entered, release = threading.Event(), threading.Event()
    minted, observed = [], []
    real_create = fixture.sessions.create_session

    def create_session(**kwargs):
        result = real_create(**kwargs)
        minted.append(result["id"])
        if len(minted) == 1:
            # Real metadata is on disk; no scheduler pointer has committed yet.
            entered.set()
            assert release.wait(3)
        return result

    async def run_turn(sid, _model, prompt, **_kwargs):
        observed.append((sid, prompt))
        return "synthetic completed", None

    monkeypatch.setattr(fixture.sessions, "create_session", create_session)
    monkeypatch.setattr(sched, "_run_sdk_task_turn", run_turn)
    task = sched.create_task("synthetic original", "original prompt", _daily_at(), session_mode="fresh")
    _start_due(fixture, task["id"])
    try:
        await _until(entered.is_set)
        edited = await asyncio.to_thread(
            fixture.api.patch_task_endpoint, task["id"],
            fixture.api.TaskPatch(name="synthetic edited", prompt="next prompt", session_mode=next_mode),
        )
        release.set()
        await _until(lambda: not sched._RUN_TASKS)
        state, durable = _disk_task(fixture, task["id"])
        expected_sid = minted[0] if next_mode == "fresh" else edited["session_id"]
        assert durable["session_id"] == expected_sid
        assert durable["last_run"] == fixture.clock.now
        assert durable["name"] == "synthetic edited" and durable["prompt"] == "next prompt"
        assert durable["session_mode"] == next_mode
        assert state["history"][0]["session_id"] == minted[0]
        assert observed == [(minted[0], "original prompt")]
        assert all(fixture.sessions.get_session(sid) for sid in minted)
        # A new reuse binding selected during the old fresh run must remain
        # the next run's session, rather than being replaced by the old result.
        if next_mode == "reuse":
            assert expected_sid == minted[1] and expected_sid != minted[0]
            assert await sched.run_task_now(task["id"])
            await _until(lambda: not sched._RUN_TASKS)
            assert observed[-1] == (expected_sid, "next prompt")
    finally:
        release.set()
        await sched.stop_scheduler()


@pytest.mark.asyncio
async def test_deleted_inflight_task_cannot_publish_run_metadata(scheduled_fixture, monkeypatch):
    fixture = scheduled_fixture
    sched = fixture.sched
    started = asyncio.Event()
    owner = None

    async def run_turn(_sid, _model, _prompt, **_kwargs):
        nonlocal owner
        owner = asyncio.current_task()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sched, "_run_sdk_task_turn", run_turn)
    task = sched.create_task("synthetic delete", "synthetic prompt", _daily_at(), session_mode="fresh")
    _start_due(fixture, task["id"])
    try:
        await asyncio.wait_for(started.wait(), 3)
        deleted = await fixture.api.delete_task_endpoint(task["id"])
        assert deleted == {"deleted": task["id"]}
        assert owner.cancelled()
        state, durable = _disk_task(fixture, task["id"])
        assert durable is None and sched.get_task(task["id"]) is None
        assert state["history"] == [] and state["unread_count"] == 0
        assert state["cleanup_pending"] == {}
        assert not sched._RUN_TASKS
    finally:
        await sched.stop_scheduler()
