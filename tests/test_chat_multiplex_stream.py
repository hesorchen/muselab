import asyncio
import json

import pytest
from fastapi import HTTPException

from tests.conftest import TEST_TOKEN


@pytest.fixture()
def chat_mod(app_module):
    from backend import chat
    return chat


def _active_state(broadcast, *, attachable=True, background=False):
    return {
        "active": True,
        "attachable": attachable,
        "background": background,
        "continuation": bool(getattr(broadcast, "is_continuation", False)),
        "turn_id": broadcast.turn_id if attachable else "watcher-origin",
        "started_at": broadcast.started_at,
        "events_so_far": broadcast.replay_count(),
        "background_tasks_pending": 1 if background else 0,
        "runtime_background_tasks_pending": 0,
        "runtime_continuation_pending": 0,
        "runtime_ui_revision": 0,
        "activity_source": "background" if background else "direct",
        "user_text": broadcast.user_text,
        "user_images": [],
        "user_docs": [],
    }


def test_turn_start_admits_accepts_and_launches(
        chat_mod, client, monkeypatch):
    broadcast = chat_mod.TurnBroadcast("turn-start-session", model="model-a")
    launched = []

    async def admit(session_id, prompt, *, model="", image_ids="", **_kwargs):
        assert session_id == "turn-start-session"
        assert prompt == "hello"
        assert model == "model-a"
        assert image_ids == ""
        chat_mod._active_turns[session_id] = broadcast
        return broadcast

    def launch(bc, **kwargs):
        launched.append((bc, kwargs))
        return None

    monkeypatch.setattr(chat_mod, "_admit_turn", admit)
    monkeypatch.setattr(chat_mod, "_launch_admitted_turn", launch)

    response = client.post(
        "/api/chat/turns/start",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={
            "prompt": "hello",
            "session_id": "turn-start-session",
            "model": "model-a",
            "permission": "bypassPermissions",
            "image_ids": "",
            "mobile": False,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "accepted": True,
        "session_id": broadcast.session_id,
        "turn_id": broadcast.turn_id,
        "started_at": broadcast.started_at,
    }
    assert launched == [(broadcast, {
        "prompt": "hello",
        "model": "model-a",
        "permission": "bypassPermissions",
        "image_ids": "",
    })]
    assert broadcast.startup_phase == "accepted"


def test_turn_start_is_new_turn_only(chat_mod, client):
    unauthenticated = client.post(
        "/api/chat/turns/start",
        json={"prompt": "hello", "session_id": "no-auth"},
    )
    assert unauthenticated.status_code == 401

    response = client.post(
        "/api/chat/turns/start",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={"prompt": "", "session_id": "no-reconnect"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "prompt or image_ids required for a new turn")


def test_mux_ticket_validation_dedupes_and_is_single_use(
        chat_mod, client):
    unauthenticated = client.post(
        "/api/chat/stream/mux/start", json={"checkpoints": []})
    assert unauthenticated.status_code == 401

    missing_turn = client.post(
        "/api/chat/stream/mux/start",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={
            "checkpoints": [{
                "session_id": "s1",
                "turn_id": "",
                "last_event_seq": 1,
            }],
            "mobile": False,
        },
    )
    assert missing_turn.status_code == 422

    contradictory = client.post(
        "/api/chat/stream/mux/start",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={
            "checkpoints": [
                {"session_id": "s1", "turn_id": "t1", "last_event_seq": 1},
                {"session_id": "s1", "turn_id": "t1", "last_event_seq": 2},
            ],
        },
    )
    assert contradictory.status_code == 422

    accepted = client.post(
        "/api/chat/stream/mux/start",
        headers={"X-Auth-Token": TEST_TOKEN},
        json={
            "checkpoints": [
                {"session_id": "s1", "turn_id": "t1", "last_event_seq": 1},
                {"session_id": "s1", "turn_id": "t1", "last_event_seq": 1},
            ],
        },
    )
    assert accepted.status_code == 200, accepted.text
    ticket = accepted.json()["ticket"]

    response = asyncio.run(chat_mod.mux_stream(ticket))
    assert response.status_code == 200
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(chat_mod.mux_stream(ticket))
    assert exc_info.value.status_code == 401


def test_mux_auto_discovers_wraps_events_and_disconnect_only_unsubscribes(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    broadcast = chat_mod.TurnBroadcast("auto-discovered")
    broadcast.publish({"event": "text", "data": json.dumps({"text": "hello"})})
    chat_mod._active_turns[broadcast.session_id] = broadcast
    monkeypatch.setattr(
        chat_mod,
        "session_active_status",
        lambda sid: _active_state(chat_mod._active_turns[sid]),
    )

    async def exercise():
        owner = asyncio.create_task(asyncio.sleep(10))
        broadcast.startup_owner_task = owner
        stream = chat_mod._subscribe_multiplex({}, mobile=False)
        first = await anext(stream)
        second = await anext(stream)

        assert first["event"] == "session_state"
        assert json.loads(first["data"])["session_id"] == broadcast.session_id
        assert second["event"] == "text"
        assert json.loads(second["data"]) == {
            "text": "hello",
            "turn_id": broadcast.turn_id,
            "event_seq": 1,
            "session_id": broadcast.session_id,
        }
        assert len(broadcast.subscribers) == 1

        await stream.aclose()
        assert not broadcast.subscribers
        assert not owner.done()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())


def test_mux_watcher_state_can_become_attachable_dynamically(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    sid = "watcher-to-turn"
    placeholder = chat_mod.TurnBroadcast(sid)
    phase = {"attachable": False}
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}

    def state(_sid):
        if not phase["attachable"]:
            return _active_state(
                placeholder, attachable=False, background=True)
        return _active_state(chat_mod._active_turns[sid])

    monkeypatch.setattr(chat_mod, "session_active_status", state)

    async def exercise():
        stream = chat_mod._subscribe_multiplex({}, mobile=False)
        watcher_state = await anext(stream)
        watcher_payload = json.loads(watcher_state["data"])
        assert watcher_state["event"] == "session_state"
        assert watcher_payload["active"] is True
        assert watcher_payload["attachable"] is False
        assert watcher_payload["session_id"] == sid

        broadcast = chat_mod.TurnBroadcast(sid)
        broadcast.publish({
            "event": "tool_result", "data": json.dumps({"id": "r1"})})
        chat_mod._active_turns[sid] = broadcast
        phase["attachable"] = True

        attachable_state = await asyncio.wait_for(anext(stream), timeout=0.2)
        wrapped_event = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert attachable_state["event"] == "session_state"
        assert json.loads(attachable_state["data"])["turn_id"] == broadcast.turn_id
        assert wrapped_event["event"] == "tool_result"
        assert json.loads(wrapped_event["data"])["session_id"] == sid
        await stream.aclose()

    asyncio.run(exercise())
    placeholder.close()


def test_mux_emits_inactive_state_with_finished_turn_identity(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    broadcast = chat_mod.TurnBroadcast("becomes-inactive")
    phase = {"active": True}

    def state(_sid):
        if phase["active"]:
            return _active_state(broadcast)
        return {"active": False, "background_tasks_pending": 0}

    chat_mod._active_turns[broadcast.session_id] = broadcast
    monkeypatch.setattr(chat_mod, "session_active_status", state)

    async def exercise():
        stream = chat_mod._subscribe_multiplex({})
        active = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert json.loads(active["data"])["active"] is True
        broadcast.publish({
            "event": "done",
            "data": json.dumps({"turn_id": broadcast.turn_id}),
        })
        broadcast.finish()
        phase["active"] = False
        chat_mod._active_turns.pop(broadcast.session_id, None)
        terminal = await asyncio.wait_for(anext(stream), timeout=0.2)
        assert terminal["event"] == "done"
        inactive = await asyncio.wait_for(anext(stream), timeout=0.2)
        payload = json.loads(inactive["data"])
        assert inactive["event"] == "session_state"
        assert payload["active"] is False
        assert payload["turn_id"] == broadcast.turn_id
        await stream.aclose()

    asyncio.run(exercise())
    broadcast.close()


def test_mux_exact_recent_checkpoint_replays_without_cold_recent_discovery(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    exact = chat_mod.TurnBroadcast("recent-exact")
    exact.publish({"event": "text", "data": json.dumps({"text": "A"})})
    exact.publish({"event": "text", "data": json.dumps({"text": "B"})})
    exact.finish()
    ordinary = chat_mod.TurnBroadcast("recent-ordinary")
    ordinary.publish({"event": "text", "data": json.dumps({"text": "do not replay"})})
    ordinary.finish()
    chat_mod._recent_turns[exact.session_id] = exact
    chat_mod._recent_turns[ordinary.session_id] = ordinary
    monkeypatch.setattr(
        chat_mod,
        "session_active_status",
        lambda _sid: {"active": False},
    )

    async def exercise():
        stream = chat_mod._subscribe_multiplex({
            exact.session_id: {
                "session_id": exact.session_id,
                "turn_id": exact.turn_id,
                "last_event_seq": 1,
            },
        })
        replayed = await asyncio.wait_for(anext(stream), timeout=0.2)
        payload = json.loads(replayed["data"])
        assert replayed["event"] == "text"
        assert payload["text"] == "B"
        assert payload["session_id"] == exact.session_id
        await stream.aclose()

    asyncio.run(exercise())


def test_mux_turn_mismatch_resyncs_and_still_attaches_current_turn(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    broadcast = chat_mod.TurnBroadcast("changed-session")
    broadcast.publish({
        "event": "text", "data": json.dumps({"text": "new turn"})})
    chat_mod._active_turns[broadcast.session_id] = broadcast
    monkeypatch.setattr(
        chat_mod,
        "session_active_status",
        lambda sid: _active_state(chat_mod._active_turns[sid]),
    )

    async def exercise():
        stream = chat_mod._subscribe_multiplex({
            broadcast.session_id: {
                "session_id": broadcast.session_id,
                "turn_id": "stale-turn",
                "last_event_seq": 3,
            },
        })
        seen = [
            await asyncio.wait_for(anext(stream), timeout=0.2)
            for _ in range(3)
        ]
        decoded = [(event["event"], json.loads(event["data"])) for event in seen]
        assert any(
            name == "resync"
            and payload["reason"] == "turn_changed"
            and payload["current_turn_id"] == broadcast.turn_id
            for name, payload in decoded
        )
        assert any(
            name == "text" and payload["text"] == "new turn"
            for name, payload in decoded
        )
        await stream.aclose()

    asyncio.run(exercise())


def test_mux_child_resync_does_not_close_other_sessions(
        chat_mod, monkeypatch):
    monkeypatch.setattr(chat_mod, "_MUX_RECONCILE_INTERVAL_S", 0.01)
    gap = chat_mod.TurnBroadcast(
        "gap-session", replay_max_events=2, replay_max_bytes=1024 * 1024)
    for index in range(4):
        gap.publish({"event": "tool_result", "data": json.dumps({"id": index})})
    good = chat_mod.TurnBroadcast("good-session")
    good.publish({"event": "text", "data": json.dumps({"text": "still live"})})
    chat_mod._active_turns[gap.session_id] = gap
    chat_mod._active_turns[good.session_id] = good
    monkeypatch.setattr(
        chat_mod,
        "session_active_status",
        lambda sid: _active_state(chat_mod._active_turns[sid]),
    )

    async def exercise():
        stream = chat_mod._subscribe_multiplex({
            gap.session_id: {
                "session_id": gap.session_id,
                "turn_id": gap.turn_id,
                "last_event_seq": 1,
            },
        })
        seen = []
        while len(seen) < 4:
            seen.append(await asyncio.wait_for(anext(stream), timeout=0.2))

        decoded = [(event["event"], json.loads(event["data"])) for event in seen]
        assert any(
            name == "resync"
            and payload["reason"] == "replay_gap"
            and payload["session_id"] == gap.session_id
            for name, payload in decoded
        )
        assert any(
            name == "text"
            and payload["text"] == "still live"
            and payload["session_id"] == good.session_id
            for name, payload in decoded
        )
        await stream.aclose()

    asyncio.run(exercise())
