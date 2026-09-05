"""Terminal cleanup waits must not turn an idle send into visible queue churn."""
import asyncio
import pytest


@pytest.fixture
def admission_runtime(app_module, monkeypatch):
    from backend import chat
    sid = "terminal-admission-fixture"
    monkeypatch.setattr(chat, "_lock", asyncio.Lock())
    monkeypatch.setattr(chat, "_active_turns", {})
    monkeypatch.setattr(chat, "_sessions_with_inflight_tasks", {})
    monkeypatch.setattr(chat, "_session_has_live_watcher", lambda _sid: False)
    monkeypatch.setattr(chat, "_session_has_scheduled_delivery", lambda _sid: False)
    monkeypatch.setattr(chat, "_write_active_turn_sidecar", lambda *a, **kw: True)
    monkeypatch.setattr(chat, "_announce_mux_turn", lambda b: None)
    monkeypatch.setattr(chat.sess, "get_queue", lambda _sid: {"items": [], "inflight": None})

    async def handoff(_sid):
        return None

    monkeypatch.setattr(chat, "_handoff_task_watcher", handoff)
    yield chat, sid
    for broadcast in list(chat._active_turns.values()):
        broadcast.close()


@pytest.mark.asyncio
async def test_direct_send_waits_for_exact_finished_reply_owner(admission_runtime):
    chat, sid = admission_runtime
    previous = chat.TurnBroadcast(session_id=sid)
    previous.result_forwarded = True
    chat._active_turns[sid] = previous
    task = asyncio.create_task(chat._admit_turn(sid, "next fixture"))
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
        assert chat._active_turns[sid] is previous
        previous.finish()
        broadcast = await asyncio.wait_for(task, 1)
        assert broadcast is not previous
        assert broadcast.user_text == "next fixture"
        assert not broadcast.queue_item_id
    finally:
        if not task.done():
            task.cancel()
        previous.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_state", ["successor", "background", "queue"])
async def test_cleanup_wait_revalidates_successor_background_and_fifo(admission_runtime, monkeypatch, next_state):
    chat, sid = admission_runtime
    previous = chat.TurnBroadcast(session_id=sid)
    previous.result_forwarded = True
    chat._active_turns[sid] = previous
    task = asyncio.create_task(chat._admit_turn(sid, "must not overtake"))
    successor = None
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
        if next_state == "successor":
            successor = chat.TurnBroadcast(session_id=sid)
            chat._active_turns[sid] = successor
        elif next_state == "background":
            chat._sessions_with_inflight_tasks[sid] = {"background-fixture"}
        else:
            monkeypatch.setattr(chat.sess, "get_queue", lambda _sid: {
                "items": [{"id": "older-command"}], "inflight": None})
        previous.finish()
        with pytest.raises(chat._TurnBusy):
            await asyncio.wait_for(task, 1)
        if successor is not None:
            assert chat._active_turns[sid] is successor
    finally:
        if not task.done():
            task.cancel()
        previous.close()


@pytest.mark.asyncio
async def test_running_reply_still_queues_instead_of_waiting(admission_runtime):
    chat, sid = admission_runtime
    previous = chat.TurnBroadcast(session_id=sid)
    chat._active_turns[sid] = previous
    with pytest.raises(chat._TurnBusy):
        await asyncio.wait_for(chat._admit_turn(sid, "busy fixture"), 0.1)
    assert chat._active_turns[sid] is previous


def test_mux_state_carries_exact_queue_owner(admission_runtime):
    chat, sid = admission_runtime
    broadcast = chat.TurnBroadcast(session_id=sid)
    broadcast.queue_item_id = "q-owned-fixture"
    try:
        state = chat._mux_broadcast_state(broadcast)
        assert state["queue_item_id"] == "q-owned-fixture"
        assert state["turn_id"] == broadcast.turn_id
    finally:
        broadcast.close()
