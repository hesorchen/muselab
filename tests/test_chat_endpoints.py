"""Endpoint tests for chat control routes: reset / interrupt / probe.

These hit the FastAPI routes through TestClient with the pool pre-seeded
with fake clients, so the route logic (4-tuple key handling, disconnect
fan-out, response shape) runs for real without spawning a CLI.
"""
import asyncio
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

from tests.conftest import TEST_TOKEN


class _FakeSDKClient:
    def __init__(self):
        self.disconnected = False
        self.interrupted = False
        self._raise_on_interrupt = False

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        if self._raise_on_interrupt:
            raise RuntimeError("interrupt boom")
        self.interrupted = True

    async def set_model(self, _model):
        raise AssertionError("model changes must rebuild, not call set_model")


@pytest.fixture()
def chat_mod(app_module):
    from backend import chat as chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._session_runtime_locks.clear()
    chat_mod._pending_runtime_rebuilds.clear()
    chat_mod._pending_interrupts.clear()
    chat_mod._active_turns.clear()
    yield chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._session_runtime_locks.clear()
    chat_mod._pending_runtime_rebuilds.clear()
    chat_mod._pending_interrupts.clear()
    chat_mod._active_turns.clear()


def _seed(chat_mod, key, client=None):
    client = client or _FakeSDKClient()
    chat_mod._clients[key] = client
    chat_mod._client_permission[key] = "bypassPermissions"
    chat_mod._client_lru.append(key)
    return client


# ====== reset ======

def test_reset_single_session(chat_mod, client):
    """reset?session_id=X disconnects that session and returns [X]."""
    c = _seed(chat_mod, ("sid-A", "claude-sonnet-4-6", "auto", ""))
    r = client.post(f"/api/chat/reset?session_id=sid-A&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["reset"] == ["sid-A"]
    assert c.disconnected is True
    assert ("sid-A", "claude-sonnet-4-6", "auto", "") not in chat_mod._clients


def test_reset_all_with_multiple_runtime_keys(chat_mod, client):
    """L183 regression: reset() with NO session_id iterates every pooled
    client. The cache keys are 4-tuples (sid, model, effort, service tier); the response
    builder must index key[0]/key[1] (NOT unpack into 2 vars) or it raises
    'too many values to unpack'. Must return ['sid@model', ...]."""
    c1 = _seed(chat_mod, ("sidX", "claude-sonnet-4-6", "auto", ""))
    c2 = _seed(chat_mod, ("sidY", "claude-haiku-4-5", "high", ""))
    c3 = _seed(chat_mod, ("sidX", "deepseek-v4-pro", "auto", ""))

    r = client.post(f"/api/chat/reset?token={TEST_TOKEN}")
    assert r.status_code == 200, r.text   # would be 500 if unpack regressed
    body = r.json()
    assert body["ok"] is True
    assert set(body["reset"]) == {
        "sidX@claude-sonnet-4-6",
        "sidY@claude-haiku-4-5",
        "sidX@deepseek-v4-pro",
    }
    # Every client disconnected + pool fully cleared.
    assert all(c.disconnected for c in (c1, c2, c3))
    assert chat_mod._clients == {}
    assert chat_mod._client_lru == []
    assert chat_mod._client_permission == {}


def test_reset_all_empty_pool(chat_mod, client):
    """No live clients → reset returns an empty list, not an error."""
    r = client.post(f"/api/chat/reset?token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "reset": []}


def test_session_rename_updates_activity_ledger(chat_mod, client, monkeypatch):
    from backend import activity as activity_module

    created = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "Before rename"},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    calls = []
    perf_events = []

    def rename_activity(target_sid, name):
        calls.append((target_sid, name))

    monkeypatch.setattr(activity_module.activity, "rename_session", rename_activity)
    monkeypatch.setattr(chat_mod, "sdk_rename_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chat_mod, "_perf_event",
        lambda event, **fields: perf_events.append((event, fields)),
    )

    response = client.patch(
        f"/api/chat/sessions/{sid}",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "After rename"},
    )

    assert response.status_code == 200, response.text
    assert chat_mod.sess.get_session(sid)["name"] == "After rename"
    assert calls == [(sid, "After rename")]
    assert len(perf_events) == 1
    event, fields = perf_events[0]
    assert event == "session.rename"
    assert fields["session"] == sid[:8]
    assert fields["status"] == "ok"
    for key in (
        "lock_wait_ms", "local_index_ms", "sdk_rename_ms", "activity_ms",
        "total_ms",
    ):
        assert isinstance(fields[key], int)
        assert fields[key] >= 0
    assert "Before rename" not in repr(perf_events)
    assert "After rename" not in repr(perf_events)


def test_session_effort_and_fast_patch_persist_and_rebuild(
    chat_mod, client, monkeypatch,
):
    created = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "controls"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["effort"] == "auto"
    assert body["service_tier"] == ""
    sid = body["id"]
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")

    async def capability(_model):
        return {
            "supported_reasoning_levels": [
                "low", "medium", "high", "xhigh", "max", "ultra",
            ],
            "service_tiers": ["priority"],
        }

    rebuilt = []

    async def rebuild(_sid):
        rebuilt.append(_sid)

    monkeypatch.setattr(chat_mod, "_detect_gateway_context_capability", capability)
    monkeypatch.setattr(chat_mod, "_rebuild_session_runtime", rebuild)
    headers = {"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"}

    before = dict(chat_mod.sess.get_session(sid))
    invalid_combo = client.patch(
        f"/api/chat/sessions/{sid}", headers=headers,
        json={
            "model": "claude-sonnet-4-6",
            "effort": "ultra",
            "service_tier": "fast",
        },
    )
    assert invalid_combo.status_code == 400
    unchanged = chat_mod.sess.get_session(sid)
    assert unchanged["model"] == before["model"]
    assert unchanged["effort"] == before["effort"]
    assert unchanged["service_tier"] == before["service_tier"]
    assert rebuilt == []

    combined = client.patch(
        f"/api/chat/sessions/{sid}", headers=headers,
        json={
            "model": "codex:gpt-5.6-sol",
            "effort": "ultra",
            "service_tier": "fast",
        },
    )
    assert combined.status_code == 200, combined.text
    meta = chat_mod.sess.get_session(sid)
    assert meta["effort"] == "ultra"
    assert meta["service_tier"] == "fast"
    assert rebuilt == [sid]

    # Canonical `auto` accepts the old empty spelling and still rebuilds once.
    auto = client.patch(
        f"/api/chat/sessions/{sid}", headers=headers, json={"effort": ""})
    assert auto.status_code == 200, auto.text
    assert chat_mod.sess.get_session(sid)["effort"] == "auto"
    assert rebuilt == [sid, sid]

    invalid = client.patch(
        f"/api/chat/sessions/{sid}", headers=headers,
        json={"service_tier": "turbo"})
    assert invalid.status_code == 400


def test_same_provider_model_switch_rebuilds_client(chat_mod, client):
    sid = _make_compact_session(client)
    key = (sid, "claude-sonnet-4-6", "auto", "")
    fake = _seed(chat_mod, key)

    r = client.patch(
        f"/api/chat/sessions/{sid}",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"model": "claude-haiku-4-5"},
    )

    assert r.status_code == 200, r.text
    assert fake.disconnected is True
    assert key not in chat_mod._clients
    assert chat_mod.sess.get_session(sid)["model"] == "claude-haiku-4-5"


def test_permission_patch_enters_plan_and_rebuilds_client(chat_mod, client):
    sid = _make_compact_session(client)
    key = (sid, "claude-sonnet-4-6", "auto", "")
    fake = _seed(chat_mod, key)

    before = chat_mod.sess.get_session(sid)
    response = client.patch(
        f"/api/chat/sessions/{sid}",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"permission": "plan"},
    )

    assert response.status_code == 200, response.text
    assert fake.disconnected is True
    assert key not in chat_mod._clients
    current = chat_mod.sess.get_session(sid)
    assert current["permission"] == "plan"
    assert current["plan_return_permission"] == before["permission"]


def test_context_breakdown_uses_sdk_control_channel(chat_mod, client):
    """The /context UI must use the typed SDK control request on the pooled
    client; it must not open a second receive iterator on the chat stream."""
    sid = _make_compact_session(client)
    chat_mod.sess.update_model(sid, "claude-sonnet-4-6")

    class ContextClient(_FakeSDKClient):
        def __init__(self):
            super().__init__()
            self.context_calls = 0

        async def get_context_usage(self):
            self.context_calls += 1
            return {
                "categories": [
                    {"name": "Messages", "tokens": 1234, "color": "blue"},
                ],
                "totalTokens": 1234,
                "maxTokens": 200_000,
                "percentage": 0.6,
                "model": "claude-sonnet-4-6",
                "apiUsage": {"inputTokens": 1234, "outputTokens": 56},
            }

    sdk_client = ContextClient()
    _seed(
        chat_mod,
        (sid, "claude-sonnet-4-6", "auto", ""),
        client=sdk_client,
    )

    response = client.get(
        f"/api/chat/context-breakdown/{sid}",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 200, response.text
    assert response.json()["totalTokens"] == 1234
    assert response.json()["categories"][0]["name"] == "Messages"
    assert response.json()["apiUsage"]["outputTokens"] == 56
    assert sdk_client.context_calls == 1


@pytest.mark.asyncio
async def test_runtime_rebuild_defers_while_turn_is_reserved(chat_mod):
    sid = "sid-deferred-rebuild"
    key = (sid, "claude-sonnet-4-6", "auto", "")
    fake = _seed(chat_mod, key)
    active = chat_mod.TurnBroadcast(sid)
    chat_mod._active_turns[sid] = active

    await chat_mod._rebuild_session_runtime(sid)

    assert fake.disconnected is False
    assert sid in chat_mod._pending_runtime_rebuilds

    active.finish()
    chat_mod._active_turns.pop(sid, None)
    await chat_mod._rebuild_session_runtime(sid)

    assert fake.disconnected is True
    assert sid not in chat_mod._pending_runtime_rebuilds


@pytest.mark.asyncio
async def test_runtime_rebuild_defers_while_background_watcher_owns_client(
        chat_mod):
    sid = "sid-watcher-rebuild"
    key = (sid, "claude-sonnet-4-6", "auto", "")
    fake = _seed(chat_mod, key)
    watcher = asyncio.create_task(asyncio.sleep(60))
    chat_mod._task_watchers[sid] = watcher
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        await chat_mod._rebuild_session_runtime(sid)
        assert fake.disconnected is False
        assert sid in chat_mod._pending_runtime_rebuilds
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        chat_mod._task_watchers.pop(sid, None)
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)

    await chat_mod._rebuild_session_runtime(sid)
    assert fake.disconnected is True
    assert sid not in chat_mod._pending_runtime_rebuilds


def test_session_list_marks_detached_background_work_active(chat_mod, client):
    sid = _make_compact_session(client)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        response = client.get(
            "/api/chat/sessions", headers={"X-Auth-Token": TEST_TOKEN})
        assert response.status_code == 200, response.text
        row = next(s for s in response.json()["sessions"] if s["id"] == sid)
        assert row["active"] is True
        assert row["turn_active"] is False
        assert row["background_active"] is True
    finally:
        chat_mod._sessions_with_inflight_tasks.pop(sid, None)


def test_session_list_marks_native_cron_as_amber_idle_state(chat_mod, client):
    sid = _make_compact_session(client)
    chat_mod._sdk_cron_jobs[sid] = {
        "job-a": {"cron": "7 * * * *", "recurring": True},
        "job-b": {"cron": "12 * * * *", "recurring": True},
    }
    try:
        response = client.get(
            "/api/chat/sessions", headers={"X-Auth-Token": TEST_TOKEN})
        assert response.status_code == 200, response.text
        row = next(s for s in response.json()["sessions"] if s["id"] == sid)
        assert row["active"] is False
        assert row["turn_active"] is False
        assert row["background_active"] is False
        assert row["scheduled_active"] is True
        assert row["scheduled_count"] == 2
    finally:
        chat_mod._sdk_cron_jobs.pop(sid, None)


