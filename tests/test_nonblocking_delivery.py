"""Manual queue holds and exact cancellation must not lock later input."""
import asyncio

import pytest
from fastapi import HTTPException


def test_pause_holds_only_snapshot_and_survives_restart(app_module):
    from backend import sessions as s
    sid = s.create_session()["id"]
    old = s.enqueue_message(sid, "old")["item"]
    s.set_queue_paused(sid, True)
    assert s.claim_queue_message(sid) is None
    snapshot = s.recover_queue_inflight(sid)
    assert snapshot["items"][0]["held"]
    assert s.recover_queue_inflight(sid)["revision"] == snapshot["revision"]
    new = s.enqueue_message(sid, "manual follow-up")["item"]
    assert s.claim_queue_message(sid)["id"] == new["id"]
    s.ack_queue_message(sid, new["id"], "")
    assert s.claim_queue_message(sid) is None
    s.set_queue_paused(sid, False)
    assert s.claim_queue_message(sid)["id"] == old["id"]


def test_pause_snapshot_fences_late_post_not_newer_input(app_module):
    from backend import sessions as s
    sid = s.create_session()["id"]
    s.set_queue_paused(sid, True, ["q-before-stop"])
    s.enqueue_message(sid, "late old request", item_id="q-before-stop")
    s.enqueue_message(sid, "new input", item_id="q-after-stop")
    assert s.claim_queue_message(sid)["id"] == "q-after-stop"
    assert s.get_queue(sid)["items"][0]["held"]


def test_held_messages_leave_runnable_capacity_and_migrate(app_module):
    from backend import sessions as s
    sid = s.create_session()["id"]
    for i in range(s._QUEUE_MAX):
        assert s.enqueue_message(sid, f"held {i}")["ok"]
    s.set_queue_paused(sid, True)
    fresh = s.enqueue_message(sid, "new manual input")
    assert fresh["ok"]
    assert s.claim_queue_message(sid)["id"] == fresh["item"]["id"]
    s.ack_queue_message(sid, fresh["item"]["id"], "")
    child = s.create_session()["id"]
    moved = s.migrate_queue(sid, child)
    assert moved["target"]["paused"]
    assert s.claim_queue_message(child) is None


@pytest.mark.asyncio
async def test_cancel_queue_before_post_prevents_admission(app_module):
    from backend import chat as c, sessions as s
    sid = s.create_session()["id"]
    await c.cancel_turn_submission(sid, "cancel-first", "queue")
    with pytest.raises(HTTPException) as exc:
        await c.enqueue_api(sid, c.QueueEnqueueReq(text="cancelled", client_message_id="cancel-first"), c.BackgroundTasks())
    assert exc.value.status_code == 409
    assert not s.get_queue(sid)["items"]


@pytest.mark.asyncio
async def test_cancel_queue_during_post_removes_late_commit(app_module, monkeypatch):
    from backend import chat as c, sessions as s
    sid = s.create_session()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    real_enqueue = c._enqueue_impl

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await real_enqueue(*args)

    monkeypatch.setattr(c, "_enqueue_impl", delayed)
    monkeypatch.setattr(c, "_schedule_queue_drain", lambda _: None)
    request = c.QueueEnqueueReq(text="fixture", client_message_id="late-cancel")
    task = asyncio.create_task(c.enqueue_api(sid, request, c.BackgroundTasks()))
    await entered.wait()
    state = await c.cancel_turn_submission(sid, "late-cancel", "queue")
    assert state["state"] == "cancel_requested"
    release.set()
    assert (await task)["cancelled"]
    assert not s.get_queue(sid)["items"]


@pytest.mark.asyncio
async def test_cancel_queued_owner_never_interrupts_successor(app_module, monkeypatch):
    from backend import chat as c, sessions as s, submissions
    sid = s.create_session()["id"]
    request = c.QueueEnqueueReq(text="fixture", client_message_id="owned")
    await c.enqueue_api(sid, request, c.BackgroundTasks())
    item = s.claim_queue_message(sid)
    s.bind_queue_turn(sid, item["id"], "exact-owner")
    stops = []
    async def stop(session_id, turn_id):
        stops.append(turn_id)
    monkeypatch.setattr(c, "interrupt", stop)
    await c.cancel_turn_submission(sid, "owned", "queue")
    assert stops == ["exact-owner"]
    assert submissions.lookup(sid, "queue", "owned")["state"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_cancellation_holds_exact_snapshot_not_new_followup(app_module):
    from backend import chat as c, sessions as s
    sid = s.create_session()["id"]
    s.enqueue_message(sid, "old", item_id="q-old")
    await c.cancel_turn_submission(sid, "primary", pause_item_ids="q-old,q-delayed")
    s.enqueue_message(sid, "delayed old", item_id="q-delayed")
    s.enqueue_message(sid, "new manual", item_id="q-new")
    assert s.claim_queue_message(sid)["id"] == "q-new"


@pytest.mark.asyncio
async def test_stale_stop_cannot_hold_successor_queue(app_module):
    from backend import chat as c, sessions as s
    sid = s.create_session()["id"]
    s.enqueue_message(sid, "new", item_id="q-new")
    bc = c.TurnBroadcast(sid)
    c._active_turns[sid] = bc
    try:
        await c.interrupt(sid, "old-turn", pause_item_ids="q-new")
        assert not s.get_queue(sid)["items"][0].get("held")
    finally:
        c._active_turns.pop(sid, None)
        bc.close()
