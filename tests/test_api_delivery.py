"""Authenticated restore barriers exercise real temporary files, no model calls."""

from types import SimpleNamespace
import uuid
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Safe checkpoint restore requires Linux file notifications"
)


@pytest.fixture
def delivery_session(app_module, temp_root, monkeypatch):
    from backend import api_delivery, chat, file_checkpoints, sessions, task_delivery

    meta = sessions.create_session(name="Delivery fixture", model="deepseek-v4-pro", cwd=temp_root)
    sid, turn, cid = meta["id"], str(uuid.uuid4()), str(uuid.uuid4())
    task_delivery.begin(sid, turn, temp_root)
    file_checkpoints.begin(sid, turn, temp_root)
    file_checkpoints.record_id(sid, turn, cid)
    target = temp_root / "result.txt"
    target.write_text("before")
    payload = {
        "session_id": sid,
        "cwd": str(temp_root),
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "after"},
    }
    for event in ("PreToolUse", "PostToolUse"):
        if event == "PostToolUse":
            target.write_text("after")
        file_checkpoints.observe(sid, turn, temp_root, event, payload, "write_1")
        task_delivery.record_tool(sid, turn, temp_root, event, payload, "write_1")
    calls = []

    class FakeClient:
        async def rewind_files(self, user_message_id):
            assert chat._session_runtime_busy(sid)
            with pytest.raises(Exception, match="workspace_file_restore_in_progress"):
                api_delivery.assert_not_restoring(sid)
            calls.append(user_message_id)
            target.write_text("before")

        async def disconnect(self):
            pass

    fake = FakeClient()

    async def get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat, "get_client", get_client)
    return SimpleNamespace(
        sid=sid,
        cid=cid,
        turn=turn,
        target=target,
        root=temp_root,
        calls=calls,
        fake=fake,
        chat=chat,
        api=api_delivery,
    )


def preview(client, auth, fixture):
    response = client.get(
        f"/api/chat/sessions/{fixture.sid}/checkpoints/{fixture.cid}/preview", headers=auth
    )
    assert response.status_code == 200, response.text
    assert response.json()["can_restore"]
    return response.json()


def test_delivery_auth_and_real_workspace(client, auth, delivery_session):
    f = delivery_session
    url = f"/api/chat/sessions/{f.sid}/delivery"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=auth)
    assert response.status_code == 200
    data = response.json()
    assert data["runtime"]["workspace"] == str(f.root)
    assert data["artifacts"][0]["path"] == "result.txt"
    assert not data["diff"]["available"]
    denied = client.get(
        f"/api/chat/sessions/{f.sid}/runtime",
        params={"workspace": str(f.root.parent)},
        headers=auth,
    )
    assert denied.status_code == 409


