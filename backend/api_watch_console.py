"""Versioned menu protocol for one self-contained iPhone/Watch Shortcut."""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import api_watch as watch, chat, sessions as sess
from .auth import require_token
from .private_storage import (
    UnsafePrivatePath,
    ensure_private_directory,
    ensure_private_regular_file,
)
from .settings import TOKEN
from .workspaces import resolve_workspace_root

router = APIRouter(
    prefix="/api/watch",
    tags=["watch"],
    dependencies=[Depends(require_token), Depends(watch._no_cache)],
)
_SIGN_KEY = hmac.digest(TOKEN.encode(), b"muselab-watch-console-v2", "sha256")
_DB_LOCK = threading.Lock()
_TOKEN_TTL = 1800
_PAGE_SIZE = 700


class ActionRequest(BaseModel):
    client_id: UUID
    request_id: UUID
    action: str = Field(min_length=1, max_length=32768)
    text: str = Field(default="", max_length=6000)


def _scope(client_id: UUID, workspace: Path) -> str:
    return hmac.new(_SIGN_KEY, f"{client_id}:{workspace}".encode(), hashlib.sha256).hexdigest()


def _pack(scope: str, op: str, **args) -> str:
    raw = json.dumps(
        {"scope": scope, "op": op, "expires": int(time.time()) + _TOKEN_TTL, **args},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(_SIGN_KEY, data.encode(), hashlib.sha256).hexdigest()
    return data + "." + signature


def _unpack(token: str, scope: str) -> dict:
    try:
        data, signature = token.rsplit(".", 1)
        expected = hmac.new(_SIGN_KEY, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if value["scope"] != scope or value["expires"] < time.time():
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(409, "菜单已失效，请返回主菜单重新选择。") from None


@contextmanager
def _context_db(*, readonly: bool = False):
    base = sess.SESS_DIR / ".watch-console"
    path = base / "context.sqlite3"
    if readonly:
        if not ensure_private_directory(base, create=False) or not ensure_private_regular_file(
            path
        ):
            yield None
            return
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
    else:
        ensure_private_directory(base)
        if not ensure_private_regular_file(path):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        ensure_private_regular_file(path)
        connection = sqlite3.connect(path, timeout=10)
    try:
        with _DB_LOCK:
            if not readonly:
                connection.execute("""CREATE TABLE IF NOT EXISTS contexts (
                    scope TEXT PRIMARY KEY, sid TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '')""")
        yield connection
        if not readonly:
            connection.commit()
    finally:
        connection.close()


def _last(scope: str) -> str:
    with _context_db(readonly=True) as db:
        if db is None:
            return ""
        row = db.execute("SELECT sid FROM contexts WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else ""


def _remember(scope: str, sid: str, request_id: str = "") -> None:
    with _context_db() as db:
        db.execute(
            """INSERT INTO contexts(scope,sid,request_id) VALUES(?,?,?)
                      ON CONFLICT(scope) DO UPDATE SET sid=excluded.sid,
                      request_id=CASE WHEN excluded.request_id='' AND contexts.sid=excluded.sid
                      THEN contexts.request_id ELSE excluded.request_id END""",
            (scope, sid, request_id),
        )


def _view(
    kind: str,
    message: str,
    *,
    detail: str = "",
    action: str = "",
    labels: list | None = None,
    choices: dict | None = None,
    request_id: str = "",
) -> dict:
    return {
        "protocol_version": 2,
        "kind": kind,
        "message": message,
        "detail": detail,
        "action": action,
        "labels": labels or [],
        "choices": choices or {},
        "request_id": request_id or str(uuid4()),
    }


def _menu(scope: str, title: str, entries: list[tuple[str, str, dict]], detail: str = "") -> dict:
    labels, choices = [], {}
    for i, (label, op, args) in enumerate(entries, 1):
        key = f"{i:02d}"
        icons = {
            "continue": "🎙️",
            "input": "🎙️",
            "recent": "🗂️",
            "last.reply": "📖",
            "reply": "📖",
            "history": "💬",
            "session": "💬",
            "queue": "⏳",
            "remove.preview": "🗑️",
            "stop.preview": "⏸️",
            "home": "🏠",
            "exit": "⏹️",
        }
        icon = icons.get(op, "💬")
        if op == "input" and args.get("sid") == "new":
            icon = "➕"
        elif label.startswith("刷新"):
            icon = "🔄"
        elif label == "继续阅读":
            icon = "📄"
        elif label.startswith("返回"):
            icon = "↩️"
        labels.append(f"{key} · {icon} {label}")
        choices[key] = _pack(scope, op, **args)
    return _view("menu", title, detail=detail, labels=labels, choices=choices)


def _message(scope: str, message: str, sid: str = "") -> dict:
    return _view(
        "message",
        message,
        action=_pack(scope, "session" if sid else "home", **({"sid": sid} if sid else {})),
    )


def _home(scope: str) -> dict:
    return _menu(
        scope,
        "MuseLab",
        [
            ("新建并发送", "input", {"sid": "new"}),
            ("继续上次会话", "continue", {}),
            ("最近会话", "recent", {}),
            ("查看最近回复", "last.reply", {}),
            ("退出", "exit", {}),
        ],
    )


def _status(sid: str, queue: dict) -> str:
    if chat._session_runtime_busy(sid) or queue.get("inflight"):
        return "执行中"
    return "等待执行" if queue.get("items") else "会话空闲"


async def _recent(scope: str, workspace: Path) -> dict:
    rows, _ = await asyncio.to_thread(sess.list_sessions_snapshot)
    rows = [r for r in rows if watch._in_workspace(r, workspace) and not r.get("runtime_shadow")]
    rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
    entries = []
    for row in rows:
        try:
            sid = str(UUID(row["id"]))
        except (ValueError, KeyError, TypeError):
            continue
        queue = await asyncio.to_thread(sess.get_queue, sid)
        entries.append((f"{watch._title(row)} · {_status(sid, queue)}", "session", {"sid": sid}))
        if len(entries) == 10:
            break
    return _menu(
        scope,
        "最近会话" if entries else "暂无最近会话",
        entries
        + [
            ("新建并发送", "input", {"sid": "new"}),
            ("返回主菜单", "home", {}),
            ("退出", "exit", {}),
        ],
    )


async def _session(scope: str, sid: str, workspace: Path) -> dict:
    meta = await asyncio.to_thread(watch._owned_meta, sid, workspace)
    queue = await asyncio.to_thread(sess.get_queue, sid)
    await asyncio.to_thread(_remember, scope, sid)
    pending = len(queue.get("items") or [])
    entries = [
        ("发送消息", "input", {"sid": sid}),
        ("查看最近回复", "reply", {"sid": sid}),
        ("查看最近对话", "history", {"sid": sid}),
        (f"排队消息（{pending}）", "queue", {"sid": sid}),
    ]
    turn = chat._active_turns.get(sid)
    if turn is not None and not turn.done:
        entries.append(("停止当前执行", "stop.preview", {"sid": sid}))
    entries += [("返回最近会话", "recent", {}), ("返回主菜单", "home", {}), ("退出", "exit", {})]
    return _menu(scope, f"{watch._title(meta)}\n{_status(sid, queue)} · 排队 {pending} 条", entries)


async def _history(sid: str) -> list[dict]:
    data = await asyncio.to_thread(
        chat.get_session_api,
        sid,
        full=False,
        tail=40,
        offset=-1,
        limit=0,
        history_generation="",
        around_uuid="",
        before=0,
        after=0,
    )
    return [
        r
        for r in data.get("messages", [])
        if r.get("role") in {"user", "assistant"} and str(r.get("text") or "").strip()
    ]


def _row_time(row: dict) -> str:
    value = row.get("timestamp") or row.get("ts") or row.get("created_at")
    try:
        stamp = (
            datetime.fromtimestamp(float(value))
            if isinstance(value, (int, float))
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return stamp.astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return "时间未提供"


async def _read(scope: str, sid: str, workspace: Path, op: str, offset: int = 0) -> dict:
    meta = await asyncio.to_thread(watch._owned_meta, sid, workspace)
    rows = await _history(sid)
    if op == "reply":
        reply = next((r for r in reversed(rows) if r.get("role") == "assistant"), None)
        content = f"最近回复 · {_row_time(reply)}\n\n{reply['text']}" if reply else "暂无文字回复。"
    else:
        content = (
            "\n\n".join(
                f"{'你' if r['role'] == 'user' else 'MuseLab'} · {_row_time(r)}\n{r['text']}"
                for r in rows[-6:]
            )
            or "暂无文字对话。"
        )
    offset = max(0, min(offset, len(content)))
    detail = content[offset : offset + _PAGE_SIZE]
    entries = []
    if offset + _PAGE_SIZE < len(content):
        entries.append(("继续阅读", op, {"sid": sid, "offset": offset + _PAGE_SIZE}))
    entries += [
        ("继续对话", "input", {"sid": sid}),
        ("刷新回复" if op == "reply" else "刷新对话", op, {"sid": sid}),
        (
            "查看最近对话" if op == "reply" else "查看最近回复",
            "history" if op == "reply" else "reply",
            {"sid": sid},
        ),
        ("返回会话", "session", {"sid": sid}),
        ("返回主菜单", "home", {}),
        ("退出", "exit", {}),
    ]
    queue = await asyncio.to_thread(sess.get_queue, sid)
    return _menu(scope, f"{watch._title(meta)}\n{_status(sid, queue)}", entries, detail)


async def _queue(scope: str, sid: str, workspace: Path) -> dict:
    await asyncio.to_thread(watch._owned_meta, sid, workspace)
    queue = await asyncio.to_thread(sess.get_queue, sid)
    entries = [
        (
            " ".join(str(row.get("text") or "附件消息").split())[:60],
            "remove.preview",
            {"sid": sid, "item_id": row["id"]},
        )
        for row in (queue.get("items") or [])[:20]
    ]
    return _menu(
        scope,
        "选择要撤回的排队消息" if entries else "没有待执行消息",
        entries
        + [("返回会话", "session", {"sid": sid}), ("返回主菜单", "home", {}), ("退出", "exit", {})],
    )


async def _dispatch(req: ActionRequest, workspace: Path) -> dict:
    scope = _scope(req.client_id, workspace)
    claim = _unpack(req.action, scope)
    op = claim["op"]
    sid = claim.get("sid", "")
    if op == "exit":
        return _view("exit", "")
    if op == "home":
        return _home(scope)
    if op == "recent":
        return await _recent(scope, workspace)
    if op in {"continue", "last.reply"}:
        sid = await asyncio.to_thread(_last, scope)
        if not sid:
            return await _recent(scope, workspace)
        try:
            await asyncio.to_thread(watch._owned_meta, sid, workspace)
        except HTTPException:
            return await _recent(scope, workspace)
        op = "input" if op == "continue" else "reply"
    if op == "session":
        return await _session(scope, str(UUID(sid)), workspace)
    if op == "input":
        title = (
            "新建会话"
            if sid == "new"
            else watch._title(await asyncio.to_thread(watch._owned_meta, sid, workspace))
        )
        return _view(
            "input", f"{title}\n想对 MuseLab 说什么？", action=_pack(scope, "send.preview", sid=sid)
        )
    if op == "send.preview":
        text = req.text.strip()
        if not text:
            return _message(scope, "内容为空，没有发送消息。", "" if sid == "new" else sid)
        title = (
            "新建会话"
            if sid == "new"
            else watch._title(await asyncio.to_thread(watch._owned_meta, sid, workspace))
        )
        request_id = str(uuid4())
        action = _pack(scope, "send", sid=sid, text=text, request_id=request_id)
        return _view(
            "confirm",
            f"确认发送到「{title}」？\n\n{text[:800]}"
            + ("\n（显示前 800 字，发送完整内容）" if len(text) > 800 else ""),
            action=action,
            request_id=request_id,
        )
    if op == "send":
        if str(req.request_id) != claim["request_id"]:
            raise HTTPException(409, "发送编号不一致，请重新确认。")
        ack = await watch.send_message(
            watch.WatchMessage(request_id=req.request_id, session_id=sid, text=claim["text"]),
            workspace,
        )
        sid = ack["session_id"]
        try:
            await asyncio.to_thread(_remember, scope, sid, str(req.request_id))
        except (sqlite3.Error, OSError, ValueError, UnsafePrivatePath):
            return _message(
                scope, "消息已接受并排队，但无法保存上次会话。请从最近会话查看结果。", sid
            )
        return _message(
            scope,
            "消息已接受并排队。\n稍后选择「查看最近回复」查看结果。\n需要授权时，请在 MuseLab 网页处理。",
            sid,
        )
    if op in {"reply", "history"}:
        return await _read(scope, sid, workspace, op, int(claim.get("offset", 0)))
    if op == "queue":
        return await _queue(scope, sid, workspace)
    if op in {"remove.preview", "remove"}:
        await asyncio.to_thread(watch._owned_meta, sid, workspace)
        queue = await asyncio.to_thread(sess.get_queue, sid)
        item = next((r for r in queue.get("items", []) if r.get("id") == claim["item_id"]), None)
        if not item:
            return _message(scope, "这条消息已开始执行或已撤回，请刷新队列。", sid)
        if op == "remove.preview":
            return _view(
                "confirm",
                f"确认撤回这条排队消息？\n\n{str(item.get('text') or '附件消息')[:800]}",
                action=_pack(scope, "remove", sid=sid, item_id=claim["item_id"]),
            )
        await chat.remove_queue_item_api(sid, claim["item_id"])
        return _message(scope, "撤回请求已处理，请刷新队列查看。", sid)
    if op in {"stop.preview", "stop"}:
        meta = await asyncio.to_thread(watch._owned_meta, sid, workspace)
        turn = chat._active_turns.get(sid)
        if turn is None or turn.done or (op == "stop" and turn.turn_id != claim["turn_id"]):
            return _message(scope, "这一轮已结束，请刷新会话状态。", sid)
        if op == "stop.preview":
            return _view(
                "confirm",
                f"确认停止「{watch._title(meta)}」的当前执行？\n待执行消息仍保留，可在队列中撤回。",
                action=_pack(scope, "stop", sid=sid, turn_id=turn.turn_id),
            )
        result = await chat.interrupt(sid, turn_id=claim["turn_id"])
        return _message(
            scope,
            "这一轮已结束，请刷新会话状态。"
            if result.get("stale")
            else "已请求停止当前执行，请刷新会话状态。",
            sid,
        )
    raise HTTPException(400, "不支持的菜单操作，请返回主菜单。")


@router.get("/menu")
async def console_menu(client_id: UUID, workspace: Path = Depends(resolve_workspace_root)) -> dict:
    return _home(_scope(client_id, workspace))


async def _run_action(req: ActionRequest, workspace: Path) -> dict:
    try:
        return await _dispatch(req, workspace)
    except HTTPException as exc:
        messages = {
            404: "会话已不存在或不属于当前工作区，请重新选择。",
            425: "这条消息正在提交，请稍后查看会话状态。",
        }
        detail = messages.get(
            exc.status_code,
            str(exc.detail) if isinstance(exc.detail, str) else "操作未完成，请刷新后重试。",
        )
        return _message(_scope(req.client_id, workspace), detail)
    except (ValueError, sqlite3.Error, OSError, UnsafePrivatePath):
        return _message(_scope(req.client_id, workspace), "操作未完成，请返回主菜单重试。")


@router.post("/actions")
async def console_action(
    req: ActionRequest, workspace: Path = Depends(resolve_workspace_root)
) -> dict:
    # A dropped watch response must not cancel admission or context persistence.
    task = asyncio.create_task(_run_action(req, workspace))
    chat._retain_maintenance_task(task)
    task.add_done_callback(watch._consume_exception)
    return await asyncio.shield(task)
