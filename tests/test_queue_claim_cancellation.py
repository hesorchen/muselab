"""Real queue claims with controlled dispatch waits and no SDK inference."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Response


@pytest_asyncio.fixture
async def claimed_queue(app_module, monkeypatch):
    from backend import chat, sessions

    sid = sessions.create_session()["id"]
    head = sessions.enqueue_message(sid, "synthetic first message")["item"]
    follower = sessions.enqueue_message(sid, "synthetic follower message")["item"]
    runtime = SimpleNamespace(
        main=app_module,
        chat=chat,
        sessions=sessions,
        sid=sid,
        head=head,
        follower=follower,
        gate_site="depth_read",
        gate_used=False,
        entered=asyncio.Event(),
        release=asyncio.Event(),
        bind_before_wait=False,
        broadcast=None,
        owner=None,
        acknowledged=[],
    )
    original_io = chat.obs.to_thread_io

    async def gated_io(site, session_id, func, *args, **kwargs):
        if (runtime.gate_site == "depth_read" and not runtime.gate_used
                and site == "chat.queue_read" and session_id == sid):
            inflight = sessions.get_queue(sid).get("inflight") or {}
            if (inflight.get("item") or {}).get("id") == head["id"]:
                runtime.gate_used = True
                runtime.entered.set()
                await runtime.release.wait()
        return await original_io(site, session_id, func, *args, **kwargs)

    async def no_flush(_sid):
        return None

    async def mock_start(session_id, _prompt, **kwargs):
        item_id = kwargs["queue_item_id"]
        if runtime.gate_site == "start_turn" and not runtime.gate_used:
            runtime.gate_used = True
            if runtime.bind_before_wait:
                broadcast = chat.TurnBroadcast(session_id)
                broadcast.queue_item_id = item_id
                runtime.owner = asyncio.create_task(asyncio.Event().wait())
                broadcast.task = runtime.owner
                runtime.broadcast = broadcast
                chat._active_turns[session_id] = broadcast
                sessions.bind_queue_turn(session_id, item_id, broadcast.turn_id)
            runtime.entered.set()
            await runtime.release.wait()
        turn_id = f"mock-turn-{len(runtime.acknowledged) + 1}"
        sessions.bind_queue_turn(session_id, item_id, turn_id)
        assert sessions.ack_queue_message(session_id, item_id, turn_id)
        runtime.acknowledged.append(item_id)

    monkeypatch.setattr(chat.obs, "to_thread_io", gated_io)
    monkeypatch.setattr(chat, "_flush_runtime_continuations_at_turn_boundary", no_flush)
    monkeypatch.setattr(chat, "_runtime_lineage_has_ready_continuation", lambda _sid: False)
    monkeypatch.setattr(chat, "_start_turn", mock_start)
    monkeypatch.setattr(sessions, "sdk_get_session_info", lambda *_args, **_kwargs: None)
    try:
        yield runtime
    finally:
        runtime.release.set()
        if runtime.owner is not None:
            runtime.owner.cancel()
            await asyncio.gather(runtime.owner, return_exceptions=True)
        await chat.shutdown_runtime()
        if runtime.broadcast is not None:
            runtime.broadcast.close()


def _kick(runtime):
    runtime.chat._schedule_queue_drain(runtime.sid)
    return runtime.chat._queue_drain_tasks[runtime.sid]


def _assert_fifo_restored(runtime):
    snapshot = runtime.sessions.get_queue(runtime.sid)
    visible = runtime.chat.get_queue_api(runtime.sid, Response())
    ids = [runtime.head["id"], runtime.follower["id"]]
    assert [item["id"] for item in snapshot["items"]] == ids
    assert [item["id"] for item in visible["items"]] == ids
    assert snapshot["inflight"] is None
    assert not snapshot["paused"]
    assert not any(item.get("queue_issue") for item in snapshot["items"])
    assert runtime.acknowledged == []


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["depth_read", "start_turn"])
async def test_cancelled_claim_restores_visible_fifo_and_rekicks(claimed_queue, site):
    runtime = claimed_queue
    runtime.gate_site = site
    task = _kick(runtime)
    await asyncio.wait_for(runtime.entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_fifo_restored(runtime)
    runtime.release.set()
    await asyncio.wait_for(_kick(runtime), 3)
    await asyncio.wait_for(_kick(runtime), 3)
    assert runtime.acknowledged == [runtime.head["id"], runtime.follower["id"]]
    assert runtime.sessions.get_queue(runtime.sid)["inflight"] is None
    assert runtime.sessions.get_queue(runtime.sid)["items"] == []


@pytest.mark.asyncio
async def test_shutdown_during_depth_read_restores_claim_before_restart(claimed_queue):
    runtime = claimed_queue
    task = _kick(runtime)
    await asyncio.wait_for(runtime.entered.wait(), 3)
    await runtime.chat.shutdown_runtime()
    assert task.cancelled()
    _assert_fifo_restored(runtime)
    # Healthy restored records survive the real startup sweep without mutation.
    revision = runtime.sessions.get_queue(runtime.sid)["revision"]
    assert await runtime.main._recover_message_queues_at_startup(runtime.sessions) == 1
    _assert_fifo_restored(runtime)
    assert runtime.sessions.get_queue(runtime.sid)["revision"] == revision
    runtime.release.set()
    runtime.chat._queue_runtime_closing = False
    await asyncio.wait_for(_kick(runtime), 3)
    await asyncio.wait_for(_kick(runtime), 3)
    assert runtime.acknowledged == [runtime.head["id"], runtime.follower["id"]]


@pytest.mark.asyncio
async def test_cancelled_bound_claim_keeps_live_owner_and_followers_in_order(claimed_queue):
    runtime = claimed_queue
    runtime.gate_site = "start_turn"
    runtime.bind_before_wait = True
    task = _kick(runtime)
    await asyncio.wait_for(runtime.entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = runtime.sessions.get_queue(runtime.sid)
    assert snapshot["inflight"]["item"]["id"] == runtime.head["id"]
    assert snapshot["inflight"]["turn_id"] == runtime.broadcast.turn_id
    assert [item["id"] for item in snapshot["items"]] == [runtime.follower["id"]]
    assert not runtime.owner.done()
    await asyncio.wait_for(_kick(runtime), 3)
    assert runtime.acknowledged == []
    assert runtime.sessions.ack_queue_message(
        runtime.sid, runtime.head["id"], runtime.broadcast.turn_id,
    )
    runtime.acknowledged.append(runtime.head["id"])
    runtime.chat._active_turns.pop(runtime.sid)
    runtime.owner.cancel()
    await asyncio.gather(runtime.owner, return_exceptions=True)
    runtime.broadcast.close()
    runtime.release.set()
    await asyncio.wait_for(_kick(runtime), 3)
    assert runtime.acknowledged == [runtime.head["id"], runtime.follower["id"]]
    assert runtime.sessions.get_queue(runtime.sid)["inflight"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["depth_read", "start_turn"])
async def test_cancelled_claim_cannot_recreate_deleted_session(claimed_queue, site):
    runtime = claimed_queue
    runtime.gate_site = site
    task = _kick(runtime)
    await asyncio.wait_for(runtime.entered.wait(), 3)
    assert await asyncio.to_thread(runtime.sessions.delete_session, runtime.sid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.sid not in runtime.sessions.indexed_session_ids()
    assert not runtime.sessions._queue_path(runtime.sid).exists()
    assert runtime.sessions.get_queue(runtime.sid)["items"] == []
    assert runtime.sessions.get_queue(runtime.sid)["inflight"] is None
    assert runtime.sessions.recover_queue_inflight(runtime.sid)["items"] == []
    assert not runtime.sessions._queue_path(runtime.sid).exists()
    runtime.release.set()
    await asyncio.wait_for(_kick(runtime), 3)
    assert runtime.acknowledged == []
    assert not runtime.sessions._queue_path(runtime.sid).exists()
