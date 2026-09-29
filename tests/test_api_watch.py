"""Watch adapters exercise real metadata, durable queue admission and receipts."""

import asyncio
from uuid import UUID, uuid4
from urllib.parse import quote

import pytest


@pytest.fixture()
def watch_runtime(app_module, monkeypatch):
    from backend import api_watch, chat, sessions

    kicks = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", kicks.append)
    return api_watch, chat, sessions, kicks


def body(sid="new", text="请检查项目"):
    return {"request_id": str(uuid4()), "session_id": sid, "text": text}


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/api/watch/sessions", None),
        ("post", "/api/watch/messages", body()),
        ("get", f"/api/watch/sessions/{uuid4()}/reply", None),
        ("get", f"/api/watch/sessions/{uuid4()}/submissions/{uuid4()}", None),
    ],
    ids=["menu", "send", "reply", "receipt"],
)
def test_auth_required(client, method, path, payload):
    response = getattr(client, method)(path, **({"json": payload} if payload else {}))
    assert response.status_code == 401


def test_menu_is_recent_before_limit_and_has_unambiguous_keys(
    client,
    auth,
    temp_root,
    watch_runtime,
    monkeypatch,
):
    _, _, sessions, _ = watch_runtime
    recent, other_recent, pinned = [str(uuid4()) for _ in range(3)]
    monkeypatch.setattr(
        sessions,
        "list_sessions_snapshot",
        lambda: (
            [
                {
                    "id": pinned,
                    "name": "old",
                    "pinned": True,
                    "updated_at": 1,
                    "cwd": str(temp_root),
                },
                {
                    "id": other_recent,
                    "name": "same.title\n🧪",
                    "updated_at": 3,
                    "cwd": str(temp_root),
                },
                {"id": recent, "name": "same.title\n🧪", "updated_at": 4, "cwd": str(temp_root)},
                {
                    "id": str(uuid4()),
                    "name": "other",
                    "updated_at": 99,
                    "cwd": str(temp_root / "other"),
                },
                {
                    "id": str(uuid4()),
                    "name": "shadow",
                    "updated_at": 100,
                    "cwd": str(temp_root),
                    "runtime_shadow": True,
                },
            ],
            1,
        ),
    )
    response = client.get("/api/watch/sessions?limit=2", headers=auth)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    UUID(data["request_id"])
    fresh = client.get("/api/watch/sessions?limit=2", headers=auth).json()
    assert fresh["request_id"] != data["request_id"]
    assert data["labels"] == ["00 · 新建会话", "01 · same.title 🧪", "02 · same.title 🧪"]
    assert data["choices"] == {"00": "new", "01": recent, "02": other_recent}
    reply_menu = client.get("/api/watch/sessions?limit=2&include_new=false", headers=auth).json()
    assert reply_menu["choices"] == {"01": recent, "02": other_recent}


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"text": " \n "},
        {"text": "x" * 6001},
        {"request_id": "not-a-uuid"},
        {"session_id": "../bad"},
    ],
)
def test_invalid_input_never_creates_session(client, auth, watch_runtime, changes):
    _, _, sessions, kicks = watch_runtime
    payload = body()
    payload.update(changes)
    response = client.post("/api/watch/messages", json=payload, headers=auth)
    assert response.status_code == 422
    assert sessions.list_sessions() == []
    assert kicks == []


def test_new_session_and_durable_receipt_replay(client, auth, temp_root, watch_runtime):
    _, _, sessions, kicks = watch_runtime
    draft = sessions.create_session(name="keep browser draft", cwd=temp_root)["id"]
    payload = body(text="  请检查项目  ")
    first = client.post("/api/watch/messages", json=payload, headers=auth)
    second = client.post("/api/watch/messages", json=payload, headers=auth)
    assert first.status_code == second.status_code == 202
    sid = first.json()["session_id"]
    assert second.json()["session_id"] == sid
    assert first.json()["status"] == "accepted"
    queue = sessions.get_queue(sid)["items"]
    assert len(queue) == 1
    assert queue[0]["text"] == "请检查项目"
    assert queue[0]["permission"] == "default"
    assert queue[0]["id"] == "q-" + payload["request_id"]
    assert sessions.get_session_meta(sid)["permission"] == "default"
    assert sessions.get_session_meta(draft) is not None
    assert kicks == [sid]
    receipt = client.get(
        f"/api/watch/sessions/{sid}/submissions/{payload['request_id']}",
        headers=auth,
    )
    assert receipt.status_code == 200
    assert receipt.json()["state"] == "accepted"

    payload["text"] = "另一条消息"
    conflict = client.post("/api/watch/messages", json=payload, headers=auth)
    assert conflict.status_code == 409
    assert len(sessions.get_queue(sid)["items"]) == 1
    assert kicks == [sid]


