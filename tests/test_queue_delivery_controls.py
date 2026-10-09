"""Cancellation must fence uncommitted input without guessing runtime delivery."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import Response


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["recall", "annotation"])
async def test_cancel_before_native_write_prevents_late_delivery(app_module, monkeypatch, phase):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    writes, cancels = [], []

    class Client:
        async def query_steering(self, text, **kwargs):
            writes.append(kwargs["command_uuid"])

        async def cancel_async_message(self, command_uuid):
            cancels.append(command_uuid)
            return False  # The runtime has not received any input yet.

    monkeypatch.setattr(chat, "MuseLabSDKClient", Client)
    monkeypatch.setattr(chat.mem0, "enabled", lambda: phase == "recall")
    monkeypatch.setattr(chat.mem0, "clear_prepared_recall", lambda *a, **kw: None)
    original_io = chat.obs.to_thread_io

    async def prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return False

    async def io(site, *args, **kwargs):
        if phase == "annotation" and site == "chat.queue_steering_annotation":
            entered.set()
            await release.wait()
        return await original_io(site, *args, **kwargs)

    monkeypatch.setattr(chat.mem0, "prepare_recall", prepare)
    monkeypatch.setattr(chat.obs, "to_thread_io", io)
    bc = chat.TurnBroadcast(sid)
    bc.runtime_client = Client()
    bc.query_committed = True
    chat._active_turns[sid] = bc
    task = asyncio.create_task(chat.enqueue_api(sid, chat.QueueEnqueueReq(
        text="synthetic adjustment", delivery="adjust", active_turn_id=bc.turn_id,
    ), chat.BackgroundTasks()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        item = sess.get_queue(sid)["items"][0]
        result = await chat.remove_queue_item_api(sid, item["id"])
        assert result["items"] == []
        release.set()
        await asyncio.wait_for(task, 2)
        assert writes == []
        assert cancels == []
        assert sess.get_queue(sid)["items"] == []
        assert bc.steering_commands == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        chat._active_turns.pop(sid, None)
        bc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_cancel_racing_native_write_waits_for_runtime_receipt(app_module, monkeypatch, accepted):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    class Client:
        async def query_steering(self, text, **kwargs):
            entered.set()
            await release.wait()
            events.append("written")

        async def cancel_async_message(self, command_uuid):
            events.append("cancel")
            return accepted if "written" in events else False

    monkeypatch.setattr(chat, "MuseLabSDKClient", Client)
    monkeypatch.setattr(chat.mem0, "enabled", lambda: False)
    bc = chat.TurnBroadcast(sid)
    bc.runtime_client = Client()
    bc.query_committed = True
    chat._active_turns[sid] = bc
    enqueue = asyncio.create_task(chat.enqueue_api(sid, chat.QueueEnqueueReq(
        text="synthetic race", delivery="adjust", active_turn_id=bc.turn_id,
    ), chat.BackgroundTasks()))
    cancel = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        item = sess.get_queue(sid)["items"][0]
        cancel = asyncio.create_task(chat.remove_queue_item_api(sid, item["id"]))
        await asyncio.sleep(0.03)
        assert not cancel.done()
        release.set()
        await asyncio.wait_for(enqueue, 2)
        if accepted:
            assert (await asyncio.wait_for(cancel, 2))["items"] == []
        else:
            with pytest.raises(chat.HTTPException) as error:
                await asyncio.wait_for(cancel, 2)
            assert error.value.status_code == 409
            assert sess.get_queue(sid)["items"][0]["id"] == item["id"]
        assert events == ["written", "cancel"]
    finally:
        release.set()
        await asyncio.gather(enqueue, *([cancel] if cancel else []), return_exceptions=True)
        chat._active_turns.pop(sid, None)
        bc.close()


def test_empty_queue_read_does_not_run_attachment_gc(app_module, monkeypatch):
    from backend import chat

    def forbidden():
        raise AssertionError("queue reads must not wait for global attachment GC")

    monkeypatch.setattr(chat, "_gc_images_locked", forbidden)
    assert chat.get_queue_api("synthetic-empty", Response())["items"] == []
