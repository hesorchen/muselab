"""A lost response must not create a duplicate model/queue admission."""
import asyncio

import pytest
from fastapi import HTTPException


def test_receipts_are_private_idempotent_and_payload_free(app_module):
    from backend import sessions, submissions
    sid = sessions.create_session()["id"]
    payload = {"prompt": "private synthetic prompt"}
    assert submissions.reserve(sid, "turn", "request-1", payload)[0] is True
    assert submissions.reserve(sid, "turn", "request-1", payload)[0] is False
    with pytest.raises(ValueError):
        submissions.reserve(sid, "turn", "request-1", {"prompt": "different"})
    submissions.finish(sid, "turn", "request-1", "accepted", {"turn_id": "turn-1"})
    assert submissions.lookup(sid, "turn", "request-1")["result"] == {"turn_id": "turn-1"}
    path = sessions.SESS_DIR / ".submissions" / "receipts.sqlite3"
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"private synthetic prompt" not in path.read_bytes()
    submissions.purge(sid)
    assert submissions.lookup(sid, "turn", "request-1")["state"] == "not_found"


@pytest.mark.asyncio
async def test_duplicate_turn_start_and_cancel_target_exact_receipt(app_module, monkeypatch):
    from backend import chat, sessions
    sid = sessions.create_session()["id"]
    starts, stops = [], []

    async def start(req):
        starts.append(req.prompt)
        return {"accepted": True, "session_id": sid, "turn_id": "turn-1", "started_at": 1}

    async def stop(session_id, turn_id):
        stops.append((session_id, turn_id))

    monkeypatch.setattr(chat, "_turn_start_impl", start)
    monkeypatch.setattr(chat, "interrupt", stop)
    req = chat.TurnStartReq(session_id=sid, prompt="fixture", client_message_id="request-1")
    one = await chat.turn_start(req)
    two = await chat.turn_start(req)
    assert one == two
    assert starts == ["fixture"]
    await chat.cancel_turn_submission(sid, "request-1")
    assert stops == [(sid, "turn-1")]


@pytest.mark.asyncio
async def test_cancel_before_request_or_during_admission(app_module, monkeypatch):
    from backend import chat, sessions
    sid = sessions.create_session()["id"]
    await chat.cancel_turn_submission(sid, "early")
    with pytest.raises(HTTPException) as exc:
        await chat.turn_start(chat.TurnStartReq(session_id=sid, prompt="fixture", client_message_id="early"))
    assert exc.value.status_code == 409
    entered, release = asyncio.Event(), asyncio.Event()
    stops = []

    async def start(req):
        entered.set()
        await release.wait()
        return {"accepted": True, "turn_id": "late-turn"}

    async def stop(session_id, turn_id):
        stops.append(turn_id)

    monkeypatch.setattr(chat, "_turn_start_impl", start)
    monkeypatch.setattr(chat, "interrupt", stop)
    req = chat.TurnStartReq(session_id=sid, prompt="fixture", client_message_id="late")
    pending = asyncio.create_task(chat.turn_start(req))
    await entered.wait()
    result = await chat.cancel_turn_submission(sid, "late")
    assert result["state"] == "cancel_requested"
    release.set()
    assert (await pending)["cancelled"] is True
    assert stops == ["late-turn"]


@pytest.mark.asyncio
async def test_queue_replay_does_not_enqueue_twice(app_module):
    from backend import chat, sessions
    sid = sessions.create_session()["id"]
    request = chat.QueueEnqueueReq(text="fixture", client_message_id="queue-request")
    first = await chat.enqueue_api(sid, request, chat.BackgroundTasks())
    second = await chat.enqueue_api(sid, request, chat.BackgroundTasks())
    assert first["item"]["id"] == second["item"]["id"] == "q-queue-request"
    assert len(sessions.get_queue(sid)["items"]) == 1
    # The receipt stays useful even after the item is consumed/removed.
    sessions.remove_queue_item(sid, first["item"]["id"])
    third = await chat.enqueue_api(sid, request, chat.BackgroundTasks())
    assert third["queue"]["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_foreground_steering_allows_background_and_queue_origin(app_module, monkeypatch, queued):
    from backend import chat, sessions
    sid = sessions.create_session()["id"]
    writes = []

    class Client:
        async def query_steering(self, text, **kwargs):
            writes.append(text)

    monkeypatch.setattr(chat, "MuseLabSDKClient", Client)
    broadcast = chat.TurnBroadcast(sid)
    broadcast.query_committed = True
    broadcast.runtime_client = Client()
    if queued:
        broadcast.queue_item_id = "root-queue-item"
    chat._active_turns[sid] = broadcast
    chat._sessions_with_inflight_tasks[sid] = {"background-task"}
    try:
        response = await chat.enqueue_api(sid, chat.QueueEnqueueReq(
            text="adjust fixture", delivery="adjust", active_turn_id=broadcast.turn_id,
        ), chat.BackgroundTasks())
        assert response["effective_delivery"] == "adjust"
        assert writes == ["adjust fixture"]
        assert chat._admitted_steering_turn(sid, "different-turn") is None
    finally:
        chat._active_turns.pop(sid, None)
        chat._sessions_with_inflight_tasks.pop(sid, None)
        broadcast.close()


def test_submission_and_service_endpoints_require_auth(client):
    assert client.get("/api/settings/service").status_code == 401
    assert client.get("/api/chat/sessions/s/submissions/r").status_code == 401


@pytest.mark.asyncio
async def test_disconnected_http_owner_still_commits_one_receipt(app_module, monkeypatch):
    from backend import chat, sessions, submissions
    sid = sessions.create_session()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    starts = []

    async def start(req):
        starts.append(req.client_message_id)
        entered.set()
        await release.wait()
        return {"accepted": True, "turn_id": "retained-turn"}

    monkeypatch.setattr(chat, "_turn_start_impl", start)
    request = chat.TurnStartReq(session_id=sid, prompt="fixture", client_message_id="retained")
    task = asyncio.create_task(chat.turn_start(request))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        receipt = await asyncio.to_thread(submissions.lookup, sid, "turn", "retained")
        if receipt["state"] == "accepted":
            break
        await asyncio.sleep(0.01)
    assert receipt["state"] == "accepted"
    assert (await chat.turn_start(request))["turn_id"] == "retained-turn"
    assert starts == ["retained"]


@pytest.mark.asyncio
async def test_queue_commit_after_previous_turn_stop_remains_runnable(app_module, monkeypatch):
    from backend import chat, sessions
    sid = sessions.create_session()["id"]
    broadcast = chat.TurnBroadcast(sid)
    chat._active_turns[sid] = broadcast
    real_enqueue = sessions.enqueue_existing_message

    def delayed_enqueue(*args, **kwargs):
        # Simulate Stop after its empty-queue read but before commit.
        broadcast.cancelled = True
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(sessions, "enqueue_existing_message", delayed_enqueue)
    try:
        result = await chat.enqueue_api(sid, chat.QueueEnqueueReq(
            text="late fixture", active_turn_id=broadcast.turn_id,
            client_message_id="late-queue"), chat.BackgroundTasks())
        assert result["queue"]["paused"] is False
        assert sessions.dequeue_message(sid)["text"] == "late fixture"
    finally:
        chat._active_turns.pop(sid, None)
        broadcast.close()