@pytest.mark.parametrize("permission", ["default", "acceptEdits", "plan", "bypassPermissions"])
def test_existing_session_inherits_permission_and_replay_survives_mode_change(
    client,
    auth,
    temp_root,
    watch_runtime,
    permission,
):
    _, _, sessions, kicks = watch_runtime
    sid = sessions.create_session(cwd=temp_root, permission=permission)["id"]
    payload = body(sid)
    assert client.post("/api/watch/messages", json=payload, headers=auth).status_code == 202
    assert sessions.get_queue(sid)["items"][0]["permission"] == permission
    sessions.update_permission(sid, "default" if permission != "default" else "plan")
    retry = client.post("/api/watch/messages", json=payload, headers=auth)
    assert retry.status_code == 202
    assert len(sessions.get_queue(sid)["items"]) == 1
    assert kicks == [sid]


def test_busy_session_queues_without_interrupt(client, auth, temp_root, watch_runtime, monkeypatch):
    _, chat, sessions, kicks = watch_runtime
    sid = sessions.create_session(cwd=temp_root, permission="default")["id"]
    monkeypatch.setattr(chat, "_session_runtime_busy", lambda session: True)

    async def never_interrupt(*args, **kwargs):
        pytest.fail("watch admission must not interrupt a turn")

    monkeypatch.setattr(chat, "interrupt", never_interrupt)
    response = client.post("/api/watch/messages", json=body(sid), headers=auth)
    assert response.status_code == 202
    assert sessions.get_queue(sid)["items"][0]["delivery"] == "queue"
    assert kicks == [sid]


def test_unknown_session_rejected_before_queue_write(client, auth, watch_runtime):
    _, _, sessions, kicks = watch_runtime
    payload = body(str(uuid4()))
    response = client.post("/api/watch/messages", json=payload, headers=auth)
    assert response.status_code == 404
    assert sessions.list_sessions() == []
    assert kicks == []


def test_workspace_scope_and_encoded_header(client, auth, temp_root, watch_runtime):
    _, _, sessions, _ = watch_runtime
    from backend.workspaces import registry

    workspace = temp_root / "测试 工作区"
    workspace.mkdir()
    registry.register(workspace)
    sid = sessions.create_session(cwd=workspace, permission="default")["id"]
    payload = body(sid)
    assert client.post("/api/watch/messages", json=payload, headers=auth).status_code == 404
    headers = {**auth, "X-Muselab-Workspace": quote(str(workspace), safe="")}
    assert client.post("/api/watch/messages", json=payload, headers=headers).status_code == 202
    assert client.get(f"/api/watch/sessions/{sid}/reply", headers=auth).status_code == 404
    assert (
        client.get(
            f"/api/watch/sessions/{sid}/submissions/{payload['request_id']}",
            headers=auth,
        ).status_code
        == 404
    )
    assert client.get("/api/watch/sessions?include_new=false", headers=headers).json()[
        "choices"
    ] == {"01": sid}

    new_payload = body()
    first = client.post("/api/watch/messages", json=new_payload, headers=auth)
    assert first.status_code == 202
    assert client.post("/api/watch/messages", json=new_payload, headers=headers).status_code == 409


def test_invalid_workspace_rejected_without_creation(client, auth, watch_runtime):
    _, _, sessions, kicks = watch_runtime
    response = client.post(
        "/api/watch/messages",
        json=body(),
        headers={**auth, "X-Muselab-Workspace": "/not/a/registered/workspace"},
    )
    assert response.status_code == 400
    assert sessions.list_sessions() == []
    assert kicks == []


@pytest.mark.asyncio
async def test_concurrent_retry_creates_and_enqueues_once(temp_root, watch_runtime):
    watch, _, sessions, kicks = watch_runtime
    payload = watch.WatchMessage(**body())
    outcomes = await asyncio.gather(
        watch.send_message(payload, temp_root),
        watch.send_message(payload, temp_root),
        return_exceptions=True,
    )
    accepted = [value for value in outcomes if isinstance(value, dict)]
    assert accepted
    from fastapi import HTTPException

    assert all(
        isinstance(value, dict) or (isinstance(value, HTTPException) and value.status_code == 425)
        for value in outcomes
    )
    replay = await watch.send_message(payload, temp_root)
    assert replay["session_id"] == accepted[0]["session_id"]
    assert len(sessions.list_sessions()) == 1
    assert len(sessions.get_queue(replay["session_id"])["items"]) == 1
    assert kicks == [replay["session_id"]]


