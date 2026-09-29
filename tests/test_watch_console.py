"""Exercise the complete menu protocol against real queue and private contexts."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture()
def runtime(app_module, monkeypatch):
    from backend import api_watch_console as console, chat, sessions

    kicks = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", kicks.append)
    monkeypatch.setattr(chat, "_session_runtime_busy", lambda sid: False)
    return console, chat, sessions, kicks


@pytest.fixture()
def cid():
    return str(uuid4())


def home(client, auth, cid):
    return client.get("/api/watch/menu", params={"client_id": cid}, headers=auth).json()


def press(client, auth, cid, view, label=None, text=""):
    action = view["action"]
    if label is not None:
        selected = next(row for row in view["labels"] if label in row)
        action = view["choices"][selected[:2]]
    r = client.post(
        "/api/watch/actions",
        headers=auth,
        json={
            "client_id": cid,
            "request_id": view["request_id"],
            "action": action,
            "text": text,
        },
    )
    assert r.status_code == 200
    return r.json()


def pick_session(client, auth, cid, name):
    recent = press(client, auth, cid, home(client, auth, cid), "最近会话")
    return press(client, auth, cid, recent, name)


def test_requires_auth_and_no_cache(client, auth, cid):
    assert client.get("/api/watch/menu", params={"client_id": cid}).status_code == 401
    assert client.post("/api/watch/actions", json={}).status_code == 401
    response = client.get("/api/watch/menu", params={"client_id": cid}, headers=auth)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["kind"] == "menu"
    assert "定时" not in str(response.json()["labels"])


def test_empty_continue_falls_back_to_recent(client, auth, cid, runtime):
    recent = press(client, auth, cid, home(client, auth, cid), "继续上次")
    assert "暂无最近" in recent["message"]
    assert any("新建" in s for s in recent["labels"])
    assert not runtime[2].list_sessions()


def test_new_send_confirm_replay_and_continue(client, auth, cid, temp_root, runtime):
    console, _, sessions, kicks = runtime
    input_view = press(client, auth, cid, home(client, auth, cid), "新建")
    assert input_view["kind"] == "input"
    confirm = press(client, auth, cid, input_view, text="  protocol test  ")
    assert confirm["kind"] == "confirm"
    assert not sessions.list_sessions()  # Viewing confirmation has no side effects.
    assert "protocol test" in confirm["message"]
    ack = press(client, auth, cid, confirm)
    duplicate = press(client, auth, cid, confirm)
    assert "已接受并排队" in ack["message"]
    assert duplicate["kind"] == "message"
    rows = sessions.list_sessions()
    assert len(rows) == 1
    sid = rows[0]["id"]
    assert len(sessions.get_queue(sid)["items"]) == 1
    assert sessions.get_queue(sid)["items"][0]["text"] == "protocol test"
    assert kicks == [sid]
    scope = console._scope(__import__("uuid").UUID(cid), temp_root)
    assert console._last(scope) == sid
    next_input = press(client, auth, cid, home(client, auth, cid), "继续上次")
    assert next_input["kind"] == "input"
    assert console._unpack(next_input["action"], scope)["sid"] == sid


def test_blank_input_never_creates_session(client, auth, cid, runtime):
    form = press(client, auth, cid, home(client, auth, cid), "新建")
    result = press(client, auth, cid, form, text=" \n ")
    assert "内容为空" in result["message"]
    assert not runtime[2].list_sessions()


def test_recent_is_only_ten_and_excludes_other_workspace(
    client, auth, cid, temp_root, runtime, monkeypatch
):
    _, _, sessions, _ = runtime
    rows = [
        {"id": str(uuid4()), "name": f"test {i}", "updated_at": i, "cwd": str(temp_root)}
        for i in range(15)
    ]
    rows.append(
        {
            "id": str(uuid4()),
            "name": "other workspace",
            "updated_at": 1000,
            "cwd": str(temp_root / "other"),
        }
    )
    rows.append(
        {
            "id": str(uuid4()),
            "name": "shadow",
            "updated_at": 1001,
            "cwd": str(temp_root),
            "runtime_shadow": True,
        }
    )
    monkeypatch.setattr(sessions, "list_sessions_snapshot", lambda: (rows, 1))
    recent = press(client, auth, cid, home(client, auth, cid), "最近会话")
    assert len(recent["labels"]) == 13  # 10 sessions and 3 navigation options.
    assert "test 14" in recent["labels"][0]
    assert all("other workspace" not in s and "shadow" not in s for s in recent["labels"])
    assert not any("搜索" in s or "下一页" in s for s in recent["labels"])


def test_action_scope_cannot_cross_clients_or_workspaces(client, auth, cid, temp_root, runtime):
    form = press(client, auth, cid, home(client, auth, cid), "新建")
    result = press(client, auth, str(uuid4()), form, text="must not send")
    assert "失效" in result["message"]
    from backend.workspaces import registry
    from urllib.parse import quote

    other = temp_root / "other"
    other.mkdir()
    registry.register(other)
    result = press(
        client,
        {**auth, "X-Muselab-Workspace": quote(str(other), safe="")},
        cid,
        form,
        text="must not send",
    )
    assert "失效" in result["message"]
    assert not runtime[2].list_sessions()


def test_tampered_action_and_mismatched_request_cannot_send(client, auth, cid, runtime):
    form = press(client, auth, cid, home(client, auth, cid), "新建")
    tampered = {**form, "action": form["action"] + "x"}
    assert "失效" in press(client, auth, cid, tampered, text="no")["message"]
    confirm = press(client, auth, cid, form, text="hello")
    confirm["request_id"] = str(uuid4())
    assert "编号不一致" in press(client, auth, cid, confirm)["message"]
    assert not runtime[2].list_sessions()


def test_reply_is_recent_snapshot_and_history_filters_tools(
    client, auth, cid, temp_root, runtime, monkeypatch
):
    _, chat, sessions, _ = runtime
    sessions.create_session(name="history fixture", cwd=temp_root)
    observed = []

    def history(session, **kwargs):
        observed.append(kwargs)
        return {
            "messages": [
                {"role": "user", "text": "first"},
                {"role": "assistant", "text": "a" * 1600, "timestamp": "2026-09-29T00:00:00Z"},
                {"role": "tool", "text": "tool-hidden"},
                {"role": "thinking", "text": "thinking-hidden"},
                {"role": "user", "text": "newer request"},
            ]
        }

    monkeypatch.setattr(chat, "get_session_api", history)
    selected = pick_session(client, auth, cid, "history fixture")
    reply = press(client, auth, cid, selected, "查看最近回复")
    assert "最近回复" in reply["detail"]
    assert "已完成" not in reply["message"]
    assert len(reply["detail"]) <= 700
    page = press(client, auth, cid, reply, "继续阅读")
    assert page["detail"] == "a" * 700
    dialogue = press(client, auth, cid, selected, "查看最近对话")
    assert "tool-hidden" not in dialogue["detail"]
    assert "thinking-hidden" not in dialogue["detail"]
    assert observed[0]["tail"] == 40 and observed[0]["full"] is False


def test_queue_withdraw_confirmation_removes_only_selected_item(
    client, auth, cid, temp_root, runtime
):
    _, _, sessions, _ = runtime
    sid = sessions.create_session(name="queue fixture", cwd=temp_root)["id"]
    for text in ["remove this", "keep this"]:
        assert (
            client.post(
                "/api/watch/messages",
                headers=auth,
                json={"request_id": str(uuid4()), "session_id": sid, "text": text},
            ).status_code
            == 202
        )
    selected = pick_session(client, auth, cid, "queue fixture")
    queue = press(client, auth, cid, selected, "排队消息")
    confirm = press(client, auth, cid, queue, "remove this")
    assert confirm["kind"] == "confirm"
    assert len(sessions.get_queue(sid)["items"]) == 2
    result = press(client, auth, cid, confirm)
    assert "撤回" in result["message"]
    assert [r["text"] for r in sessions.get_queue(sid)["items"]] == ["keep this"]
    again = press(client, auth, cid, confirm)
    assert "已开始执行或已撤回" in again["message"]


def test_stale_stop_never_interrupts_successor(client, auth, cid, temp_root, runtime, monkeypatch):
    _, chat, sessions, _ = runtime
    sid = sessions.create_session(name="stop fixture", cwd=temp_root)["id"]
    first = SimpleNamespace(done=False, turn_id=str(uuid4()))
    chat._active_turns[sid] = first
    selected = pick_session(client, auth, cid, "stop fixture")
    confirm = press(client, auth, cid, selected, "停止当前")
    assert confirm["kind"] == "confirm"
    successor = SimpleNamespace(done=False, turn_id=str(uuid4()))
    chat._active_turns[sid] = successor
    called = []

    async def interrupt(*args, **kwargs):
        called.append((args, kwargs))
        return {"stale": False}

    monkeypatch.setattr(chat, "interrupt", interrupt)
    result = press(client, auth, cid, confirm)
    assert "这一轮已结束" in result["message"]
    assert called == []
    selected = pick_session(client, auth, cid, "stop fixture")
    fresh = press(client, auth, cid, selected, "停止当前")
    press(client, auth, cid, fresh)
    assert called == [((sid,), {"turn_id": successor.turn_id})]
    chat._active_turns.clear()


def test_deleted_last_session_falls_back_and_client_state_is_independent(
    client, auth, cid, temp_root, runtime
):
    console, _, sessions, _ = runtime
    sid = sessions.create_session(name="remember fixture", cwd=temp_root)["id"]
    pick_session(client, auth, cid, "remember fixture")
    other_cid = str(uuid4())
    other = press(client, auth, other_cid, home(client, auth, other_cid), "继续上次")
    assert other["kind"] == "menu" and "最近会话" in other["message"]
    sessions.delete_session(sid)
    result = press(client, auth, cid, home(client, auth, cid), "继续上次")
    assert "暂无最近" in result["message"]


@pytest.mark.asyncio
async def test_disconnected_caller_keeps_context_after_admission(temp_root, runtime, monkeypatch):
    console, _, sessions, _ = runtime
    from backend import api_watch

    sid = sessions.create_session(cwd=temp_root)["id"]
    entered, release, persisted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def admission(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"session_id": sid}

    monkeypatch.setattr(api_watch, "send_message", admission)
    original = console._remember
    loop = asyncio.get_running_loop()

    def remember(*args):
        original(*args)
        loop.call_soon_threadsafe(persisted.set)

    monkeypatch.setattr(console, "_remember", remember)
    cid = uuid4()
    request_id = uuid4()
    token = console._pack(
        console._scope(cid, temp_root), "send", sid=sid, text="test", request_id=str(request_id)
    )
    req = console.ActionRequest(client_id=cid, request_id=request_id, action=token)
    caller = asyncio.create_task(console.console_action(req, temp_root))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.wait_for(persisted.wait(), 5)
    assert console._last(console._scope(cid, temp_root)) == sid


def test_context_failure_after_admission_still_reports_accepted(
    client, auth, cid, runtime, monkeypatch
):
    console, _, sessions, kicks = runtime
    form = press(client, auth, cid, home(client, auth, cid), "新建")
    confirm = press(client, auth, cid, form, text="test admission")

    def disk_full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(console, "_remember", disk_full)
    result = press(client, auth, cid, confirm)
    assert "已接受并排队" in result["message"]
    assert len(sessions.list_sessions()) == 1
    assert len(kicks) == 1


def test_symlinked_private_context_never_reads_or_writes_target(
    client, auth, cid, temp_root, runtime, tmp_path
):
    _, _, sessions, _ = runtime
    target = tmp_path / "other-context"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("unchanged")
    (sessions.SESS_DIR / ".watch-console").symlink_to(target, target_is_directory=True)
    result = press(client, auth, cid, home(client, auth, cid), "继续上次")
    assert "操作未完成" in result["message"]
    assert sentinel.read_text() == "unchanged"
    assert list(target.iterdir()) == [sentinel]


def test_reply_can_continue_two_turns_without_switching_session(
    client, auth, cid, temp_root, runtime, monkeypatch
):
    _, chat, sessions, kicks = runtime
    sid = sessions.create_session(name="dialogue target", cwd=temp_root)["id"]
    other = sessions.create_session(name="other session", cwd=temp_root)["id"]
    monkeypatch.setattr(
        chat,
        "get_session_api",
        lambda session, **kwargs: {"messages": [{"role": "assistant", "text": "fixture reply"}]},
    )
    selected = pick_session(client, auth, cid, "dialogue target")
    for text in ["first followup", "second followup"]:
        reply = press(client, auth, cid, selected, "查看最近回复")
        assert "fixture reply" in reply["detail"]
        form = press(client, auth, cid, reply, "继续对话")
        assert form["kind"] == "input"
        confirm = press(client, auth, cid, form, text=text)
        ack = press(client, auth, cid, confirm)
        assert "消息已接受并排队" in ack["message"]
        selected = press(client, auth, cid, ack)
        assert selected["kind"] == "menu"
        assert "dialogue target" in selected["message"]
    assert [row["text"] for row in sessions.get_queue(sid)["items"]] == [
        "first followup",
        "second followup",
    ]
    assert sessions.get_queue(other)["items"] == []
    assert len(sessions.list_sessions()) == 2
    assert kicks == [sid, sid]
