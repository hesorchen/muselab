"""Authenticated task delivery, workspace identity and guarded SDK rewind."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import weakref
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_token
from . import file_checkpoints, runtime_identity, sessions as sess, task_delivery
from .workspaces import registry

router = APIRouter(
    prefix="/api/chat/sessions", tags=["delivery"], dependencies=[Depends(require_token)]
)
RESTORING: dict[str, str] = {}
_RUNTIME_GENERATIONS = weakref.WeakKeyDictionary()


def owned_workspace(sid: str) -> tuple[dict, Path]:
    from .settings import ROOT

    try:
        task_delivery.path(sid)
    except ValueError:
        raise HTTPException(400, "invalid_session_id") from None
    meta = sess.get_session_meta(sid)
    if meta is None:
        raise HTTPException(404, "session_not_found")
    try:
        # Unlike legacy read helpers, never silently fall back to another cwd.
        workspace = registry.resolve(meta.get("cwd") or str(ROOT))
        runtime_identity.directory_identity(workspace)
    except (OSError, ValueError):
        raise HTTPException(409, "session_workspace_unavailable") from None
    return meta, workspace


def workspaces_overlap(first: Path | str, second: Path | str) -> bool:
    left, right = Path(first).resolve(), Path(second).resolve()
    return left == right or left in right.parents or right in left.parents


def assert_not_restoring(sid: str) -> None:
    if not RESTORING:
        return
    _, cwd = owned_workspace(sid)
    if sid in RESTORING or any(
        workspaces_overlap(cwd, restoring) for restoring in RESTORING.values()
    ):
        raise HTTPException(409, "workspace_file_restore_in_progress")


def _runtime_marker(sid: str, meta: dict) -> str:
    from . import chat

    clients = []
    for key, value in chat._clients.items():
        if key[0] == sid:
            generation = _RUNTIME_GENERATIONS.setdefault(value, secrets.token_hex(16))
            clients.append((str(key), generation))
    clients.sort()
    metadata = {
        k: meta.get(k)
        for k in (
            "cwd",
            "model",
            "effort",
            "service_tier",
            "runtime_profile",
            "permission",
            "plan_return_permission",
        )
    }
    return hashlib.sha256(json.dumps([clients, metadata], sort_keys=True).encode()).hexdigest()


async def _idle(sid: str, cwd: Path) -> None:
    from . import chat

    if sess.session_is_deleting(sid) or chat._session_runtime_busy(sid):
        raise HTTPException(409, "session_is_busy")
    queue = await asyncio.to_thread(sess.get_queue, sid)
    if queue.get("items") or queue.get("inflight"):
        raise HTTPException(409, "queued_messages_remain")
    if sid in chat._pending_runtime_rebuilds:
        raise HTTPException(409, "runtime_rebuild_pending")
    if chat._sdk_cron_jobs.get(sid):
        raise HTTPException(409, "native_scheduled_tasks_must_be_removed_before_restore")
    # A foreground result can finish before detached writers or scheduled
    # deliveries do. Use the runtime's authoritative busy predicate for every
    # known session owner instead of equating a completed turn with idleness.
    candidates = (
        set(chat._active_turns)
        | set(chat._sessions_with_inflight_tasks)
        | set(chat._task_watchers)
        | {key[0] for key in chat._sdk_deliveries}
        | set(chat._sdk_cron_jobs)
        | set(RESTORING)
    )
    for other in candidates - {sid}:
        if not (chat._session_runtime_busy(other) or chat._sdk_cron_jobs.get(other)):
            continue
        try:
            _, other_cwd = await asyncio.to_thread(owned_workspace, other)
        except HTTPException:
            raise HTTPException(409, "active_workspace_unavailable") from None
        if workspaces_overlap(other_cwd, cwd):
            raise HTTPException(409, "another_session_is_using_workspace")


@router.get("/{sid}/runtime")
async def runtime_api(sid: str, workspace: str = "", refresh: bool = False) -> dict:
    from . import chat, endpoints

    meta, cwd = await asyncio.to_thread(owned_workspace, sid)
    if workspace:
        try:
            cwd = await asyncio.to_thread(registry.resolve, workspace)
            await asyncio.to_thread(runtime_identity.directory_identity, cwd)
        except (OSError, ValueError):
            raise HTTPException(409, "surface_workspace_unavailable") from None
    data = await asyncio.to_thread(runtime_identity.inspect_workspace, cwd, refresh=refresh)
    data["session_id"] = sid
    data["model"] = meta.get("model")
    data["backend"] = (
        "DUCC" if endpoints.is_ducc_model(str(meta.get("model") or "")) else "Claude Agent SDK"
    )
    data["runtime_connected"] = any(k[0] == sid for k in chat._clients)
    data["service_source"] = "authenticated_muselab_backend"
    if workspace:
        data["backend"] = "MuseLab workspace service"
        data["model"] = None
    data["busy"] = chat._session_runtime_busy(sid)
    return data


@router.get("/{sid}/delivery")
async def delivery_api(sid: str, turn_id: str = Query("", max_length=128)) -> dict:
    _, cwd = await asyncio.to_thread(owned_workspace, sid)
    result = await asyncio.to_thread(task_delivery.report, sid, cwd, turn_id)
    result["checkpoints"] = await asyncio.to_thread(file_checkpoints.list_checkpoints, sid)
    result["runtime"] = await runtime_api(sid, refresh=True)
    return result


@router.get("/{sid}/checkpoints/{checkpoint_id}/preview")
async def preview_api(sid: str, checkpoint_id: str) -> dict:
    from . import chat

    meta, cwd = await asyncio.to_thread(owned_workspace, sid)
    async with chat._session_runtime_lock_for(sid):
        await _idle(sid, cwd)
        try:
            return await asyncio.to_thread(
                file_checkpoints.preview, sid, checkpoint_id, cwd, _runtime_marker(sid, meta)
            )
        except (OSError, ValueError):
            raise HTTPException(409, "checkpoint_preview_unavailable") from None


class RestoreRequest(BaseModel):
    token: str = Field(min_length=20, max_length=128)
    confirmed: bool = False


@router.post("/{sid}/checkpoints/{checkpoint_id}/restore")
async def restore_api(sid: str, checkpoint_id: str, request: RestoreRequest) -> dict:
    from . import chat

    if not request.confirmed:
        raise HTTPException(400, "explicit_confirmation_required")
    meta, cwd = await asyncio.to_thread(owned_workspace, sid)
    async with chat._session_runtime_lock_for(sid):
        await _idle(sid, cwd)
        async with chat._lock:
            assert_not_restoring(sid)
            await _idle(sid, cwd)
            RESTORING[sid] = str(cwd)
        prepared = None
        try:
            latest, latest_cwd = await asyncio.to_thread(owned_workspace, sid)
            if latest_cwd != cwd or latest != meta:
                raise HTTPException(409, "session_changed_since_preview")
            marker = _runtime_marker(sid, meta)
            # No model query is sent: connect/resume only restores the same SDK
            # session's checkpoint state before the public control operation.
            prior_clients = {key: value for key, value in chat._clients.items() if key[0] == sid}
            client = await chat.get_client(
                sid,
                str(meta.get("model") or chat.MODEL),
                str(meta.get("permission") or "default"),
                effort=str(meta.get("effort") or ""),
                service_tier=str(meta.get("service_tier") or ""),
                plan_return_permission=str(meta.get("plan_return_permission") or ""),
            )
            if prior_clients and _runtime_marker(sid, meta) != marker:
                raise HTTPException(409, "runtime_changed_since_preview")
            prepared = await asyncio.to_thread(
                file_checkpoints.prepare_restore, sid, checkpoint_id, request.token, cwd, marker
            )
            async with asyncio.timeout(20):
                await client.rewind_files(checkpoint_id)
            return await asyncio.to_thread(file_checkpoints.verify_restore, cwd, prepared)
        except HTTPException:
            raise
        except (OSError, ValueError):
            raise HTTPException(409, "checkpoint_or_files_changed_refresh_preview") from None
        except Exception:
            if prepared is not None:
                chat._pending_runtime_rebuilds.add(sid)
                raise HTTPException(
                    502,
                    {"code": "restore_outcome_uncertain", "recovery_id": prepared["recovery_id"]},
                ) from None
            raise HTTPException(502, "checkpoint_runtime_unavailable") from None
        finally:
            RESTORING.pop(sid, None)