@pytest.mark.asyncio
async def test_caller_cancel_does_not_cancel_admission_or_drain(
    temp_root,
    watch_runtime,
    monkeypatch,
):
    watch, chat, sessions, kicks = watch_runtime
    entered, release, kicked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = chat._enqueue_impl

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    def kick(sid):
        kicks.append(sid)
        kicked.set()

    monkeypatch.setattr(chat, "_enqueue_impl", delayed)
    monkeypatch.setattr(chat, "_schedule_queue_drain", kick)
    request = watch.WatchMessage(**body())
    caller = asyncio.create_task(watch.send_message(request, temp_root))
    await asyncio.wait_for(entered.wait(), timeout=5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.wait_for(kicked.wait(), timeout=5)
    assert len(kicks) == 1
    assert len(sessions.get_queue(kicks[0])["items"]) == 1
    receipt = await watch.message_receipt(UUID(kicks[0]), request.request_id, temp_root)
    assert receipt["state"] == "accepted"


def test_queue_rejection_is_not_an_ack(client, auth, temp_root, watch_runtime, monkeypatch):
    _, _, sessions, kicks = watch_runtime
    sid = sessions.create_session(cwd=temp_root, permission="default")["id"]
    monkeypatch.setattr(
        sessions,
        "enqueue_existing_message",
        lambda *args, **kwargs: {"ok": False, "error": "queue_full"},
    )
    response = client.post("/api/watch/messages", json=body(sid), headers=auth)
    assert response.status_code == 409
    assert sessions.get_queue(sid)["items"] == []
    assert kicks == []


def test_reply_is_bounded_assistant_text_and_not_completion_proof(
    client,
    auth,
    temp_root,
    watch_runtime,
    monkeypatch,
):
    _, chat, sessions, _ = watch_runtime
    sid = sessions.create_session(name="fixture", cwd=temp_root, permission="default")["id"]
    observed = {}

    def history(session, **kwargs):
        observed.update(kwargs)
        return {
            "messages": [
                {"role": "assistant", "text": "older"},
                {"role": "thinking", "text": "hidden thinking"},
                {"role": "assistant", "text": "中" * 900},
                {"role": "tool", "text": "tool payload"},
                {"role": "user", "text": "next prompt"},
                {"role": "assistant", "text": ""},
            ]
        }

    monkeypatch.setattr(chat, "get_session_api", history)
    monkeypatch.setattr(chat, "_session_runtime_busy", lambda session: True)
    response = client.get(f"/api/watch/sessions/{sid}/reply", headers=auth)
    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == "中" * 800 + "…"
    assert data["truncated"] is True
    assert data["status"] == "执行中"
    assert "hidden thinking" not in data["message"]
    assert "tool payload" not in data["message"]
    assert observed == {
        "full": False,
        "tail": 40,
        "offset": -1,
        "limit": 0,
        "history_generation": "",
        "around_uuid": "",
        "before": 0,
        "after": 0,
    }


def test_reply_empty_history_does_not_claim_task_finished(client, auth, temp_root, watch_runtime):
    _, _, sessions, _ = watch_runtime
    sid = sessions.create_session(cwd=temp_root, permission="default")["id"]
    response = client.get(f"/api/watch/sessions/{sid}/reply", headers=auth)
    assert response.status_code == 200
    assert response.json()["status"] == "会话空闲"
    assert response.json()["reply"] == ""
    assert "暂无文字回复" in response.json()["message"]


@pytest.mark.asyncio
async def test_cancelled_admission_does_not_return_accepted(temp_root, watch_runtime, monkeypatch):
    watch, chat, sessions, kicks = watch_runtime
    from backend import submissions
    from fastapi import HTTPException

    request = watch.WatchMessage(**body())
    original = chat._enqueue_impl

    async def cancel_before_ack(sid, *args):
        result = await original(sid, *args)
        await asyncio.to_thread(submissions.cancel, sid, "queue", str(request.request_id))
        return result

    monkeypatch.setattr(chat, "_enqueue_impl", cancel_before_ack)
    with pytest.raises(HTTPException) as raised:
        await watch.send_message(request, temp_root)
    assert raised.value.status_code == 409
    sid = sessions.list_sessions()[0]["id"]
    assert sessions.get_queue(sid)["items"] == []