def test_restore_requires_confirmation_and_verifies_public_control(client, auth, delivery_session):
    f = delivery_session
    data = preview(client, auth, f)
    url = f"/api/chat/sessions/{f.sid}/checkpoints/{f.cid}/restore"
    assert client.post(url, json={"token": data["token"]}, headers=auth).status_code == 400
    assert f.target.read_text() == "after" and not f.calls
    response = client.post(url, json={"token": data["token"], "confirmed": True}, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["verified"] and response.json()["conversation_preserved"]
    assert f.calls == [f.cid] and f.target.read_text() == "before"
    assert not f.api.RESTORING
    assert (
        client.post(url, json={"token": data["token"], "confirmed": True}, headers=auth).status_code
        == 409
    )


@pytest.mark.parametrize("busy", ["turn", "queue", "cron", "rebuild"])
def test_preview_rejects_active_or_scheduled_writers(
    client, auth, delivery_session, monkeypatch, busy
):
    from backend import sessions

    f = delivery_session
    if busy == "turn":
        f.chat._active_turns[f.sid] = SimpleNamespace(done=False)
    elif busy == "queue":
        monkeypatch.setattr(sessions, "get_queue", lambda _sid: {"items": [{"id": "pending"}]})
    elif busy == "cron":
        f.chat._sdk_cron_jobs[f.sid] = {"cron": {"runtime_state": "active"}}
    else:
        f.chat._pending_runtime_rebuilds.add(f.sid)
    try:
        response = client.get(
            f"/api/chat/sessions/{f.sid}/checkpoints/{f.cid}/preview", headers=auth
        )
        assert response.status_code == 409
        assert not f.calls and f.target.read_text() == "after"
    finally:
        f.chat._active_turns.pop(f.sid, None)
        f.chat._pending_runtime_rebuilds.discard(f.sid)


def test_restore_rejects_external_aba_and_other_runtime(client, auth, delivery_session):
    f = delivery_session
    data = preview(client, auth, f)
    f.target.write_text("external")
    f.target.write_text("after")
    response = client.post(
        f"/api/chat/sessions/{f.sid}/checkpoints/{f.cid}/restore",
        json={"token": data["token"], "confirmed": True},
        headers=auth,
    )
    assert response.status_code == 409 and not f.calls
    assert not f.api.RESTORING


def test_workspace_restore_barrier_covers_other_sessions(client, auth, delivery_session):
    from backend import sessions

    f = delivery_session
    other = sessions.create_session(name="Other task", model="deepseek-v4-pro", cwd=f.root)["id"]
    f.api.RESTORING[f.sid] = str(f.root)
    try:
        with pytest.raises(Exception, match="workspace_file_restore_in_progress"):
            f.api.assert_not_restoring(other)
    finally:
        f.api.RESTORING.clear()


def test_restore_blocks_parent_child_workspace_writers(client, auth, delivery_session, monkeypatch):
    f = delivery_session
    child = f.root / "nested-project"
    child.mkdir()
    f.api.RESTORING[f.sid] = str(child)
    # The actual root and child overlap even though their strings differ.
    monkeypatch.setattr(
        f.api, "owned_workspace", lambda sid: ({}, f.root if sid == "parent-session" else child)
    )
    try:
        with pytest.raises(Exception, match="workspace_file_restore_in_progress"):
            f.api.assert_not_restoring("parent-session")
        f.api.RESTORING[f.sid] = str(f.root)
        with pytest.raises(Exception, match="workspace_file_restore_in_progress"):
            f.api.assert_not_restoring("child-session")
        sibling = f.root.parent / "sibling-project"
        sibling.mkdir()
        assert not f.api.workspaces_overlap(child, sibling)
    finally:
        f.api.RESTORING.clear()


def test_deleting_session_removes_delivery_and_invalidates_preview(client, auth, delivery_session):
    from backend import sessions, task_delivery, file_checkpoints

    f = delivery_session
    data = preview(client, auth, f)
    sessions.delete_session(f.sid)
    assert not task_delivery.path(f.sid).exists()
    assert not file_checkpoints._path(f.sid).exists()
    assert data["token"] not in file_checkpoints._PREVIEWS
    task_delivery.begin(f.sid, f.turn, f.root)
    assert not task_delivery.path(f.sid).exists()


@pytest.mark.parametrize("writer", ["background", "watcher", "scheduled", "cron"])
@pytest.mark.parametrize("scope", ["same", "nested", "unrelated"])
def test_restore_detects_detached_workspace_writers(
    client, auth, delivery_session, monkeypatch, writer, scope
):
    from backend import sessions

    f = delivery_session
    other = sessions.create_session(name="Detached task", model="deepseek-v4-pro", cwd=f.root)["id"]
    owner = f.api.owned_workspace
    other_cwd = {
        "same": f.root,
        "nested": f.root / "nested-project",
        "unrelated": f.root.parent / "separate-project",
    }[scope]
    monkeypatch.setattr(f.api, "owned_workspace", lambda sid: ({}, other_cwd) if sid == other else owner(sid))
    stores = {
        "background": (f.chat._sessions_with_inflight_tasks, other, {"task"}),
        "watcher": (f.chat._task_watchers, other, SimpleNamespace(done=lambda: False)),
        "scheduled": (f.chat._sdk_deliveries, (other, "scheduled"), SimpleNamespace(broadcast=SimpleNamespace(done=False))),
        "cron": (f.chat._sdk_cron_jobs, other, [{"id": "cron"}]),
    }
    store, key, value = stores[writer]
    store[key] = value
    assert other not in f.chat._active_turns
    try:
        response = client.get(
            f"/api/chat/sessions/{f.sid}/checkpoints/{f.cid}/preview", headers=auth
        )
        assert response.status_code == (200 if scope == "unrelated" else 409)
        assert not f.calls and f.target.read_text(encoding="utf-8") == "after"
    finally:
        store.pop(key, None)