def test_native_cron_jobs_have_authenticated_read_only_inspector(
        chat_mod, client):
    sid = _make_compact_session(client)
    chat_mod._sdk_cron_jobs[sid] = {
        "job-a": {
            "cron": "7 * * * *",
            "recurring": True,
            "durable": False,
            "prompt": "inspect the workspace",
            "prompt_sha256": "a" * 64,
            "prompt_truncated": False,
        },
    }
    try:
        denied = client.get(f"/api/chat/sessions/{sid}/scheduled-tasks")
        response = client.get(
            f"/api/chat/sessions/{sid}/scheduled-tasks",
            headers={"X-Auth-Token": TEST_TOKEN},
        )
        assert denied.status_code == 401
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload == {
            "session_id": sid,
            "runtime_owned": True,
            "count": 1,
            "tasks": [{
                "job_id": "job-a",
                "cron": "7 * * * *",
                "recurring": True,
                "durable": False,
                "prompt": "inspect the workspace",
                "prompt_truncated": False,
            }],
        }
        assert "prompt_sha256" not in response.text
    finally:
        chat_mod._sdk_cron_jobs.pop(sid, None)


# ====== interrupt ======

def test_interrupt_no_live_client(chat_mod, client):
    """interrupt on a session with no client returns the no-op note,
    NOT an error."""
    r = client.post(f"/api/chat/interrupt?session_id=ghost&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == []
    assert body.get("note") == "no live client"
    # No bogus pending-interrupt flag left behind.
    assert "ghost" not in chat_mod._pending_interrupts


def test_interrupt_calls_sdk_and_marks_pending(chat_mod, client):
    """interrupt must call client.interrupt(), record 'sid@model', and set
    the pending-interrupt flag (used to suppress the turn-done push)."""
    c = _seed(chat_mod, ("sid-int", "claude-sonnet-4-6", "auto", ""))
    r = client.post(f"/api/chat/interrupt?session_id=sid-int&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == ["sid-int@claude-sonnet-4-6"]
    assert c.interrupted is True
    assert ("sid-int", "") in chat_mod._pending_interrupts


def test_interrupt_rejects_stale_turn_before_touching_client_or_queue(
        chat_mod, client, monkeypatch):
    sid = "sid-stale-stop"
    sdk_client = _seed(
        chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    current = chat_mod.TurnBroadcast(sid)
    chat_mod._active_turns[sid] = current
    pauses = []
    monkeypatch.setattr(
        chat_mod.sess,
        "pause_queue_if_nonempty",
        lambda stopped_sid: pauses.append(stopped_sid),
    )
    try:
        response = client.post(
            f"/api/chat/interrupt?session_id={sid}&turn_id=older-turn"
            f"&token={TEST_TOKEN}")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "interrupted": [],
            "stale": True,
            "requested_turn_id": "older-turn",
            "current_turn_id": current.turn_id,
            "turn_id": current.turn_id,
            "active": True,
            "stopping": False,
            "phase": "running",
        }
        assert sdk_client.interrupted is False
        assert pauses == []
        assert (sid, "older-turn") not in chat_mod._pending_interrupts
    finally:
        chat_mod._active_turns.pop(sid, None)
        current.close()


@pytest.mark.asyncio
async def test_interrupt_rechecks_exact_owner_after_queue_pause(
        chat_mod, monkeypatch):
    sid = "sid-stop-owner-race"
    sdk_client = _seed(
        chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    old = chat_mod.TurnBroadcast(sid)
    replacement = chat_mod.TurnBroadcast(sid)
    chat_mod._active_turns[sid] = old
    pause_entered = threading.Event()
    release_pause = threading.Event()

    def blocked_pause(_sid):
        pause_entered.set()
        assert release_pause.wait(1)
        return {"items": [], "paused": False}

    monkeypatch.setattr(
        chat_mod.sess, "pause_queue_if_nonempty", blocked_pause)
    try:
        stop_task = asyncio.create_task(
            chat_mod.interrupt(sid, turn_id=old.turn_id))
        assert await asyncio.to_thread(pause_entered.wait, 1)

        # Model the old pump finishing and a successor taking the same session
        # while the queue pause is in flight. The delayed Stop belongs only to
        # `old`; its pooled runtime snapshot must never reach `replacement`.
        chat_mod._active_turns[sid] = replacement
        release_pause.set()
        response = await asyncio.wait_for(stop_task, timeout=1)

        assert response["stale"] is True
        assert response["requested_turn_id"] == old.turn_id
        assert response["current_turn_id"] == replacement.turn_id
        assert sdk_client.interrupted is False
        assert replacement.cancelled is False
        assert (sid, old.turn_id) not in chat_mod._pending_interrupts
    finally:
        release_pause.set()
        chat_mod._active_turns.pop(sid, None)
        old.close()
        replacement.close()


def test_interrupt_swallows_sdk_error_but_still_marks_pending(chat_mod, client):
    """If client.interrupt() raises, the route must not 500 — it logs and
    returns ok with that client omitted from `interrupted`. Pending flag
    is set BEFORE the SDK call, so it stays set (better early than late)."""
    c = _FakeSDKClient()
    c._raise_on_interrupt = True
    _seed(chat_mod, ("sid-boom", "claude-sonnet-4-6", "auto", ""), client=c)
    r = client.post(f"/api/chat/interrupt?session_id=sid-boom&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == []   # failing client omitted
    assert ("sid-boom", "") in chat_mod._pending_interrupts


def test_stop_background_task_uses_sdk_control_channel(chat_mod, client):
    """Stopping a named background task is distinct from interrupting the
    foreground turn and delegates to ClaudeAgentSDK.stop_task()."""
    sid = "sid-task-stop"

    class TaskClient(_FakeSDKClient):
        def __init__(self):
            super().__init__()
            self.stopped_tasks = []

        async def stop_task(self, task_id):
            self.stopped_tasks.append(task_id)

    sdk_client = TaskClient()
    _seed(
        chat_mod,
        (sid, "claude-sonnet-4-6", "auto", ""),
        client=sdk_client,
    )

    response = client.post(
        f"/api/chat/sessions/{sid}/tasks/task-123/stop",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "task_id": "task-123"}
    assert sdk_client.stopped_tasks == ["task-123"]
    assert sdk_client.interrupted is False


def test_stop_background_task_without_live_owner_is_conflict(chat_mod, client):
    response = client.post(
        "/api/chat/sessions/missing/tasks/task-123/stop",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 409, response.text
    assert "no live client" in response.json()["detail"]


def test_interrupt_does_not_inherit_sdk_60_second_ack_timeout(
        chat_mod, client, monkeypatch):
    """A wedged SDK control request must not make the Stop button wait."""
    sid = "sid-slow-interrupt"

    class SlowClient(_FakeSDKClient):
        async def interrupt(self):
            await asyncio.Event().wait()

    _seed(chat_mod, (sid, "claude-sonnet-4-6", "auto", ""), client=SlowClient())
    monkeypatch.setattr(chat_mod, "_INTERRUPT_ACK_TIMEOUT_S", 0.01)

    response = client.post(
        f"/api/chat/interrupt?session_id={sid}&token={TEST_TOKEN}")

    assert response.status_code == 200, response.text
    assert response.json()["interrupted"] == []
    assert (sid, "") in chat_mod._pending_interrupts


@pytest.mark.asyncio
async def test_interrupt_cancels_cold_client_startup_immediately(
        chat_mod, monkeypatch):
    """Stop must work before get_client() has produced a cached client."""
    from backend import activity as activity_module

    sid = "00000000-0000-4000-8000-000000000042"
    startup_entered = asyncio.Event()
    activity_transitions = []

    async def slow_get_client(*_args, **_kwargs):
        startup_entered.set()
        await asyncio.Event().wait()

    async def no_watcher(_sid):
        return None

    monkeypatch.setattr(chat_mod, "get_client", slow_get_client)
    monkeypatch.setattr(chat_mod, "_handoff_task_watcher", no_watcher)
    monkeypatch.setattr(
        chat_mod.sess,
        "get_session",
        lambda _sid: {"model": "glm-5.2-internal", "effort": ""},
    )
    monkeypatch.setattr(chat_mod.sess, "update_permission", lambda *_a: True)
    monkeypatch.setattr(chat_mod.sess, "bump_session", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda activity_sid, *, summary="", activity_source="", owner_id="": activity_transitions.append(
            ("start", activity_sid, summary)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None: activity_transitions.append(
            ("finish", activity_sid, status)),
    )

    start_task = asyncio.create_task(chat_mod._start_turn(sid, "stop me"))
    await asyncio.wait_for(startup_entered.wait(), timeout=1)
    broadcast = chat_mod._active_turns[sid]
    assert activity_transitions == [("start", sid, "stop me")]

    result = await chat_mod.interrupt(sid)
    finished = await asyncio.wait_for(start_task, timeout=1)

    assert result["interrupted"] == [f"{sid}@startup"]
    assert result["phase"] == "starting"
    assert finished is broadcast
    assert broadcast.cancelled is True
    assert broadcast.done is True
    replay = list(broadcast.replay_events())
    assert [event["event"] for event in replay] == [
        "startup", "startup", "cancelled",
    ]
    cancelled_payload = json.loads(replay[-1]["data"])
    assert cancelled_payload["snapshot_ready"] is True
    snapshots, _ = chat_mod._load_cancelled_turn_snapshots(sid)
    assert [message["text"] for message in snapshots[0]["messages"]] == ["stop me"]
    assert sid not in chat_mod._active_turns
    assert sid not in chat_mod._clients
    assert activity_transitions == [
        ("start", sid, "stop me"),
        ("finish", sid, "cancelled"),
    ]
    recent = chat_mod._recent_turns.pop(sid, None)
    if recent is not None:
        recent.close()
    chat_mod._delete_cancelled_turn_snapshots(sid)


@pytest.mark.asyncio
async def test_request_cancel_during_activity_start_releases_queue_claim(
        chat_mod, monkeypatch):
    """Cancellation at the Activity write cannot leave a phantom busy turn."""
    from backend import activity as activity_module

    sid = "00000000-0000-4000-8000-000000000043"
    queued = chat_mod.sess.enqueue_message(sid, "queued startup")["item"]
    chat_mod.sess.claim_queue_message(sid)
    activity_entered = threading.Event()
    release_activity = threading.Event()
    transitions = []

    def blocking_start(
        activity_sid, *, summary="", activity_source="", owner_id="",
    ):
        activity_entered.set()
        assert release_activity.wait(timeout=2)
        transitions.append(("start", activity_sid, summary, activity_source))

    def finish(
        activity_sid,
        status,
        *,
        activity_source="",
        owner_id="",
        mark_read=None,
    ):
        transitions.append(("finish", activity_sid, status, activity_source))

    async def no_watcher(_sid):
        return None

    async def must_not_start_client(*_args, **_kwargs):
        raise AssertionError("client startup must not begin")

    monkeypatch.setattr(chat_mod, "_handoff_task_watcher", no_watcher)
    monkeypatch.setattr(chat_mod, "get_client", must_not_start_client)
    monkeypatch.setattr(
        chat_mod.sess,
        "get_session",
        lambda _sid: {"model": "claude-sonnet-4-6", "effort": "auto"},
    )
    monkeypatch.setattr(
        chat_mod, "_heal_unreachable_locked_model",
        lambda _sid, locked, _requested: locked,
    )
    monkeypatch.setattr(activity_module.activity, "start", blocking_start)
    monkeypatch.setattr(activity_module.activity, "finish", finish)

    task = asyncio.create_task(chat_mod._start_turn(
        sid,
        "queued startup",
        permission="default",
        persist_permission=False,
        queue_item_id=queued["id"],
    ))
    assert await asyncio.to_thread(activity_entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    release_activity.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    queue = chat_mod.sess.get_queue(sid)
    assert sid not in chat_mod._active_turns
    assert queue["inflight"] is None
    assert queue["paused"] is True
    assert [item["id"] for item in queue["items"]] == [queued["id"]]
    assert transitions == [
        ("start", sid, "queued startup", "queued"),
        ("finish", sid, "cancelled", "queued"),
    ]


@pytest.mark.asyncio
async def test_repeated_cancel_during_activity_start_cannot_leave_phantom(
        chat_mod, monkeypatch):
    """Two cancellation sources still join the non-cancellable worker write."""
    from backend import activity as activity_module

    sid = "sid-double-cancel-activity"
    broadcast = chat_mod.TurnBroadcast(sid)
    entered = threading.Event()
    release = threading.Event()
    transitions = []

    def blocked_start(
        activity_sid, *, summary="", activity_source="", owner_id="",
    ):
        entered.set()
        assert release.wait(timeout=2)
        transitions.append(("start", activity_sid))

    monkeypatch.setattr(activity_module.activity, "start", blocked_start)
    task = asyncio.create_task(
        chat_mod._start_activity_early(sid, broadcast, "prompt"))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert transitions == [("start", sid)]
    assert broadcast.activity_started is True
    await chat_mod._finish_activity(sid, broadcast, "cancelled")
    assert broadcast.activity_started is False


@pytest.mark.asyncio
async def test_cancelled_startup_keeps_slot_until_activity_is_terminal(
        chat_mod, monkeypatch):
    """Old Activity finish cannot race a replacement turn for the same sid."""
    sid = "sid-cancel-activity-order"
    broadcast = chat_mod.TurnBroadcast(sid)
    broadcast.cancelled = True
    broadcast.activity_started = True
    chat_mod._active_turns[sid] = broadcast
    finish_entered = asyncio.Event()
    release_finish = asyncio.Event()

    monkeypatch.setattr(
        chat_mod, "_persist_cancelled_turn_snapshot", lambda _bc: True)

    async def blocked_finish(_sid, _broadcast, status):
        assert status == "cancelled"
        finish_entered.set()
        await release_finish.wait()
        _broadcast.activity_started = False

    monkeypatch.setattr(chat_mod, "_finish_activity", blocked_finish)
    cleanup = asyncio.create_task(
        chat_mod._finish_cancelled_startup(sid, broadcast))
    await asyncio.wait_for(finish_entered.wait(), timeout=1)

    assert chat_mod._active_turns[sid] is broadcast
    assert broadcast.done is False
    release_finish.set()
    assert await cleanup is broadcast
    assert sid not in chat_mod._active_turns
    assert broadcast.done is True
    assert chat_mod._recent_turns[sid] is broadcast
    recent = chat_mod._recent_turns.pop(sid)
    recent.close()


@pytest.mark.asyncio
async def test_second_cancel_during_snapshot_still_finishes_startup_cleanup(
        chat_mod, monkeypatch):
    """A subscriber disconnect cannot cancel the worker-backed cleanup."""
    sid = "sid-cancel-snapshot-join"
    broadcast = chat_mod.TurnBroadcast(sid)
    broadcast.cancelled = True
    broadcast.queue_item_id = "q-corrupt"
    broadcast.activity_started = True
    chat_mod._active_turns[sid] = broadcast
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    finished = []

    def bad_release(*_args, **_kwargs):
        raise RuntimeError("corrupt queue")

    def blocked_snapshot(_broadcast):
        snapshot_entered.set()
        assert release_snapshot.wait(timeout=2)
        return True

    async def finish_activity(_sid, _broadcast, status):
        finished.append(status)
        _broadcast.activity_started = False

    monkeypatch.setattr(chat_mod.sess, "release_queue_claim", bad_release)
    monkeypatch.setattr(
        chat_mod, "_persist_cancelled_turn_snapshot", blocked_snapshot)
    monkeypatch.setattr(chat_mod, "_finish_activity", finish_activity)

    task = asyncio.create_task(
        chat_mod._finish_cancelled_startup(sid, broadcast))
    assert await asyncio.to_thread(snapshot_entered.wait, 1)
    task.cancel()
    release_snapshot.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished == ["cancelled"]
    assert sid not in chat_mod._active_turns
    assert broadcast.done is True
    assert chat_mod._recent_turns[sid] is broadcast
    recent = chat_mod._recent_turns.pop(sid)
    recent.close()


def test_interrupt_pauses_nonempty_queue_before_sdk_call(chat_mod, client):
    """The current turn may finish while interrupt() awaits the SDK. Queue
    state must already be paused then, otherwise its finally block can dequeue
    and start the next turn after the user pressed Stop."""
    from backend import sessions as sess

    sid = "sid-queued-stop"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    sess.enqueue_message(sid, "do not auto-run")
    observed = []

    async def inspect_interrupt():
        observed.append(sess.get_queue(sid))
        c.interrupted = True

    c.interrupt = inspect_interrupt
    response = client.post(
        f"/api/chat/interrupt?session_id={sid}&token={TEST_TOKEN}")

    assert response.status_code == 200, response.text
    assert observed and observed[0]["paused"] is True
    assert observed[0]["items"][0]["text"] == "do not auto-run"


# ====== force-stop watchdog (interrupt that the SDK refuses to honor) ======

@pytest.mark.asyncio
async def test_session_runtime_cleanup_invalidates_continuation_owner(chat_mod):
    sid = "sid-delete-continuation"
    watcher = asyncio.create_task(asyncio.sleep(60))
    prewarm = asyncio.create_task(asyncio.sleep(60))
    pump = asyncio.create_task(asyncio.sleep(60))
    broadcast = chat_mod.TurnBroadcast(sid)
    broadcast.task = pump
    chat_mod._task_watchers[sid] = watcher
    chat_mod._runtime_prewarm_tasks[sid] = prewarm
    chat_mod._continuation_generations[sid] = 3
    chat_mod._active_turns[sid] = broadcast
    chat_mod._recent_turns[sid] = broadcast
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    chat_mod._bg_task_descriptions["task-1"] = "background work"
    chat_mod._background_turn_started_at[sid] = 1_700_000_000
    chat_mod._background_origin_turn_id[sid] = "origin-turn"

    chat_mod._clear_session_runtime_state(sid)
    await asyncio.gather(watcher, prewarm, pump, return_exceptions=True)

    assert chat_mod._continuation_generations[sid] == 4
    assert sid not in chat_mod._task_watchers
    assert sid not in chat_mod._runtime_prewarm_tasks
    assert sid not in chat_mod._active_turns
    assert sid not in chat_mod._recent_turns
    assert sid not in chat_mod._sessions_with_inflight_tasks
    assert sid not in chat_mod._background_turn_started_at
    assert sid not in chat_mod._background_origin_turn_id
    assert "task-1" not in chat_mod._bg_task_descriptions
    assert broadcast.cancelled is True
    assert broadcast.done is True
    assert prewarm.cancelled() is True


@pytest.mark.asyncio
async def test_async_purge_does_not_treat_background_watcher_as_activity(
        chat_mod, monkeypatch):
    from backend import activity as activity_module

    sid = "sid-purge-loop-thread"
    loop_thread = threading.get_ident()
    observed_threads = []
    activity_finishes = []
    original_clear = chat_mod._clear_session_runtime_state

    def tracked_clear(target_sid):
        observed_threads.append(threading.get_ident())
        return original_clear(target_sid)

    async def no_disconnect(_sid):
        return None

    monkeypatch.setattr(chat_mod, "_clear_session_runtime_state", tracked_clear)
    monkeypatch.setattr(chat_mod, "disconnect_client", no_disconnect)
    monkeypatch.setattr(
        chat_mod,
        "_purge_single_session_storage",
        lambda _sid: True,
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status, *, activity_source="", owner_id="", mark_read=None:
            activity_finishes.append((activity_sid, status, activity_source)),
    )
    watcher = asyncio.create_task(asyncio.sleep(0))
    await watcher
    chat_mod._task_watchers[sid] = watcher

    assert await chat_mod.purge_session_storage_async(sid) is True
    assert observed_threads == [loop_thread]
    assert activity_finishes == []


@pytest.mark.asyncio
async def test_get_client_rejects_cold_commit_after_delete_fence(
        chat_mod, monkeypatch):
    sid = "sid-delete-during-client-build"
    key = (sid, "claude-sonnet-4-6", "auto", "")
    build_entered = asyncio.Event()
    release_build = asyncio.Event()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    fake_client = FakeClient()

    async def blocked_build(*_args, **_kwargs):
        build_entered.set()
        await release_build.wait()
        return fake_client

    monkeypatch.setattr(chat_mod, "_build_and_connect_client", blocked_build)
    monkeypatch.setattr(chat_mod, "_has_enabled_external_mcp", lambda: False)

    creating = asyncio.create_task(chat_mod.get_client(
        sid, "claude-sonnet-4-6", effort="auto"))
    await asyncio.wait_for(build_entered.wait(), timeout=1)
    await asyncio.to_thread(chat_mod.sess.begin_session_delete, sid)
    release_build.set()

    with pytest.raises(RuntimeError, match="being deleted"):
        await asyncio.wait_for(creating, timeout=1)
    assert fake_client.disconnected is True
    assert key not in chat_mod._clients
    assert key not in chat_mod._session_streams
    assert key not in chat_mod._client_lru


@pytest.mark.asyncio
async def test_async_purge_aborts_when_scheduler_join_fails(
        chat_mod, monkeypatch):
    disk_purges = []

    async def fail_join(_sid, _runs, *, timeout):
        raise RuntimeError("scheduler join failed")

    monkeypatch.setattr(
        chat_mod,
        "_join_session_runtime_cleanup",
        fail_join,
    )
    monkeypatch.setattr(
        chat_mod,
        "purge_session_storage",
        lambda sid: disk_purges.append(sid) or True,
    )

    with pytest.raises(RuntimeError, match="scheduler join failed"):
        await chat_mod.purge_session_storage_async("sid-join-failure")
    assert disk_purges == []


@pytest.mark.asyncio
async def test_async_purge_timeout_keeps_disk_until_owner_finishes(
        chat_mod, monkeypatch):
    disk_purges = []

    async def incomplete_cleanup(_sid, _runs, *, timeout):
        return False

    monkeypatch.setattr(
        chat_mod,
        "_join_session_runtime_cleanup",
        incomplete_cleanup,
    )
    monkeypatch.setattr(
        chat_mod,
        "purge_session_storage",
        lambda sid: disk_purges.append(sid) or True,
    )

    with pytest.raises(chat_mod.RuntimeCleanupTimeout):
        await chat_mod.purge_session_storage_async("sid-owner-pending")
    assert disk_purges == []


@pytest.mark.asyncio
async def test_async_purge_public_deadline_reuses_same_owner(
        chat_mod, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_purge(_sid):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(chat_mod, "_SESSION_DELETE_DEADLINE_S", 0.01)
    monkeypatch.setattr(
        chat_mod,
        "_purge_session_storage_async_inner",
        slow_purge,
    )

    with pytest.raises(chat_mod.RuntimeCleanupTimeout):
        await chat_mod.purge_session_storage_async("sid-slow-purge")
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert calls == 1

    release.set()
    assert await chat_mod.purge_session_storage_async("sid-slow-purge") is True
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_mcp_gate_disconnects_unpooled_client(
        chat_mod, monkeypatch):
    sid = "sid-cancel-unpooled-mcp"
    gate_entered = asyncio.Event()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    fake_client = FakeClient()

    async def build(*_args, **_kwargs):
        return fake_client

    async def blocked_gate(_client):
        gate_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_mod, "_build_and_connect_client", build)
    monkeypatch.setattr(chat_mod, "_has_enabled_external_mcp", lambda: True)
    monkeypatch.setattr(chat_mod, "_await_mcp_ready", blocked_gate)

    creating = asyncio.create_task(chat_mod.get_client(
        sid, "claude-sonnet-4-6", effort="auto"))
    await asyncio.wait_for(gate_entered.wait(), timeout=1)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(creating, timeout=1)

    assert fake_client.disconnected is True
    assert all(key[0] != sid for key in chat_mod._clients)
    assert all(key[0] != sid for key in chat_mod._session_streams)


@pytest.mark.asyncio
async def test_async_purge_joins_scheduler_run_before_disk_delete(
        chat_mod, monkeypatch):
    from backend import activity as activity_module
    from backend import scheduler

    sid = "sid-running-scheduler-delete"
    task = {
        "id": "scheduled-delete",
        "name": "Scheduled delete",
        "prompt": "wait",
        "session_id": sid,
        "session_mode": "reuse",
        "model": "",
    }
    entered = asyncio.Event()
    events = []

    async def blocked_turn(_sid, _model, _prompt, **kwargs):
        scheduler._mark_current_run_activity_started()
        activity_module.activity.start(
            _sid,
            summary=kwargs["activity_summary"],
            kind="scheduled",
            source_id=kwargs["activity_source_id"],
            owner_id=kwargs["activity_owner_id"],
        )
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "_run_sdk_task_turn", blocked_turn)
    monkeypatch.setattr(
        activity_module.activity,
        "start",
        lambda activity_sid, **_kwargs:
            events.append(("start", activity_sid)),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda activity_sid, status, **_kwargs:
            events.append(("finish", activity_sid, status)),
    )

    running = scheduler._track_task(
        asyncio.create_task(scheduler._execute_task(task)),
        task_id=task["id"],
        session_id=sid,
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    def disk_purge(target_sid):
        assert running.done()
        assert scheduler._state["history"] == []
        assert scheduler._state["unread_count"] == 0
        events.append(("disk", target_sid))
        return True

    async def no_disconnect(_sid):
        return None

    monkeypatch.setattr(chat_mod, "disconnect_client", no_disconnect)
    monkeypatch.setattr(
        chat_mod, "_purge_single_session_storage", disk_purge)

    assert await chat_mod.purge_session_storage_async(sid) is True
    assert running.cancelled()
    assert events[0] == ("start", sid)
    assert events[-1] == ("disk", sid)
    assert any(event == ("finish", sid, "cancelled") for event in events)


@pytest.mark.asyncio
async def test_async_purge_joins_future_startup_cleanup_before_disk_delete(
        chat_mod, monkeypatch):
    """DELETE must join cleanup created only after cold startup is cancelled."""
    from backend import activity as activity_module

    sid = "00000000-0000-4000-8000-000000000044"
    chat_mod.sess.register_session(
        sid,
        name="deleting cold startup",
        model="claude-sonnet-4-6",
    )
    startup_entered = asyncio.Event()
    finish_entered = asyncio.Event()
    release_finish = asyncio.Event()
    transitions = []

    async def slow_get_client(*_args, **_kwargs):
        startup_entered.set()
        await asyncio.Event().wait()

    async def no_watcher(_sid):
        return None

    async def no_disconnect(_sid):
        return None

    async def blocked_finish(activity_sid, broadcast, status):
        if not broadcast.activity_started:
            return
        assert status == "cancelled"
        finish_entered.set()
        await release_finish.wait()
        transitions.append(("finish", activity_sid))
        broadcast.activity_started = False

    def activity_start(
        activity_sid, *, summary="", activity_source="", owner_id="",
    ):
        transitions.append(("start", activity_sid))

    def disk_purge(target_sid):
        assert target_sid == sid
        assert transitions == [("start", sid), ("finish", sid)]
        transitions.append(("disk", target_sid))
        chat_mod.sess.delete_session(target_sid)
        return True

    monkeypatch.setattr(chat_mod, "get_client", slow_get_client)
    monkeypatch.setattr(chat_mod, "_handoff_task_watcher", no_watcher)
    monkeypatch.setattr(chat_mod, "disconnect_client", no_disconnect)
    monkeypatch.setattr(chat_mod, "_finish_activity", blocked_finish)
    monkeypatch.setattr(
        chat_mod, "_purge_single_session_storage", disk_purge)
    monkeypatch.setattr(activity_module.activity, "start", activity_start)
    monkeypatch.setattr(
        activity_module.activity,
        "finish",
        lambda _sid, _status, *, activity_source="", owner_id="": None,
    )

    start_task = asyncio.create_task(chat_mod._start_turn(sid, "delete me"))
    await asyncio.wait_for(startup_entered.wait(), timeout=1)
    broadcast = chat_mod._active_turns[sid]
    assert broadcast.startup_owner_task is start_task

    purge_task = asyncio.create_task(chat_mod.purge_session_storage_async(sid))
    await asyncio.wait_for(finish_entered.wait(), timeout=1)
    await asyncio.sleep(0)

    # Disk deletion cannot overtake the late Activity cleanup created by the
    # outer `_start_turn` owner after its nested SDK startup was cancelled.
    assert not purge_task.done()
    assert sid not in chat_mod._recent_turns
    release_finish.set()

    assert await asyncio.wait_for(purge_task, timeout=1) is True
    assert await asyncio.wait_for(start_task, timeout=1) is broadcast
    assert transitions == [("start", sid), ("finish", sid), ("disk", sid)]
    assert broadcast.activity_started is False
    assert sid not in chat_mod._active_turns
    assert sid not in chat_mod._recent_turns


def test_sync_purge_rechecks_late_successor_after_delete_fence(
        chat_mod, monkeypatch):
    source_sid = "late-source"
    late_sid = "late-successor"
    fenced = []
    purged = []

    monkeypatch.setattr(
        chat_mod.sess, "runtime_lineage", lambda _sid: [source_sid])
    monkeypatch.setattr(
        chat_mod.sess,
        "begin_session_delete",
        lambda runtime_sid: fenced.append(runtime_sid),
    )
    monkeypatch.setattr(
        chat_mod.sess,
        "get_session_meta",
        lambda runtime_sid: {
            "runtime_successor": late_sid if runtime_sid == source_sid else "",
        },
    )
    monkeypatch.setattr(
        chat_mod,
        "_purge_single_session_storage",
        lambda runtime_sid: purged.append(runtime_sid) or True,
    )

    assert chat_mod.purge_session_storage(source_sid) is True
    assert fenced == [source_sid, late_sid]
    assert purged == [source_sid, late_sid]


def test_sync_purge_fails_closed_for_live_runtime(chat_mod, monkeypatch):
    sid = "sync-live-runtime"
    monkeypatch.setattr(chat_mod.sess, "runtime_lineage", lambda _sid: [sid])
    active = chat_mod.TurnBroadcast(sid)
    chat_mod._active_turns[sid] = active
    try:
        with pytest.raises(RuntimeError, match="purge_session_storage_async"):
            chat_mod.purge_session_storage(sid)
    finally:
        chat_mod._active_turns.pop(sid, None)
        active.close()


def test_sync_purge_rechecks_live_owner_after_delete_fence(
        chat_mod, monkeypatch):
    sid = "sync-reservation-race"
    active = chat_mod.TurnBroadcast(sid)
    disk_purged = False

    monkeypatch.setattr(chat_mod.sess, "runtime_lineage", lambda _sid: [sid])

    def fence(_sid):
        chat_mod._active_turns[sid] = active

    def must_not_purge(_sid):
        nonlocal disk_purged
        disk_purged = True
        return True

    monkeypatch.setattr(chat_mod.sess, "begin_session_delete", fence)
    monkeypatch.setattr(
        chat_mod.sess, "get_session_meta", lambda _sid: {})
    monkeypatch.setattr(
        chat_mod, "_purge_single_session_storage", must_not_purge)
    try:
        with pytest.raises(RuntimeError, match="purge_session_storage_async"):
            chat_mod.purge_session_storage(sid)
        assert disk_purged is False
    finally:
        chat_mod._active_turns.pop(sid, None)
        active.close()


@pytest.mark.asyncio
async def test_sync_and_async_purge_from_leaf_clear_full_runtime_lineage(
        chat_mod, monkeypatch):
    def missing_sdk(*_args, **_kwargs):
        raise FileNotFoundError

    def register_lineage(ids):
        for index, runtime_sid in enumerate(ids):
            chat_mod.sess.register_session(
                runtime_sid,
                name=f"runtime-{index}",
                model="claude-sonnet-4-6",
                runtime_predecessor=ids[index - 1] if index else "",
            )
            if index:
                assert chat_mod.sess.link_runtime_successor(
                    ids[index - 1], runtime_sid)

    def seed_private_artifacts(ids):
        outbox = chat_mod._runtime_continuation_outbox_path(
            ids[0], "abababab-cdcd-4efe-8a8a-010101010101")
        snapshot = chat_mod._cancelled_turn_snapshot_path(
            ids[-1], "bcbcbcbc-dede-4faf-8b8b-020202020202")
        for path in (outbox, snapshot):
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        return outbox, snapshot

    monkeypatch.setattr(chat_mod, "sdk_delete_session", missing_sdk)
    sync_ids = [
        "56565656-7878-49ab-8ccd-efefefefefef",
        "67676767-8989-4abc-8dde-f0f0f0f0f0f0",
        "78787878-9a9a-4bcd-8eef-010101010101",
    ]
    register_lineage(sync_ids)
    sync_outbox, sync_snapshot = seed_private_artifacts(sync_ids)
    assert chat_mod.purge_session_storage(sync_ids[-1]) is True
    assert all(
        chat_mod.sess.get_session_meta(runtime_sid) is None
        for runtime_sid in sync_ids
    )
    assert not sync_outbox.exists()
    assert not sync_snapshot.exists()

    async_ids = [
        "89898989-abab-4cde-8ff0-121212121212",
        "9a9a9a9a-bcbc-4def-8011-232323232323",
        "abababab-cdcd-4ef0-8122-343434343434",
    ]
    register_lineage(async_ids)
    async_outbox, async_snapshot = seed_private_artifacts(async_ids)
    assert await chat_mod.purge_session_storage_async(async_ids[-1]) is True
    assert all(
        chat_mod.sess.get_session_meta(runtime_sid) is None
        for runtime_sid in async_ids
    )
    assert not async_outbox.exists()
    assert not async_snapshot.exists()


@pytest.mark.asyncio
async def test_force_stop_tears_down_stuck_turn(chat_mod, monkeypatch):
    """The SDK's client.interrupt() is best-effort; for an agentic turn the CLI
    may keep running, pinning the slot in _active_turns and bouncing every
    subsequent send with 'previous turn still running'. The force-stop watchdog
    must, after the grace window, kill the client and free the slot itself."""
    sid = "sid-stuck"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    bc = chat_mod.TurnBroadcast(session_id=sid, model="claude-sonnet-4-6")
    bc.queue_item_id = "q-stuck"
    bc.activity_started = True
    activity_finishes = []
    queue_releases = []
    queue_pauses = []
    memory_clears = []
    remembered = []

    async def finish_activity(got_sid, got_bc, status):
        activity_finishes.append((got_sid, got_bc.turn_id, status))
        got_bc.activity_started = False

    monkeypatch.setattr(chat_mod, "_finish_activity", finish_activity)
    monkeypatch.setattr(
        chat_mod.sess, "release_queue_claim",
        lambda got_sid, item_id, **kwargs: queue_releases.append(
            (got_sid, item_id, kwargs.get("turn_id"), kwargs.get("pause"))) or True,
    )
    monkeypatch.setattr(
        chat_mod.sess, "pause_queue_if_nonempty", queue_pauses.append)
    monkeypatch.setattr(
        chat_mod.mem0, "pop_recall_trace", memory_clears.append)
    monkeypatch.setattr(
        chat_mod, "_remember_recent_turn",
        lambda got_sid, got_bc: remembered.append((got_sid, got_bc.turn_id)),
    )
    chat_mod._active_turns[sid] = bc
    try:
        # Tiny grace; the (absent) pump never frees the slot, so the watchdog
        # must force teardown: disconnect the client + free the slot by hand.
        await chat_mod._force_stop_after_grace(sid, bc, grace=0.01)
        assert c.disconnected is True            # CLI killed
        assert sid not in chat_mod._active_turns  # slot freed → next send works
        assert bc.cancelled is True
        assert bc.done is True                    # subscribers get the sentinel
        assert activity_finishes == [(sid, bc.turn_id, "cancelled")]
        assert queue_releases == [(sid, "q-stuck", bc.turn_id, True)]
        assert queue_pauses == [sid]
        assert memory_clears == [sid]
        assert remembered == [(sid, bc.turn_id)]
    finally:
        chat_mod._active_turns.pop(sid, None)


@pytest.mark.asyncio
async def test_force_stop_lets_cancelled_pump_own_terminal_cleanup(
    chat_mod, monkeypatch,
):
    """Cancelling the real pump must not race a second manual settlement."""
    sid = "sid-pump-cleanup"
    _seed(chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    bc = chat_mod.TurnBroadcast(session_id=sid, model="claude-sonnet-4-6")
    manual_finishes = []
    monkeypatch.setattr(
        chat_mod,
        "_finish_activity",
        lambda *args, **kwargs: manual_finishes.append((args, kwargs)),
    )

    async def pump():
        try:
            await asyncio.Event().wait()
        finally:
            bc.finish()
            chat_mod._active_turns.pop(sid, None)

    task = asyncio.create_task(pump())
    bc.task = task
    chat_mod._active_turns[sid] = bc
    await asyncio.sleep(0)
    try:
        await chat_mod._force_stop_after_grace(sid, bc, grace=0.01)
        assert task.done()
        assert bc.done is True
        assert sid not in chat_mod._active_turns
        assert manual_finishes == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        chat_mod._active_turns.pop(sid, None)


@pytest.mark.asyncio
async def test_force_stop_noop_when_turn_drained_naturally(chat_mod):
    """If the SDK interrupt DID drain the turn within the grace window, the
    watchdog must not tear down the (now warm) client — that would needlessly
    drop the CLI subprocess on every successful interrupt."""
    sid = "sid-drained"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", "auto", ""))
    bc = chat_mod.TurnBroadcast(session_id=sid, model="claude-sonnet-4-6")
    bc.finish()   # turn ended naturally before grace elapsed
    # _active_turns no longer holds it (the pump's finally popped it).
    await chat_mod._force_stop_after_grace(sid, bc, grace=0.01)
    assert c.disconnected is False


def test_cancelled_turn_snapshot_survives_reload_export_and_delete(
    chat_mod, client, auth,
):
    """A force-stopped turn is display history even when no CLI JSONL exists."""
    created = client.post(
        "/api/chat/sessions",
        headers=auth,
        json={"name": "cancel snapshot", "model": "claude-sonnet-4-6"},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    bc = chat_mod.TurnBroadcast(sid, model="claude-sonnet-4-6")
    bc.user_text = "keep this interrupted prompt"
    bc.cancelled = True
    bc.publish({
        "event": "text",
        "data": '{"text":"partial assistant text"}',
    })
    bc.publish({
        "event": "thinking",
        "data": '{"text":"partial reasoning"}',
    })
    bc.publish({
        "event": "tool_use",
        "data": (
            '{"name":"Read","id":"toolu_cancelled",'
            '"summary":"notes.md","input":{"file_path":"notes.md"}}'
        ),
    })
    bc.publish({
        "event": "tool_result",
        "data": (
            '{"id":"toolu_cancelled","tool_name":"Read",'
            '"preview":"line one","text":"line one\\nline two",'
            '"truncated":false,"text_truncated":false,"is_error":false}'
        ),
    })

    try:
        assert chat_mod._persist_cancelled_turn_snapshot(bc) is True
        path = chat_mod._cancelled_turn_snapshot_path(sid, bc.turn_id)
        assert path is not None and path.exists()
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600

        first = client.get(
            f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 50})
        assert first.status_code == 200, first.text
        body = first.json()
        messages = body["messages"]
        assert [message["role"] for message in messages] == [
            "user", "assistant", "thinking", "tool_use", "tool_result",
        ]
        assert messages[0]["text"] == "keep this interrupted prompt"
        assert messages[1]["text"] == "partial assistant text"
        assert messages[3]["id"] == "toolu_cancelled"
        assert messages[4]["text"] == "line one\nline two"
        assert all(message["_interrupted"] is True for message in messages)
        assert len({message["_key"] for message in messages}) == len(messages)
        assert "~cancelled-" in body["history_generation"]
        assert body["message_count"] == len(messages)
        assert body["turn_count"] == 1

        # Disk, not _active_turns/_recent_turns, is the recovery source.
        chat_mod._active_turns.pop(sid, None)
        chat_mod._recent_turns.pop(sid, None)
        second = client.get(
            f"/api/chat/sessions/{sid}", headers=auth, params={"tail": 50})
        assert second.status_code == 200, second.text
        assert second.json()["messages"] == messages

        ticket = client.post(
            "/api/chat/resource-ticket",
            headers=auth,
            json={"resource": "export", "session_id": sid},
        )
        assert ticket.status_code == 200, ticket.text
        exported = client.get(ticket.json()["url"])
        assert exported.status_code == 200, exported.text
        assert "keep this interrupted prompt" in exported.text
        assert "partial assistant text" in exported.text

        deleted = client.delete(f"/api/chat/sessions/{sid}", headers=auth)
        assert deleted.status_code == 200, deleted.text
        assert not path.exists()
    finally:
        bc.close()


# ====== probe_provider ======

def test_probe_unknown_model(client, auth):
    """probe/{model} for an unknown model returns ok=False with a reason,
    not a 500."""
    r = client.get("/api/chat/probe/totally-made-up-model", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "unknown model" in body["reason"]


def test_probe_third_party_without_key(client, auth, monkeypatch):
    """probe for a real third-party model with NO configured API key returns
    ok=False pointing at Settings — no network call made."""
    # conftest already clears DEEPSEEK_API_KEY; be explicit.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = client.get("/api/chat/probe/deepseek-v4-pro", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "not configured" in body["reason"]


def test_probe_hits_vendor_endpoint_with_fake_httpx(client, auth, monkeypatch, chat_mod):
    """With a key set, probe POSTs to the vendor's /v1/messages and echoes
    the vendor status back. We inject a fake httpx.AsyncClient so no real
    network call happens, and assert the body carries the vendor status +
    masked key hint."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-abcd1234efgh5678")

    posted = {}

    class _FakeResp:
        status_code = 200
        text = '{"id":"msg_1","content":[{"type":"text","text":"pong"}]}'

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["json"] = json
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.get("/api/chat/probe/deepseek-v4-pro", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == 200
    assert body["vendor"]   # display name present
    assert body["url"].endswith("/v1/messages")
    # The key is masked, never echoed in full.
    assert "sk-deepseek-abcd1234efgh5678" not in str(body)
    assert body["key_hint"].startswith("sk-d")
    # The request carried the api key header + ping body.
    assert posted["headers"]["x-api-key"] == "sk-deepseek-abcd1234efgh5678"
    assert posted["json"]["messages"][0]["content"] == "ping"


class _FakeCompactClient:
    def __init__(self, result, totals=(190_000, 190_000)):
        self.result = result
        self.totals = iter(totals)
        self.queries = []

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield self.result

    async def get_context_usage(self):
        return {"totalTokens": next(self.totals), "maxTokens": 200_000}

def _make_compact_session(client):
    r = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "compact endpoint", "model": "claude-sonnet-4-6"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_native_compact_rejects_in_band_context_error(chat_mod, client, monkeypatch):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeCompactClient(result, totals=(190_000,))

    observed = {}

    async def fake_get_client(*args, **kwargs):
        observed["permission"] = args[2] if len(args) > 2 else kwargs.get("permission")
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    meta = chat_mod.sess.get_session_meta(sid)
    key = (
        sid,
        meta["model"] or chat_mod.MODEL,
        meta["effort"],
        meta["service_tier"],
    )
    chat_mod._client_permission[key] = "default"

    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 409, r.text
    assert "context window" in r.json()["detail"]
    assert fake.queries == ["/compact"]
    assert observed["permission"] == "default"


def test_native_compact_rejects_active_turn(chat_mod, client, monkeypatch):
    sid = _make_compact_session(client)
    chat_mod._active_turns[sid] = SimpleNamespace(done=False)

    async def should_not_get_client(*_args, **_kwargs):
        raise AssertionError("compact must stop before touching the SDK")

    monkeypatch.setattr(chat_mod, "get_client", should_not_get_client)
    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 409, r.text
    assert "turn is active" in r.json()["detail"]


def test_vendor_fork_uses_vendor_session_store_and_restores_env(
    chat_mod, client, monkeypatch, tmp_path,
):
    from backend import endpoints

    r = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "vendor fork", "model": "codex:gpt-5.6-sol"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # Test fixture availability filtering can canonicalize the requested model
    # to Claude; pin the metadata to the vendor model this regression targets.
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    vendor_dir = tmp_path / "vendor-config"
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", vendor_dir)
    observed = {}

    def fake_fork(*_args, **_kwargs):
        observed["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR")
        return SimpleNamespace(session_id="11111111-2222-4333-8444-555555555555")

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "original-config")
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"title": "vendor recovery"},
    )

    assert response.status_code == 200, response.text
    assert observed["config_dir"] == str(vendor_dir)
    assert os.environ["CLAUDE_CONFIG_DIR"] == "original-config"


def test_existing_native_transcript_overrides_third_party_model_store(
    chat_mod, client, monkeypatch,
):
    r = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "native transcript", "model": "claude-sonnet-4-6"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    native_path = (
        Path.home() / ".claude" / "projects" / "-workspace" / f"{sid}.jsonl"
    )
    original_find = chat_mod._find_session_jsonl
    monkeypatch.setattr(
        chat_mod,
        "_find_session_jsonl",
        lambda requested: native_path if requested == sid else original_find(requested),
    )
    observed = {}

    def fake_fork(*_args, **_kwargs):
        observed["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR")
        return SimpleNamespace(session_id="21111111-2222-4333-8444-555555555555")

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "inherited-vendor-config")
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"title": "native store fork"},
    )

    assert response.status_code == 200, response.text
    assert observed["config_dir"] is None
    assert os.environ["CLAUDE_CONFIG_DIR"] == "inherited-vendor-config"


def test_fork_inherits_session_settings_and_records_lineage(
    chat_mod,
    client,
    monkeypatch,
):
    from backend import activity as activity_module

    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={
            "name": "source chat",
            "model": "claude-sonnet-4-6",
            "permission": "bypassPermissions",
        },
    )
    assert source.status_code == 200, source.text
    sid = source.json()["id"]
    assert source.json()["activity_hidden"] is False
    chat_mod.sess.update_permission(sid, "plan")
    chat_mod.sess.update_effort(sid, "high")
    chat_mod.sess.update_service_tier(sid, "fast")
    chat_mod.sess.update_thinking(sid, False)
    chat_mod.sess.bump_session(sid, message_count=12, turn_count=3)
    new_sid = "11111111-2222-4333-8444-555555555555"
    boundary = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        chat_mod,
        "sdk_fork_session",
        lambda *_args, **_kwargs: SimpleNamespace(session_id=new_sid),
    )
    activity_inherits = []
    monkeypatch.setattr(
        activity_module.activity,
        "inherit_session",
        lambda source_sid, child_sid, **kwargs: activity_inherits.append(
            (source_sid, child_sid, kwargs)),
    )
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={
            "up_to_message_id": boundary,
            "title": "source chat · 分支",
            "activity_hidden": True,
            "runtime_profile": "side_question",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == new_sid
    assert body["session_id"] == new_sid
    assert body["name"] == "source chat · 分支"
    assert body["permission"] == "plan"
    assert body["plan_return_permission"] == "bypassPermissions"
    assert body["effort"] == "high"
    assert body["service_tier"] == "fast"
    assert body["thinking"] is False
    # A point-in-time fork starts with unknown presentation counts. The
    # existing session-read self-heal fills the exact values on first open
    # without making the fork request scan the new transcript twice.
    assert body["message_count"] == 0
    assert body["turn_count"] == 0
    assert body["forked_from"] == sid
    assert body["forked_from_name"] == "source chat"
    assert body["forked_from_message_id"] == boundary
    assert body["activity_hidden"] is True
    assert body["runtime_profile"] == "side_question"
    assert body["cwd"] == source.json()["cwd"]
    assert activity_inherits == [(sid, new_sid, {})]


def test_fork_activity_inheritance_failure_removes_provisional_child(
    chat_mod,
    client,
    monkeypatch,
):
    from backend import activity as activity_module

    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "atomic fork source"},
    ).json()
    child_sid = "22222222-3333-4444-8555-666666666666"
    sdk_deletes = []
    monkeypatch.setattr(
        chat_mod,
        "sdk_fork_session",
        lambda *_args, **_kwargs: SimpleNamespace(session_id=child_sid),
    )
    monkeypatch.setattr(
        activity_module.activity,
        "inherit_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("activity disk")),
    )
    monkeypatch.setattr(
        chat_mod,
        "sdk_delete_session",
        lambda sid, **_kwargs: sdk_deletes.append(sid),
    )

    response = client.post(
        f"/api/chat/sessions/{source['id']}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={},
    )

    assert response.status_code == 500
    assert chat_mod.sess.get_session_meta(child_sid) is None
    assert sdk_deletes == [child_sid]


def test_fork_child_stays_hidden_until_all_projections_commit(
    chat_mod,
    client,
    monkeypatch,
):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "hidden provisional source"},
    ).json()
    child_sid = "23232323-3434-4567-8789-676767676767"
    monkeypatch.setattr(
        chat_mod,
        "sdk_fork_session",
        lambda *_args, **_kwargs: SimpleNamespace(session_id=child_sid),
    )
    monkeypatch.setattr(
        chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    real_copy = chat_mod.sess.copy_message_annotations
    observed = []

    def inspect_hidden(source_sid, target_sid, mapping):
        meta = chat_mod.sess.get_session_meta(target_sid)
        assert meta is not None and meta["runtime_shadow"] is True
        assert target_sid not in {
            row["id"] for row in chat_mod.sess.list_sessions()
        }
        observed.append(target_sid)
        return real_copy(source_sid, target_sid, mapping)

    monkeypatch.setattr(
        chat_mod.sess, "copy_message_annotations", inspect_hidden)

    response = client.post(
        f"/api/chat/sessions/{source['id']}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={},
    )

    assert response.status_code == 200, response.text
    assert observed == [child_sid]
    assert chat_mod.sess.get_session_meta(child_sid)["runtime_shadow"] is False
    assert child_sid in {row["id"] for row in chat_mod.sess.list_sessions()}


def test_existing_successor_retry_repairs_every_durable_projection(
    chat_mod,
    client,
    monkeypatch,
):
    from backend import activity as activity_module

    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "retry source"},
    ).json()
    source_sid = source["id"]
    child_sid = "24242424-3535-4678-889a-787878787878"
    chat_mod.sess.register_session(
        child_sid,
        name="retry child",
        model=source["model"],
        runtime_predecessor=source_sid,
        cwd=source["cwd"],
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)

    old_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    new_uuid = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    chat_mod.sess.set_message_annotation(
        source_sid, old_uuid, turn_status="completed", model="Claude")
    queued = chat_mod.sess.enqueue_message(source_sid, "migrate me")
    assert queued["ok"] is True
    chat_mod.sess.set_runtime_task_overlay(
        source_sid,
        "task-retry",
        owner_session_id=source_sid,
        state="running",
    )
    event = activity_module.activity.start(source_sid, summary="preserve me")
    group = activity_module.activity.create_group("Retry lane", "cyan")["group"]
    activity_module.activity.set_group(event["id"], group["id"])
    monkeypatch.setattr(
        chat_mod,
        "_runtime_fork_uuid_mapping",
        lambda sid: {old_uuid: new_uuid} if sid == child_sid else {},
    )

    lifecycle = chat_mod._commit_fork_lifecycle(
        source_sid,
        chat_mod.sess.get_session_meta(source_sid),
        fork_child=lambda: pytest.fail("retry must not fork again"),
        register_kwargs={},
        successor=True,
        copy_runtime_overlays=True,
    )

    assert lifecycle["reused"] is True
    assert lifecycle["child_sid"] == child_sid
    assert chat_mod.sess.get_message_annotations(child_sid)[new_uuid][
        "turn_status"
    ] == "completed"
    assert [row["text"] for row in chat_mod.sess.get_queue(child_sid)["items"]] == [
        "migrate me"
    ]
    overlay = chat_mod.sess.get_runtime_task_overlays(child_sid)["task-retry"]
    assert overlay["owner_session_id"] == source_sid
    activity_row = next(
        row for row in activity_module.activity.list()
        if row["session_id"] == child_sid
    )
    assert activity_row["id"] == event["id"]
    assert activity_row["group_id"] == group["id"]
    assert chat_mod.sess.get_session_meta(source_sid)["runtime_shadow"] is True
    assert chat_mod.sess.get_session_meta(child_sid)["runtime_shadow"] is False


def test_successor_projection_failure_rolls_back_queue_activity_and_child(
    chat_mod,
    client,
    monkeypatch,
):
    from backend import activity as activity_module

    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "rollback source"},
    ).json()
    source_sid = source["id"]
    child_sid = "25252525-3636-4789-89ab-898989898989"
    event = activity_module.activity.start(source_sid, summary="stay here")
    queued = chat_mod.sess.enqueue_message(source_sid, "restore me")
    assert queued["ok"] is True
    monkeypatch.setattr(
        chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(
        chat_mod.sess, "link_runtime_successor", lambda *_args: False)
    monkeypatch.setattr(chat_mod, "sdk_delete_session", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="successor link changed"):
        chat_mod._commit_fork_lifecycle(
            source_sid,
            chat_mod.sess.get_session_meta(source_sid),
            fork_child=lambda: SimpleNamespace(session_id=child_sid),
            register_kwargs={
                "name": "rollback child",
                "model": source["model"],
                "cwd": source["cwd"],
            },
            successor=True,
            copy_runtime_overlays=True,
        )

    assert chat_mod.sess.get_session_meta(child_sid) is None
    assert chat_mod.sess.get_session_meta(source_sid)["runtime_shadow"] is False
    assert [row["text"] for row in chat_mod.sess.get_queue(source_sid)["items"]] == [
        "restore me"
    ]
    row = next(
        row for row in activity_module.activity.list()
        if row["session_id"] == source_sid
    )
    assert row["id"] == event["id"]


def test_fork_rejects_active_source_session(
    chat_mod,
    client,
    monkeypatch,
):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "busy source"},
    )
    assert source.status_code == 200, source.text
    sid = source.json()["id"]
    chat_mod._active_turns[sid] = SimpleNamespace(done=False)
    called = False

    def fake_fork(*_args, **_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            session_id="11111111-2222-4333-8444-555555555555",
        )

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={},
    )

    assert response.status_code == 409
    assert called is False


def test_fork_snapshots_source_while_background_task_keeps_running(
    chat_mod,
    client,
    monkeypatch,
):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "background source"},
    )
    assert source.status_code == 200, source.text
    sid = source.json()["id"]
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-a", "task-b"}
    new_sid = "11111111-2222-4333-8444-555555555555"
    called = False

    def fake_fork(*_args, **_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(session_id=new_sid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"title": "background snapshot"},
    )

    assert response.status_code == 200, response.text
    assert called is True
    assert response.json()["source_background_tasks_pending"] == 2
    assert chat_mod._sessions_with_inflight_tasks[sid] == {"task-a", "task-b"}


def test_continue_detached_forks_once_hides_source_and_migrates_queue(
    chat_mod, client, monkeypatch, tmp_path,
):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "background source"},
    ).json()
    sid = source["id"]
    boundary = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    child_sid = "11111111-2222-4333-8444-555555555555"
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    native_path = (
        Path.home() / ".claude" / "projects" / "-workspace" / f"{sid}.jsonl"
    )
    original_find = chat_mod._find_session_jsonl
    monkeypatch.setattr(
        chat_mod,
        "_find_session_jsonl",
        lambda requested: native_path if requested == sid else original_find(requested),
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "inherited-vendor-config")
    chat_mod.sess.set_runtime_background_boundary(sid, boundary)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-a"}
    chat_mod.sess.set_runtime_task_overlay(
        sid, "task-a", state="running", tool_use_id="tool-a",
        owner_session_id=sid,
    )
    queued = chat_mod.sess.enqueue_existing_message(
        sid, "continue now", permission="default")
    assert queued["ok"] is True
    fork_calls = []
    observed = {}

    def fake_fork(source_sid, **kwargs):
        observed["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR")
        fork_calls.append((source_sid, kwargs))
        return SimpleNamespace(session_id=child_sid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setattr(chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(chat_mod, "_backfill_runtime_task_overlays", lambda _sid: None)
    monkeypatch.setattr(chat_mod, "_schedule_queue_drain", lambda _sid: None)

    headers = {"X-Auth-Token": TEST_TOKEN}
    first = client.post(
        f"/api/chat/sessions/{sid}/continue-detached", headers=headers)
    second = client.post(
        f"/api/chat/sessions/{sid}/continue-detached", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["session_id"] == child_sid
    assert first.json()["source_session_id"] == sid
    assert first.json()["owner_session_id"] == sid
    assert first.json()["inherited_background_tasks_pending"] == 1
    assert first.json()["queue_migrated"] == 1
    assert second.json()["reused"] is True
    assert len(fork_calls) == 1
    assert fork_calls[0][1]["up_to_message_id"] == boundary
    assert observed["config_dir"] is None
    assert os.environ["CLAUDE_CONFIG_DIR"] == "inherited-vendor-config"
    assert chat_mod.sess.get_session_meta(sid)["runtime_shadow"] is True
    child_meta = chat_mod.sess.get_session_meta(child_sid)
    assert child_meta["runtime_predecessor"] == sid
    assert child_meta["runtime_fork_boundary_at"].endswith("Z")
    assert chat_mod.sess.normalize_runtime_fork_boundary_at(
        child_meta["runtime_fork_boundary_at"]
    ) == child_meta["runtime_fork_boundary_at"]
    listed = {row["id"] for row in chat_mod.sess.list_sessions()}
    assert sid not in listed
    assert child_sid in listed
    assert chat_mod.sess.get_queue(sid)["items"] == []
    assert [row["text"] for row in chat_mod.sess.get_queue(child_sid)["items"]] == [
        "continue now",
    ]
    overlay = chat_mod.sess.get_runtime_task_overlays(child_sid)["task-a"]
    assert overlay["owner_session_id"] == sid


def test_continue_detached_rejects_foreground_active(chat_mod, client, monkeypatch):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "busy source"},
    ).json()
    sid = source["id"]
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-a"}
    chat_mod._active_turns[sid] = SimpleNamespace(
        done=False, is_continuation=False)
    called = False

    def fake_fork(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    response = client.post(
        f"/api/chat/sessions/{sid}/continue-detached",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert response.status_code == 409
    assert called is False


def test_continue_detached_allows_canonical_done_during_postlude(
    chat_mod, client, monkeypatch,
):
    source = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "postlude source"},
    ).json()
    sid = source["id"]
    boundary = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    child_sid = "22222222-3333-4444-8555-666666666666"
    chat_mod.sess.set_runtime_background_boundary(sid, boundary)
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-a"}
    chat_mod._active_turns[sid] = SimpleNamespace(
        done=False,
        is_continuation=False,
        canonical_terminal_published=True,
    )

    def fake_fork(source_sid, **_kwargs):
        assert source_sid == sid
        return SimpleNamespace(session_id=child_sid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setattr(chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(chat_mod, "_backfill_runtime_task_overlays", lambda _sid: None)
    monkeypatch.setattr(chat_mod, "_schedule_queue_drain", lambda _sid: None)

    response = client.post(
        f"/api/chat/sessions/{sid}/continue-detached",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == child_sid
    assert chat_mod.sess.get_session_meta(sid)["runtime_successor"] == child_sid


@pytest.mark.asyncio
async def test_eager_prewarm_and_manual_continue_share_one_successor(
    chat_mod, monkeypatch,
):
    source_sid = "88888888-9999-4aaa-8bbb-cccccccccccc"
    child_sid = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    boundary = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    chat_mod.sess.register_session(
        source_sid,
        name="eager source",
        model="claude-sonnet-4-6",
        permission="default",
        effort="high",
        service_tier="",
    )
    chat_mod.sess.set_runtime_background_boundary(source_sid, boundary)
    chat_mod._sessions_with_inflight_tasks[source_sid] = {"task-a"}
    chat_mod._active_turns[source_sid] = SimpleNamespace(
        done=False,
        is_continuation=False,
        canonical_terminal_published=True,
    )

    fork_entered = threading.Event()
    allow_fork = threading.Event()
    fork_calls = []
    warmed = []

    def fake_fork(sid, **_kwargs):
        fork_calls.append(sid)
        fork_entered.set()
        assert allow_fork.wait(timeout=2)
        return SimpleNamespace(session_id=child_sid)

    async def fake_get_client(sid, model, permission, **kwargs):
        warmed.append((sid, model, permission, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(
        chat_mod, "_backfill_runtime_task_overlays", lambda _sid: None)
    monkeypatch.setattr(chat_mod, "_schedule_queue_drain", lambda _sid: None)

    chat_mod._schedule_detached_successor_prewarm(source_sid)
    eager = chat_mod._runtime_prewarm_tasks[source_sid]
    assert await asyncio.to_thread(fork_entered.wait, 1)
    manual = asyncio.create_task(
        chat_mod._continue_detached_runtime(source_sid))
    await asyncio.sleep(0.02)

    assert fork_calls == [source_sid]
    assert not manual.done()
    allow_fork.set()
    manual_result = await manual
    await eager
    await asyncio.sleep(0)

    assert manual_result["session_id"] == child_sid
    assert manual_result["reused"] is True
    assert fork_calls == [source_sid]
    assert warmed == [(
        child_sid,
        "claude-sonnet-4-6",
        "default",
        {"effort": "high", "service_tier": ""},
    )]
    assert source_sid not in chat_mod._runtime_prewarm_tasks
    assert (
        chat_mod.sess.get_session_meta(source_sid)["runtime_successor"]
        == child_sid
    )


@pytest.mark.asyncio
async def test_delete_fences_and_rolls_back_inflight_eager_successor(
    chat_mod, monkeypatch,
):
    source_sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
    child_sid = "bbbbbbbb-cccc-4ddd-8eee-fffffffffff2"
    chat_mod.sess.register_session(
        source_sid,
        name="delete while eager",
        model="claude-sonnet-4-6",
        permission="default",
    )
    chat_mod.sess.set_runtime_background_boundary(
        source_sid, "cccccccc-dddd-4eee-8fff-aaaaaaaaaaa3")
    chat_mod._sessions_with_inflight_tasks[source_sid] = {"task-a"}
    active = chat_mod.TurnBroadcast(source_sid)
    active.canonical_terminal_published = True
    chat_mod._active_turns[source_sid] = active

    fork_entered = threading.Event()
    allow_fork = threading.Event()
    sdk_deletes = []

    def fake_fork(_sid, **_kwargs):
        fork_entered.set()
        assert allow_fork.wait(timeout=2)
        return SimpleNamespace(session_id=child_sid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setattr(
        chat_mod, "sdk_delete_session",
        lambda sid, **_kwargs: sdk_deletes.append(sid),
    )
    monkeypatch.setattr(chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(
        chat_mod, "_backfill_runtime_task_overlays", lambda _sid: None)
    monkeypatch.setattr(chat_mod, "_schedule_queue_drain", lambda _sid: None)

    chat_mod._schedule_detached_successor_prewarm(source_sid)
    assert await asyncio.to_thread(fork_entered.wait, 1)
    deleting = asyncio.create_task(chat_mod.delete_session_api(source_sid))
    for _ in range(100):
        if chat_mod.sess.session_is_deleting(source_sid):
            break
        await asyncio.sleep(0.01)
    assert chat_mod.sess.session_is_deleting(source_sid) is True

    allow_fork.set()
    result = await asyncio.wait_for(deleting, timeout=5)
    await asyncio.sleep(0)

    assert result == {"ok": True}
    assert chat_mod.sess.get_session_meta(source_sid) is None
    assert chat_mod.sess.get_session_meta(child_sid) is None
    assert source_sid not in chat_mod._runtime_prewarm_tasks
    assert not any(key[0] == child_sid for key in chat_mod._clients)
    assert child_sid in sdk_deletes
    assert source_sid in sdk_deletes


@pytest.mark.asyncio
async def test_cancelled_continue_detached_keeps_lock_until_owner_commits(
    chat_mod, monkeypatch,
):
    source_sid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    child_sid = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    chat_mod.sess.register_session(source_sid, name="cancel source")
    chat_mod.sess.set_runtime_background_boundary(
        source_sid, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    chat_mod._sessions_with_inflight_tasks[source_sid] = {"task-a"}
    queued = chat_mod.sess.enqueue_existing_message(
        source_sid, "preserve me", permission="default")
    assert queued["ok"] is True

    fork_entered = threading.Event()
    allow_fork = threading.Event()
    fork_calls = []

    def fake_fork(sid, **_kwargs):
        fork_calls.append(sid)
        fork_entered.set()
        assert allow_fork.wait(timeout=2)
        return SimpleNamespace(session_id=child_sid)

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setattr(chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {})
    monkeypatch.setattr(chat_mod, "_backfill_runtime_task_overlays", lambda _sid: None)
    monkeypatch.setattr(chat_mod, "_schedule_queue_drain", lambda _sid: None)

    first = asyncio.create_task(
        chat_mod._continue_detached_runtime(source_sid))
    assert await asyncio.to_thread(fork_entered.wait, 1)
    first.cancel()
    await asyncio.sleep(0)
    retry = asyncio.create_task(
        chat_mod._continue_detached_runtime(source_sid))
    await asyncio.sleep(0.02)

    assert not first.done()
    assert not retry.done()
    assert fork_calls == [source_sid]

    allow_fork.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await retry

    assert result["session_id"] == child_sid
    assert result["reused"] is True
    assert fork_calls == [source_sid]
    assert chat_mod.sess.get_queue(source_sid)["items"] == []
    assert [
        item["text"]
        for item in chat_mod.sess.get_queue(child_sid)["items"]
    ] == ["preserve me"]


def test_runtime_postlude_syncs_annotations_and_auto_name_through_chain(
    chat_mod, monkeypatch,
):
    source_sid = "33333333-4444-4555-8666-777777777777"
    child_sid = "44444444-5555-4666-8777-888888888888"
    grandchild_sid = "55555555-6666-4777-8888-999999999999"
    inherited_name = "New session"
    chat_mod.sess.register_session(
        source_sid, name=inherited_name, auto_named=True)
    chat_mod.sess.register_session(
        child_sid,
        name=inherited_name,
        auto_named=False,
        forked_from=source_sid,
        forked_from_name=inherited_name,
        runtime_predecessor=source_sid,
    )
    chat_mod.sess.register_session(
        grandchild_sid,
        name=inherited_name,
        auto_named=False,
        forked_from=child_sid,
        forked_from_name=inherited_name,
        runtime_predecessor=child_sid,
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
    assert chat_mod.sess.link_runtime_successor(child_sid, grandchild_sid)

    source_uuid = "aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb"
    child_uuid = "bbbbbbbb-2222-4333-8444-cccccccccccc"
    grandchild_uuid = "cccccccc-3333-4444-8555-dddddddddddd"
    chat_mod.sess.set_message_annotation(
        source_sid,
        source_uuid,
        turn_status="completed",
        memory_recall={"count": 2},
    )
    mappings = {
        child_sid: {source_uuid: child_uuid},
        grandchild_sid: {child_uuid: grandchild_uuid},
    }
    monkeypatch.setattr(
        chat_mod,
        "_runtime_fork_uuid_mapping",
        lambda sid: mappings[sid],
    )
    rename_calls = []
    monkeypatch.setattr(
        chat_mod,
        "sdk_rename_session",
        lambda sid, name, **_kwargs: rename_calls.append((sid, name)),
    )

    chat_mod.sess.bump_session(
        source_sid, auto_rename_from="late automatic title")
    final_name = chat_mod.sess.get_session_meta(source_sid)["name"]
    result = chat_mod._sync_runtime_successor_postlude(source_sid)

    assert result == {"annotations": 2, "renamed": 2}
    assert chat_mod.sess.get_message_annotations(child_sid)[child_uuid] == {
        "turn_status": "completed",
        "memory_recall": {"count": 2},
    }
    assert chat_mod.sess.get_message_annotations(
        grandchild_sid)[grandchild_uuid] == {
            "turn_status": "completed",
            "memory_recall": {"count": 2},
        }
    assert chat_mod.sess.get_session_meta(child_sid)["name"] == final_name
    assert chat_mod.sess.get_session_meta(grandchild_sid)["name"] == final_name
    assert rename_calls == [
        (child_sid, final_name),
        (grandchild_sid, final_name),
    ]


def test_runtime_postlude_preserves_explicit_successor_name(
    chat_mod, monkeypatch,
):
    source = chat_mod.sess.create_session("automatic source")
    child = chat_mod.sess.create_session("inherited title")
    assert chat_mod.sess.link_runtime_successor(source["id"], child["id"])
    # Simulate a user rename after the successor became visible.
    assert chat_mod.sess.rename_session(child["id"], "my explicit title")
    rename_calls = []
    monkeypatch.setattr(
        chat_mod, "sdk_rename_session",
        lambda *_args, **_kwargs: rename_calls.append(True),
    )
    monkeypatch.setattr(
        chat_mod, "_runtime_fork_uuid_mapping", lambda _sid: {},
    )

    result = chat_mod._sync_runtime_successor_postlude(source["id"])

    assert result == {"annotations": 0, "renamed": 0}
    assert chat_mod.sess.get_session_meta(child["id"])["name"] == (
        "my explicit title")
    assert rename_calls == []


def test_runtime_task_terminal_overlay_reaches_every_successor(chat_mod):
    source = chat_mod.sess.create_session("source")
    child = chat_mod.sess.create_session("child")
    grandchild = chat_mod.sess.create_session("grandchild")
    assert chat_mod.sess.link_runtime_successor(source["id"], child["id"])
    assert chat_mod.sess.link_runtime_successor(child["id"], grandchild["id"])
    chat_mod._pin_background_task(source["id"], "task-a")
    chat_mod._record_background_task_launch(
        source["id"], "task-a", tool_use_id="tool-a", description="sleep")

    # A resumed successor can receive a synthetic orphan notification for the
    # predecessor-owned task. It is not lifecycle truth: it must neither
    # release the source pin nor poison any persisted/UI state.
    assert chat_mod._on_task_settled(
        child["id"], "task-a", status="stopped",
        summary="No completion record was found in the previous session.",
    ) is None
    assert "task-a" in chat_mod._sessions_with_inflight_tasks[source["id"]]
    for sid in (source["id"], child["id"], grandchild["id"]):
        overlay = chat_mod.sess.get_runtime_task_overlays(sid)["task-a"]
        assert overlay["state"] == "running"
        assert overlay["owner_session_id"] == source["id"]
        assert "summary" not in overlay

    assert chat_mod._on_task_settled(
        source["id"], "task-a", status="completed",
        tool_use_id="tool-a", summary="done", output_file="/tmp/a.output",
    ) is True
    # A second, sparse terminal patch is a common SDK shape. It may update the
    # state but must not erase richer typed-notification metadata.
    assert chat_mod._on_task_settled(
        source["id"], "task-a", status="completed",
    ) is False

    # Even after the source pin is gone, durable owner authority and terminal
    # monotonicity reject the successor's late synthetic stop.
    assert chat_mod._on_task_settled(
        child["id"], "task-a", status="stopped", summary="synthetic stop",
    ) is None

    for sid in (source["id"], child["id"], grandchild["id"]):
        overlay = chat_mod.sess.get_runtime_task_overlays(sid)["task-a"]
        assert overlay["state"] == "completed"
        assert overlay["owner_session_id"] == source["id"]
        assert overlay["tool_use_id"] == "tool-a"
        assert overlay["summary"] == "done"
        assert overlay["output_file"] == "/tmp/a.output"
    assert chat_mod._record_background_task_launch(
        source["id"], "task-a", tool_use_id="tool-a",
    ) is False
    assert source["id"] not in chat_mod._sessions_with_inflight_tasks


def test_native_compact_rejects_success_without_token_drop(chat_mod, client, monkeypatch):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid,
    )
    fake = _FakeCompactClient(result, totals=(190_000, 190_000))

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 500, r.text
    assert "did not decrease" in r.json()["detail"]


def test_compact_recovery_publishes_activity_successor_only_after_registration(
    chat_mod,
    client,
    monkeypatch,
    tmp_path,
):
    from backend import activity as activity_module

    sid = _make_compact_session(client)
    child_sid = "33333333-4444-4555-8666-777777777777"
    source_path = tmp_path / f"{sid}.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    child_path = tmp_path / f"{child_sid}.jsonl"
    child_path.write_text("{}\n", encoding="utf-8")
    stats = SimpleNamespace(
        included_messages=1,
        omitted_messages=0,
        truncated_messages=0,
        estimated_post_tokens=123,
    )
    monkeypatch.setattr(chat_mod, "_find_session_jsonl", lambda _sid: source_path)
    monkeypatch.setattr(
        chat_mod.context_recovery,
        "create_recovery_fork",
        lambda *_args, **_kwargs: SimpleNamespace(
            session_id=child_sid,
            path=child_path,
            stats=stats,
        ),
    )
    calls = []

    def inherit(source_sid, recovered_sid, **kwargs):
        assert chat_mod.sess.get_session_meta(recovered_sid) is not None
        calls.append((source_sid, recovered_sid, kwargs))

    monkeypatch.setattr(activity_module.activity, "inherit_session", inherit)

    result = chat_mod._create_context_recovery_session(
        sid,
        "claude-sonnet-4-6",
        pre_tokens=456,
        context_limit=200_000,
    )

    assert result["session"]["id"] == child_sid
    assert calls == [(sid, child_sid, {"successor": True})]


def test_compact_recovery_retry_reuses_linked_child_and_repairs_projections(
    chat_mod,
    client,
    monkeypatch,
):
    from backend import activity as activity_module

    source_sid = _make_compact_session(client)
    source_meta = chat_mod.sess.get_session_meta(source_sid)
    child_sid = "34343434-4545-4789-89ab-909090909090"
    chat_mod.sess.register_session(
        child_sid,
        name="existing compact child",
        model=source_meta["model"],
        runtime_predecessor=source_sid,
        cwd=source_meta["cwd"],
    )
    assert chat_mod.sess.link_runtime_successor(source_sid, child_sid)
    monkeypatch.setattr(chat_mod, "_find_session_jsonl", lambda _sid: None)
    monkeypatch.setattr(
        chat_mod.context_recovery,
        "create_recovery_fork",
        lambda *_args, **_kwargs: pytest.fail("retry must reuse linked child"),
    )
    old_uuid = "cccccccc-dddd-4eee-8fff-111111111111"
    new_uuid = "dddddddd-eeee-4fff-8111-222222222222"
    chat_mod.sess.set_message_annotation(source_sid, old_uuid, cost=1.25)
    chat_mod.sess.enqueue_message(source_sid, "compact retry queue")
    event = activity_module.activity.start(source_sid, summary="compact retry")
    monkeypatch.setattr(
        chat_mod,
        "_runtime_fork_uuid_mapping",
        lambda sid: {old_uuid: new_uuid} if sid == child_sid else {},
    )

    result = chat_mod._create_context_recovery_session(
        source_sid,
        source_meta["model"],
        pre_tokens=456,
        context_limit=200_000,
    )

    assert result["session"]["id"] == child_sid
    assert result["stats"]["included_messages"] == 0
    assert chat_mod.sess.get_message_annotations(child_sid)[new_uuid]["cost"] == 1.25
    assert [row["text"] for row in chat_mod.sess.get_queue(child_sid)["items"]] == [
        "compact retry queue"
    ]
    activity_row = next(
        row for row in activity_module.activity.list()
        if row["session_id"] == child_sid
    )
    assert activity_row["id"] == event["id"]


def test_native_codex_compact_recovers_verified_no_shrink(
    chat_mod, client, monkeypatch,
):
    sid = _make_compact_session(client)
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid,
    )
    fake = _FakeCompactClient(result, totals=(364_270, 364_270))
    recovered_id = "004208ac-a47b-4b75-999b-d7b6b0e62aa0"
    calls = []

    async def fake_get_client(*_args, **_kwargs):
        return fake

    async def fake_recover(target_sid, model, *, pre_tokens, context_limit):
        calls.append((target_sid, model, pre_tokens, context_limit))
        return {
            "session": {
                "id": recovered_id,
                "session_id": recovered_id,
                "name": "compact endpoint · recovery",
                "model": model,
            },
            "stats": {"estimated_post_tokens": 24_000},
        }

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_recover_context_session", fake_recover)

    response = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["recovered"] is True
    assert body["recovered_session"]["id"] == recovered_id
    assert body["recovery_stats"]["estimated_post_tokens"] == 24_000
    assert fake.queries == ["/compact"]
    assert calls == [(
        sid, "codex:gpt-5.6-sol", 364_270, 200_000,
    )]


def test_native_codex_compact_recovers_generic_context_exception(
    chat_mod, client, monkeypatch,
):
    """A transport-level context rejection uses the same safe recovery path."""
    sid = _make_compact_session(client)
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")

    class GenericContextClient(_FakeCompactClient):
        async def query(self, prompt):
            self.queries.append(prompt)
            raise RuntimeError(
                "API Error: 400 Your input exceeds the context window of this model"
            )

    fake = GenericContextClient(result=None, totals=(364_270,))
    recovered_id = "f54e160e-2368-46f2-8d76-00499625f9de"
    calls = []

    async def fake_get_client(*_args, **_kwargs):
        return fake

    async def fake_recover(target_sid, model, *, pre_tokens, context_limit):
        calls.append((target_sid, model, pre_tokens, context_limit))
        return {
            "session": {
                "id": recovered_id,
                "session_id": recovered_id,
                "name": "compact endpoint · recovery",
                "model": model,
            },
            "stats": {"estimated_post_tokens": 24_000},
        }

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(chat_mod, "_recover_context_session", fake_recover)
    monkeypatch.setitem(chat_mod._session_usage, sid, {
        "context_used": 364_270,
        "context_limit": 353_400,
    })

    response = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["recovered"] is True
    assert body["recovered_session"]["id"] == recovered_id
    assert fake.queries == ["/compact"]
    assert calls == [(
        sid, "codex:gpt-5.6-sol", 364_270, 353_400,
    )]


def test_native_compact_total_timeout_covers_post_command_verification(
    chat_mod,
    client,
    monkeypatch,
):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid,
    )

    class SlowVerifyClient(_FakeCompactClient):
        async def get_context_usage(self):
            try:
                return {"totalTokens": next(self.totals), "maxTokens": 200_000}
            except StopIteration:
                await asyncio.Event().wait()

    fake = SlowVerifyClient(result, totals=(190_000,))

    async def fake_get_client(*_args, **_kwargs):
        return fake

    original_env_int = chat_mod.env_int
    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    disconnected = []

    async def fake_disconnect(target_sid):
        assert chat_mod._session_runtime_lock_for(target_sid).locked()
        chat_mod._pending_runtime_rebuilds.discard(target_sid)
        disconnected.append(target_sid)

    monkeypatch.setattr(chat_mod, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(
        chat_mod,
        "env_int",
        lambda name, default, **kwargs: (
            0.02 if name == "MUSELAB_COMPACT_TIMEOUT_S"
            else original_env_int(name, default, **kwargs)
        ),
    )

    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 504, r.text
    assert "timed out" in r.json()["detail"]
    assert disconnected == [sid]


@pytest.mark.asyncio
async def test_native_compact_lock_wait_timeout_does_not_kill_holder(
    chat_mod,
    client,
    monkeypatch,
):
    sid = _make_compact_session(client)
    disconnected = []

    async def fake_disconnect(target_sid):
        disconnected.append(target_sid)

    original_env_int = chat_mod.env_int
    monkeypatch.setattr(chat_mod, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(
        chat_mod,
        "env_int",
        lambda name, default, **kwargs: (
            0.02 if name == "MUSELAB_COMPACT_TIMEOUT_S"
            else original_env_int(name, default, **kwargs)
        ),
    )

    lock = chat_mod._session_runtime_lock_for(sid)
    async with lock:
        with pytest.raises(chat_mod.HTTPException) as exc_info:
            await chat_mod.native_compact_session_api(sid)

    assert exc_info.value.status_code == 504
    assert disconnected == []


@pytest.mark.asyncio
async def test_native_compact_outer_timeout_cleans_before_unlock(
    chat_mod,
    client,
    monkeypatch,
):
    sid = _make_compact_session(client)
    disconnected = []
    cleanup_finished = asyncio.Event()

    async def stalled_compact(_sid):
        await asyncio.Event().wait()

    async def fake_disconnect(target_sid):
        assert chat_mod._session_runtime_lock_for(target_sid).locked()
        await asyncio.sleep(0.01)
        chat_mod._pending_runtime_rebuilds.discard(target_sid)
        disconnected.append(target_sid)
        cleanup_finished.set()

    original_env_int = chat_mod.env_int
    monkeypatch.setattr(chat_mod, "_native_compact_session_locked", stalled_compact)
    monkeypatch.setattr(chat_mod, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(
        chat_mod,
        "env_int",
        lambda name, default, **kwargs: (
            0.02 if name == "MUSELAB_COMPACT_TIMEOUT_S"
            else original_env_int(name, default, **kwargs)
        ),
    )

    with pytest.raises(chat_mod.HTTPException) as exc_info:
        await chat_mod.native_compact_session_api(sid)

    assert exc_info.value.status_code == 504
    assert cleanup_finished.is_set()
    assert disconnected == [sid]
    assert not chat_mod._session_runtime_lock_for(sid).locked()


def test_native_compact_returns_after_verification_and_schedules_recount(
    chat_mod,
    client,
    monkeypatch,
):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid,
    )
    fake = _FakeCompactClient(result, totals=(190_000, 50_000))
    scheduled = []

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    monkeypatch.setattr(
        chat_mod,
        "_schedule_post_compact_refresh",
        lambda compact_sid, model, usage: scheduled.append(
            (compact_sid, model, usage)),
    )

    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 200, r.text
    assert scheduled
    assert scheduled[0][0] == sid
    assert scheduled[0][2]["totalTokens"] == 50_000


@pytest.mark.parametrize("owner_state", ["absent", "pre_cancelled", "hung"])
@pytest.mark.asyncio
async def test_force_stop_finalizes_attachments_for_every_owner_state(
    chat_mod,
    monkeypatch,
    owner_state,
):
    sid = f"force-attachment-{owner_state}"
    aid = f"force-aid-{owner_state}"
    entry = {
        "kind": "text",
        "mime": "text/plain",
        "name": "force.txt",
        "raw": b"force",
        "text": "force",
        "ts": chat_mod.time.time(),
    }
    with chat_mod._image_store_lock:
        chat_mod._image_store[aid] = entry
    lease, _missing, _busy = chat_mod._lease_staged_attachments(
        aid, require_all=True)
    broadcast = chat_mod.TurnBroadcast(sid)
    broadcast._attachment_lease = lease
    artifact = (
        chat_mod._attachments_base() / sid / f"{aid}-force.txt")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"artifact")
    broadcast._prepared_attachments = (
        chat_mod._PreparedStagedAttachments(
            artifact_paths=[str(artifact)])
    )
    owner_release = asyncio.Event()
    owner = None

    async def ignore_cancel_until_released():
        while not owner_release.is_set():
            try:
                await owner_release.wait()
            except asyncio.CancelledError:
                continue

    if owner_state == "pre_cancelled":
        owner = asyncio.create_task(asyncio.Event().wait())
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        broadcast.task = owner
    elif owner_state == "hung":
        owner = asyncio.create_task(ignore_cancel_until_released())
        broadcast.task = owner
        await asyncio.sleep(0)

    disconnect_release = asyncio.Event()

    async def stuck_disconnect(_sid):
        await disconnect_release.wait()

    monkeypatch.setattr(chat_mod, "disconnect_client", stuck_disconnect)
    monkeypatch.setattr(
        chat_mod, "_INTERRUPT_FORCE_OWNER_JOIN_S", 0.01)
    monkeypatch.setattr(
        chat_mod, "_INTERRUPT_FORCE_DISCONNECT_JOIN_S", 0.01)
    monkeypatch.setattr(
        chat_mod, "_persist_cancelled_turn_snapshot",
        lambda _broadcast: True,
    )
    chat_mod._active_turns[sid] = broadcast
    try:
        await chat_mod._force_stop_after_grace(
            sid, broadcast, grace=0.001)
        assert sid not in chat_mod._active_turns
        assert broadcast.done is True
        assert not artifact.exists()
        assert chat_mod._image_store.get(aid) is entry
        assert aid not in chat_mod._staged_attachment_claims
        assert lease.state == "released"
    finally:
        owner_release.set()
        disconnect_release.set()
        if owner is not None:
            await asyncio.gather(owner, return_exceptions=True)
        for _ in range(100):
            if not chat_mod._maintenance_tasks:
                break
            await asyncio.sleep(0.01)
        chat_mod._active_turns.pop(sid, None)
        with chat_mod._image_store_lock:
            chat_mod._image_store.pop(aid, None)
            chat_mod._staged_attachment_claims.pop(aid, None)
