"""Small authenticated adapters for Apple Watch Shortcuts.

Admission uses the ordinary durable queue and its submission receipts. Shortcuts
never wait for inference, interrupt a running turn, or edit canonical history.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from . import chat, sessions as sess
from .auth import require_token
from .settings import ROOT
from .workspaces import resolve_workspace_root


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/api/watch",
    tags=["watch"],
    dependencies=[Depends(require_token), Depends(_no_cache)],
)
_NEW_SESSION_NAMESPACE = UUID("78e286f5-426a-4449-bf1b-85458d0a97bf")
_REPLY_LIMIT = 800


def _title(meta: dict) -> str:
    return " ".join(str(meta.get("name") or "未命名会话").split())[:48]


def _in_workspace(meta: dict, workspace: Path) -> bool:
    return Path(meta.get("cwd") or ROOT).resolve() == workspace


def _owned_meta(sid: str, workspace: Path) -> dict:
    meta = sess.get_session_meta(sid)
    if meta is None or not _in_workspace(meta, workspace) or meta.get("runtime_shadow"):
        raise HTTPException(404, "session not found in selected workspace")
    return meta


@router.get("/sessions")
def recent_sessions(
    limit: int = Query(10, ge=1, le=20),
    include_new: bool = Query(True),
    workspace: Path = Depends(resolve_workspace_root),
) -> dict:
    rows, _ = sess.list_sessions_snapshot()
    rows = [row for row in rows if _in_workspace(row, workspace) and not row.get("runtime_shadow")]
    # The desktop list is pinned-first. A watch picker is strictly recent.
    rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    labels = ["00 · 新建会话"] if include_new else []
    choices = {"00": "new"} if include_new else {}
    for row in rows[:limit]:
        try:
            sid = str(UUID(row["id"]))
        except (ValueError, KeyError, TypeError):
            continue
        key = f"{len(choices) + (0 if include_new else 1):02d}"
        labels.append(f"{key} · {_title(row)}")
        # Shortcuts treats dots in dictionary keys as key paths. Numeric keys
        # also keep duplicate titles, emoji and newlines unambiguous.
        choices[key] = sid
    return {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "labels": labels,
        "choices": choices,
    }


class WatchMessage(BaseModel):
    request_id: UUID
    session_id: str
    text: str = Field(min_length=1, max_length=6000)

    @field_validator("session_id")
    @classmethod
    def validate_session(cls, value: str) -> str:
        return "new" if value == "new" else str(UUID(value))

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain text")
        return value.strip()


async def _admit(req: WatchMessage, workspace: Path) -> dict:
    request_id = str(req.request_id)
    sid = (
        str(uuid5(_NEW_SESSION_NAMESPACE, request_id))
        if req.session_id == "new"
        else req.session_id
    )
    if req.session_id == "new":
        # Do not call the desktop create endpoint: its open-tab pruning would
        # mistake another browser's blank draft for an abandoned scratch tab.
        meta = await asyncio.to_thread(
            sess.register_session,
            sid,
            model=chat._resolve_default_model(allow_fallback=False),
            permission="default",
            cwd=workspace,
            auto_named=True,
        )
        if not _in_workspace(meta, workspace):
            raise HTTPException(409, "request_id belongs to another workspace")
    meta = await asyncio.to_thread(_owned_meta, sid, workspace)

    bg = BackgroundTasks()

    async def enqueue() -> dict:
        permission = chat._validate_permission(meta.get("permission") or "default")
        return await chat._enqueue_impl(
            sid,
            chat.QueueEnqueueReq(
                text=req.text,
                client_message_id=request_id,
                permission=permission,
                delivery="queue",
            ),
            bg,
        )

    # Fingerprint the caller's stable watch request, not derived session
    # settings. Replays remain identical after the session permission changes.
    result = await chat._run_submission(
        sid,
        "queue",
        request_id,
        req.model_dump(mode="json"),
        enqueue,
    )
    if result.get("cancelled"):
        raise HTTPException(409, "submission cancelled")
    # Kick the existing loop-owned scheduler before returning the ACK. This
    # callback must run even if the watch drops the HTTP response.
    await bg()
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "session_id": sid,
        "item_id": (result.get("item") or {}).get("id", ""),
        "status": "accepted",
        "message": f"已接受并排队：{_title(meta)}。稍后运行「MuseLab 最近回复」查看；需要授权时请打开 MuseLab。",
    }


def _consume_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


@router.post("/messages", status_code=202)
async def send_message(
    req: WatchMessage,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict:
    # The application lifecycle owns this task; a disconnected caller does not
    # cancel the create/admit/kick transaction. No prompt is logged on failure.
    task = asyncio.create_task(_admit(req, workspace))
    chat._retain_maintenance_task(task)
    task.add_done_callback(_consume_exception)
    return await asyncio.shield(task)


@router.get("/sessions/{sid}/submissions/{request_id}")
async def message_receipt(
    sid: UUID,
    request_id: UUID,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict:
    await asyncio.to_thread(_owned_meta, str(sid), workspace)
    receipt = await chat.submission_status(str(sid), str(request_id), kind="queue")
    return {
        "request_id": str(request_id),
        "session_id": str(sid),
        "state": receipt["state"],
    }


@router.get("/sessions/{sid}/reply")
async def latest_reply(
    sid: UUID,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict:
    session_id = str(sid)
    meta = await asyncio.to_thread(_owned_meta, session_id, workspace)
    data = await asyncio.to_thread(
        chat.get_session_api,
        session_id,
        full=False,
        tail=40,
        offset=-1,
        limit=0,
        history_generation="",
        around_uuid="",
        before=0,
        after=0,
    )
    reply = next(
        (
            str(row["text"]).strip()
            for row in reversed(data.get("messages") or [])
            if row.get("role") == "assistant" and str(row.get("text") or "").strip()
        ),
        "",
    )
    truncated = len(reply) > _REPLY_LIMIT
    reply = reply[:_REPLY_LIMIT] + ("…" if truncated else "")
    queue = await asyncio.to_thread(sess.get_queue, session_id)
    pending = len(queue.get("items") or [])
    busy = chat._session_runtime_busy(session_id) or bool(queue.get("inflight"))
    status = "执行中" if busy else "等待执行" if pending else "会话空闲"
    # This is a session snapshot, not proof that one particular request ran.
    message = f"{_title(meta)}\n{status}（排队 {pending} 条）\n\n"
    message += reply or "最近消息中暂无文字回复，请在 MuseLab 查看详情。"
    return {
        "protocol_version": 1,
        "session_id": session_id,
        "status": status,
        "queued": pending,
        "reply": reply,
        "truncated": truncated,
        "message": message,
    }
