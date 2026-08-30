"""Session fork and detached-runtime successor lifecycle.

Claude CLI JSONL remains the canonical, forkable conversation history.  This
module coordinates only MuseLab's lifecycle projections around an injected
transcript fork: index rows, annotations, queues, task-state sidecars, Activity
placement, rollover links, and runtime prewarming.  It imports neither the chat
composition root nor the Claude SDK; runtime operations are supplied through
callbacks configured by ``backend.chat``.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from fastapi import HTTPException

from . import chat_overlays


@dataclass(frozen=True)
class SuccessorHooks:
    sessions: Any
    activity: Any
    model_default: str
    root: Path
    is_chinese_locale: Callable[[], bool]
    normalize_effort: Callable[[str | None], str]
    validate_permission: Callable[[str], str]
    normalize_plan_return_permission: Callable[[str, Any], str]
    session_config_dir: Callable[..., Any]
    sdk_fork_session: Callable[..., Any]
    sdk_delete_session: Callable[..., Any]
    sdk_rename_session: Callable[..., Any]
    find_session_jsonl: Callable[[str], Path | None]
    jsonl_path_cache: dict[str, Path]
    purge_single_session_storage: Callable[[str], bool]
    copy_runtime_continuation_snapshots: Callable[..., int]
    shaped_ui_messages: Callable[[str, str, bool], list[dict]]
    parse_bg_launch: Callable[[str], dict | None]
    record_background_task_launch: Callable[..., bool]
    sessions_with_inflight_tasks: dict[str, set[str]]
    bg_task_tool_use_ids: dict[str, str]
    bg_task_descriptions: dict[str, str]
    active_turns: dict[str, Any]
    session_has_live_watcher: Callable[[str], bool]
    schedule_queue_drain: Callable[[str], Any]
    get_client: Callable[..., Any]
    session_runtime_lock_for: Callable[[str], Any]
    maintenance_tasks: set[asyncio.Task]
    runtime_fork_uuid_mapping: Callable[[str], dict[str, str]]
    sync_runtime_successor_postlude: Callable[[str], dict[str, int]]
    runtime_fork_boundary: Callable[[str, dict], str]
    backfill_runtime_task_overlays: Callable[[str], None]
    commit_fork_lifecycle: Callable[..., dict[str, Any]]
    continue_detached_runtime_locked: Callable[[str], Any]
    continue_detached_runtime: Callable[[str], Any]
    prepare_detached_successor_runtime: Callable[[str], Any]
    runtime_rollover_lock_for: Callable[[str], Any]
    session_title_lock: Callable[[str], Any]


_hooks: SuccessorHooks | None = None


def configure_hooks(hooks: SuccessorHooks) -> None:
    global _hooks
    _hooks = hooks


def _require_hooks() -> SuccessorHooks:
    if _hooks is None:
        raise RuntimeError("chat successor hooks are not configured")
    return _hooks


# Runtime-continuation delivery and successor publication must fence on the
# exact same per-session lock objects.
RUNTIME_ROLLOVER_LOCKS = chat_overlays.RUNTIME_CONTINUATION_FENCES
RUNTIME_PREWARM_TASKS: dict[str, asyncio.Task] = {}
SESSION_TITLE_LOCKS = tuple(threading.RLock() for _ in range(64))


def runtime_rollover_lock_for(session_id: str) -> asyncio.Lock:
    return RUNTIME_ROLLOVER_LOCKS.setdefault(session_id, asyncio.Lock())


@contextmanager
def session_title_lock(session_id: str):
    lock = SESSION_TITLE_LOCKS[hash(session_id) % len(SESSION_TITLE_LOCKS)]
    with lock:
        yield


def runtime_fork_uuid_mapping(child_sid: str) -> dict[str, str]:
    """Read the SDK fork's explicit old-to-new UUID backlinks."""
    path = _require_hooks().find_session_jsonl(child_sid)
    if path is None:
        return {}
    mapping: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                origin = entry.get("forkedFrom") or {}
                old_uuid = str(origin.get("messageUuid") or "")
                new_uuid = str(entry.get("uuid") or "")
                if old_uuid and new_uuid:
                    mapping[old_uuid] = new_uuid
    except OSError:
        return {}
    return mapping


def commit_fork_lifecycle(
    source_sid: str,
    source_meta: dict[str, Any],
    *,
    fork_child: Callable[[], Any] | None,
    register_kwargs: dict[str, Any],
    successor: bool,
    copy_runtime_overlays: bool = False,
) -> dict[str, Any]:
    """Commit every MuseLab projection for one canonical transcript fork."""
    hooks = _require_hooks()
    sess = hooks.sessions
    activity = hooks.activity
    existing_sid = (
        str(source_meta.get("runtime_successor") or "") if successor else ""
    )
    reused = bool(existing_sid)
    forked = None
    child_sid = existing_sid
    child_created = False
    queue_projected = False
    activity_projected = False
    linked = False
    try:
        if not child_sid:
            if fork_child is None:
                raise ValueError("successor transcript is unavailable")
            forked = fork_child()
            child_sid = str(forked.session_id)
            if not child_sid or child_sid == source_sid:
                raise ValueError("fork returned an invalid child session")
            child_created = True
            child_meta = sess.register_session(
                child_sid,
                runtime_shadow=True,
                **register_kwargs,
            )
        else:
            child_meta = sess.get_session_meta(child_sid)
            if child_meta is None:
                raise ValueError("successor session is unavailable")

        hooks.jsonl_path_cache.pop(child_sid, None)
        uuid_mapping = hooks.runtime_fork_uuid_mapping(child_sid)
        sess.copy_message_annotations(source_sid, child_sid, uuid_mapping)

        if successor:
            queue_move = sess.migrate_queue(source_sid, child_sid)
            queue_projected = True
        else:
            queue_move = {
                "migrated": 0,
                "source": sess.get_queue(source_sid),
                "target": sess.get_queue(child_sid),
            }

        if copy_runtime_overlays:
            sess.copy_runtime_task_overlays(source_sid, child_sid)

        activity.inherit_session(
            source_sid,
            child_sid,
            **({"successor": True} if successor else {}),
        )
        activity_projected = True

        if successor:
            def _link_if_source_live() -> bool:
                with sess.session_lifecycle_lock(source_sid):
                    if sess.session_is_deleting(source_sid):
                        return False
                    return sess.link_runtime_successor(source_sid, child_sid)

            if not _link_if_source_live():
                raise ValueError("successor link changed")
            linked = True
        elif not sess.publish_fork_child(child_sid):
            raise ValueError("fork child disappeared before publication")

        return {
            "child_sid": child_sid,
            "child_meta": sess.get_session_meta(child_sid) or child_meta,
            "forked": forked,
            "uuid_mapping": uuid_mapping,
            "queue_move": queue_move,
            "reused": reused,
        }
    except Exception:
        if not child_created:
            # Existing public successors remain durable repair targets.
            raise
        if linked:
            with suppress(Exception):
                sess.unlink_runtime_successor(source_sid, child_sid)
        if activity_projected:
            if successor:
                with suppress(Exception):
                    activity.inherit_session(
                        child_sid, source_sid, successor=True)
            else:
                with suppress(Exception):
                    activity.discard_session(child_sid)
        if queue_projected:
            with suppress(Exception):
                sess.migrate_queue(child_sid, source_sid)
        try:
            hooks.purge_single_session_storage(child_sid)
        except Exception:
            sess.delete_session(child_sid)
            with suppress(Exception):
                hooks.sdk_delete_session(
                    child_sid,
                    directory=str(sess.session_workspace(source_sid)),
                )
            child_path = hooks.find_session_jsonl(child_sid)
            if child_path is not None:
                child_path.unlink(missing_ok=True)
        raise


def fork_session(
    sid: str,
    *,
    up_to_message_id: str | None,
    title: str | None,
    activity_hidden: bool,
    runtime_profile: str,
) -> dict:
    """Create and publish an explicit point-in-time transcript branch."""
    hooks = _require_hooks()
    sess = hooks.sessions
    src_meta = sess.get_session_meta(sid)
    if src_meta is None:
        raise HTTPException(404, "session not found")
    active = hooks.active_turns.get(sid)
    if active is not None and not active.done:
        raise HTTPException(409, "cannot fork while a turn is active")

    background_tasks_pending = len(
        hooks.sessions_with_inflight_tasks.get(sid, ())
    )
    source_model = (src_meta.get("model") or hooks.model_default).strip()
    source_name = (src_meta.get("name") or "会话").strip()
    requested_title = (title or "").strip()
    new_name = requested_title or (
        f"{source_name} · {'分支' if hooks.is_chinese_locale() else 'Fork'}"
    )
    forked_count = (
        0 if up_to_message_id else int(src_meta.get("message_count") or 0)
    )
    forked_turns = (
        0 if up_to_message_id else int(src_meta.get("turn_count") or 0)
    )

    def _fork_explicit():
        with hooks.session_config_dir(source_model, sid=sid):
            return hooks.sdk_fork_session(
                sid,
                directory=str(sess.session_workspace(sid)),
                up_to_message_id=up_to_message_id,
                title=new_name,
            )

    try:
        lifecycle = hooks.commit_fork_lifecycle(
            sid,
            src_meta,
            fork_child=_fork_explicit,
            register_kwargs={
                "name": new_name,
                "model": src_meta.get("model") or hooks.model_default,
                "permission": src_meta.get("permission") or "",
                "plan_return_permission": src_meta.get("plan_return_permission"),
                "auto_named": False,
                "message_count": forked_count,
                "turn_count": forked_turns,
                "effort": hooks.normalize_effort(src_meta.get("effort")),
                "service_tier": src_meta.get("service_tier") or "",
                "thinking": src_meta.get("thinking") is not False,
                "forked_from": sid,
                "forked_from_name": source_name,
                "forked_from_message_id": up_to_message_id or "",
                "activity_hidden": activity_hidden,
                "runtime_profile": runtime_profile,
                "cwd": src_meta.get("cwd") or str(hooks.root),
            },
            successor=False,
        )
    except FileNotFoundError:
        raise HTTPException(404, "source transcript not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:
        sys.stderr.write(
            f"[chat] fork commit failed sid={sid[:8]} "
            f"exc={type(exc).__name__}\n"
        )
        sys.stderr.flush()
        raise HTTPException(500, "fork failed — see server log") from None

    new_sid = str(lifecycle["child_sid"])
    return {
        **lifecycle["child_meta"],
        "session_id": new_sid,
        "source_background_tasks_pending": background_tasks_pending,
    }


def sync_runtime_successor_postlude(source_sid: str) -> dict[str, int]:
    """Propagate late display metadata through every runtime successor."""
    hooks = _require_hooks()
    sess = hooks.sessions
    try:
        source_meta = sess.get_session_meta(source_sid)
    except Exception as exc:
        sys.stderr.write(
            f"[chat] runtime postlude source read failed "
            f"sid={source_sid[:8]} exc={type(exc).__name__}\n"
        )
        return {"annotations": 0, "renamed": 0}
    if source_meta is None:
        return {"annotations": 0, "renamed": 0}
    desired_name = str(source_meta.get("name") or "").strip()
    current_sid = source_sid
    propagate_name = bool(desired_name)
    seen: set[str] = set()
    copied = 0
    renamed = 0
    for _ in range(32):
        if not current_sid or current_sid in seen:
            break
        seen.add(current_sid)
        try:
            current_meta = sess.get_session_meta(current_sid) or {}
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime postlude lineage read failed "
                f"sid={current_sid[:8]} exc={type(exc).__name__}\n"
            )
            break
        child_sid = str(current_meta.get("runtime_successor") or "")
        if not child_sid or child_sid in seen:
            break
        try:
            child_meta = sess.get_session_meta(child_sid)
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime postlude child read failed "
                f"sid={child_sid[:8]} exc={type(exc).__name__}\n"
            )
            break
        if child_meta is None:
            break
        try:
            uuid_mapping = hooks.runtime_fork_uuid_mapping(child_sid)
            copied += sess.copy_message_annotations(
                current_sid, child_sid, uuid_mapping)
            hooks.copy_runtime_continuation_snapshots(
                current_sid, child_sid, uuid_mapping)
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime display postlude sync failed "
                f"sid={current_sid[:8]} child={child_sid[:8]} "
                f"exc={type(exc).__name__}\n"
            )

        if propagate_name:
            try:
                with hooks.session_title_lock(child_sid):
                    fresh_child = sess.get_session_meta(child_sid) or child_meta
                    child_name = str(fresh_child.get("name") or "").strip()
                    inherited_name = str(
                        fresh_child.get("forked_from_name") or ""
                    ).strip()
                    if child_name not in {desired_name, inherited_name}:
                        propagate_name = False
                    elif child_name != desired_name:
                        child_model = str(
                            fresh_child.get("model") or hooks.model_default
                        ).strip()
                        try:
                            with hooks.session_config_dir(
                                child_model, sid=child_sid
                            ):
                                hooks.sdk_rename_session(
                                    child_sid,
                                    desired_name,
                                    directory=str(
                                        sess.session_workspace(child_sid)
                                    ),
                                )
                        except (FileNotFoundError, ValueError):
                            pass
                        if sess.rename_session(child_sid, desired_name):
                            renamed += 1
            except Exception as exc:
                sys.stderr.write(
                    f"[chat] runtime title postlude sync failed "
                    f"sid={child_sid[:8]} exc={type(exc).__name__}\n"
                )
        current_sid = child_sid
    return {"annotations": copied, "renamed": renamed}


def runtime_fork_boundary(_sid: str, meta: dict) -> str:
    """Return the explicit canonical boundary; never infer a later tail."""
    return str(meta.get("runtime_boundary_message_id") or "")


def backfill_runtime_task_overlays(source_sid: str) -> None:
    """Recover running-card ownership for sessions predating rollover state."""
    hooks = _require_hooks()
    sess = hooks.sessions
    pending = set(hooks.sessions_with_inflight_tasks.get(source_sid, ()))
    if not pending:
        return
    meta = sess.get_session_meta(source_sid) or {}
    try:
        messages = hooks.shaped_ui_messages(
            source_sid,
            str(meta.get("model") or hooks.model_default),
            True,
        )
    except Exception:
        messages = []
    for message in messages:
        if message.get("role") != "tool_result":
            continue
        launch = hooks.parse_bg_launch(str(message.get("text") or ""))
        if not launch or launch.get("task_id") not in pending:
            continue
        hooks.record_background_task_launch(
            source_sid,
            str(launch["task_id"]),
            tool_use_id=str(message.get("id") or ""),
            output_file=launch.get("output_file"),
        )
    for task_id in pending:
        if task_id not in sess.get_runtime_task_overlays(source_sid):
            hooks.record_background_task_launch(
                source_sid,
                task_id,
                tool_use_id=hooks.bg_task_tool_use_ids.get(task_id),
                description=hooks.bg_task_descriptions.get(task_id),
            )


async def continue_detached_runtime_locked(source_sid: str) -> dict:
    """Commit one rollover; caller must hold the per-source lock."""
    hooks = _require_hooks()
    sess = hooks.sessions
    source_meta = await asyncio.to_thread(sess.get_session_meta, source_sid)
    if source_meta is None:
        raise HTTPException(404, "session not found")
    if sess.session_is_deleting(source_sid):
        raise HTTPException(409, "session is being deleted")
    existing_sid = str(source_meta.get("runtime_successor") or "")
    if existing_sid:
        try:
            lifecycle = await asyncio.to_thread(
                hooks.commit_fork_lifecycle,
                source_sid,
                source_meta,
                fork_child=None,
                register_kwargs={},
                successor=True,
                copy_runtime_overlays=True,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        await asyncio.to_thread(
            hooks.sync_runtime_successor_postlude, source_sid)
        child_meta = lifecycle["child_meta"]
        queue_move = lifecycle["queue_move"]
        if queue_move["target"].get("items"):
            hooks.schedule_queue_drain(existing_sid)
        inherited_overlays = await asyncio.to_thread(
            sess.get_runtime_task_overlays, existing_sid)
        return {
            **child_meta,
            "session_id": existing_sid,
            "source_session_id": source_sid,
            "owner_session_id": source_sid,
            "inherited_background_tasks_pending": sum(
                1 for overlay in inherited_overlays.values()
                if overlay.get("state") == "running"
            ),
            "queue_migrated": queue_move["migrated"],
            "queue_pending": len(queue_move["target"].get("items") or []),
            "reused": True,
        }

    active = hooks.active_turns.get(source_sid)
    if (
        active is not None
        and not active.done
        and not getattr(active, "is_continuation", False)
        and not getattr(active, "canonical_terminal_published", False)
    ):
        raise HTTPException(409, "cannot continue while a turn is active")
    background_pending = len(
        hooks.sessions_with_inflight_tasks.get(source_sid, ())
    )
    if not background_pending and not hooks.session_has_live_watcher(source_sid):
        return {
            **source_meta,
            "session_id": source_sid,
            "source_session_id": source_sid,
            "owner_session_id": source_sid,
            "inherited_background_tasks_pending": 0,
            "queue_migrated": 0,
            "reused": True,
        }

    boundary = await asyncio.to_thread(
        hooks.runtime_fork_boundary, source_sid, source_meta)
    if not boundary:
        raise HTTPException(409, "background turn boundary is unavailable")
    await asyncio.to_thread(hooks.backfill_runtime_task_overlays, source_sid)
    if sess.session_is_deleting(source_sid):
        raise HTTPException(409, "session is being deleted")
    source_model = str(
        source_meta.get("model") or hooks.model_default
    ).strip()
    source_name = str(source_meta.get("name") or "会话").strip()
    register_kwargs: dict[str, Any] = {
        "name": source_name,
        "model": source_model,
        "permission": source_meta.get("permission") or "",
        "plan_return_permission": source_meta.get("plan_return_permission"),
        "auto_named": False,
        "message_count": int(source_meta.get("message_count") or 0),
        "turn_count": int(source_meta.get("turn_count") or 0),
        "effort": hooks.normalize_effort(source_meta.get("effort")),
        "service_tier": source_meta.get("service_tier") or "",
        "thinking": source_meta.get("thinking") is not False,
        "forked_from": source_sid,
        "forked_from_name": source_name,
        "forked_from_message_id": boundary,
        "activity_hidden": bool(source_meta.get("activity_hidden", False)),
        "runtime_profile": source_meta.get("runtime_profile") or "",
        "runtime_predecessor": source_sid,
        "cwd": source_meta.get("cwd") or str(hooks.root),
    }

    def _fork_runtime():
        if sess.session_is_deleting(source_sid):
            raise ValueError("source session is being deleted")
        with hooks.session_config_dir(source_model, sid=source_sid):
            forked = hooks.sdk_fork_session(
                source_sid,
                directory=str(sess.session_workspace(source_sid)),
                up_to_message_id=boundary,
                title=source_name,
            )
        register_kwargs["runtime_fork_boundary_at"] = (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        return forked

    try:
        lifecycle = await asyncio.to_thread(
            hooks.commit_fork_lifecycle,
            source_sid,
            source_meta,
            fork_child=_fork_runtime,
            register_kwargs=register_kwargs,
            successor=True,
            copy_runtime_overlays=True,
        )
    except FileNotFoundError:
        raise HTTPException(404, "source transcript not found") from None
    except ValueError:
        raise HTTPException(409, "runtime rollover could not commit") from None
    except Exception as exc:
        sys.stderr.write(
            f"[chat] detached runtime fork failed sid={source_sid[:8]} "
            f"exc={type(exc).__name__}\n"
        )
        raise HTTPException(
            500, "runtime rollover could not commit"
        ) from None

    child_sid = str(lifecycle["child_sid"])
    child_meta = lifecycle["child_meta"]
    queue_move = lifecycle["queue_move"]
    try:
        await asyncio.to_thread(
            sess.copy_runtime_task_overlays, source_sid, child_sid)
    except Exception as exc:
        sys.stderr.write(
            f"[chat] runtime overlay post-link sync failed "
            f"sid={source_sid[:8]} child={child_sid[:8]} "
            f"exc={type(exc).__name__}\n"
        )
    await asyncio.to_thread(hooks.sync_runtime_successor_postlude, source_sid)
    if queue_move["target"].get("items"):
        hooks.schedule_queue_drain(child_sid)
    public_child = (
        await asyncio.to_thread(sess.get_session_meta, child_sid)
    ) or child_meta
    inherited_overlays = await asyncio.to_thread(
        sess.get_runtime_task_overlays, child_sid)
    return {
        **public_child,
        "session_id": child_sid,
        "source_session_id": source_sid,
        "owner_session_id": source_sid,
        "inherited_background_tasks_pending": sum(
            1 for overlay in inherited_overlays.values()
            if overlay.get("state") == "running"
        ),
        "queue_migrated": queue_move["migrated"],
        "queue_pending": len(queue_move["target"].get("items") or []),
        "reused": False,
    }


async def continue_detached_runtime(source_sid: str) -> dict:
    """Run rollover to commit or rollback even if its HTTP owner is cancelled."""
    hooks = _require_hooks()
    async with hooks.runtime_rollover_lock_for(source_sid):
        owner = asyncio.create_task(
            hooks.continue_detached_runtime_locked(source_sid))
        cancellation: asyncio.CancelledError | None = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as exc:
                cancellation = exc
        if cancellation is not None:
            with suppress(BaseException):
                owner.result()
            raise cancellation
        return owner.result()


async def prepare_detached_successor_runtime(source_sid: str) -> None:
    """Create and warm the interactive child of a background-owned runtime."""
    hooks = _require_hooks()
    sess = hooks.sessions
    try:
        successor = await hooks.continue_detached_runtime(source_sid)
        child_sid = str(successor.get("session_id") or "")
        if not child_sid or child_sid == source_sid:
            return
        child_meta = await asyncio.to_thread(sess.get_session_meta, child_sid)
        if (
            child_meta is None
            or sess.session_is_deleting(source_sid)
            or sess.session_is_deleting(child_sid)
        ):
            return

        model = str(
            child_meta.get("model") or hooks.model_default
        ).strip()
        permission = hooks.validate_permission(
            str(child_meta.get("permission") or ""))
        effort = hooks.normalize_effort(child_meta.get("effort"))
        service_tier = str(
            child_meta.get("service_tier") or ""
        ).strip()
        client_kwargs: dict[str, Any] = {
            "effort": effort,
            "service_tier": service_tier,
        }
        if permission == "plan":
            client_kwargs["plan_return_permission"] = (
                hooks.normalize_plan_return_permission(
                    permission, child_meta.get("plan_return_permission")
                )
            )

        async with hooks.session_runtime_lock_for(child_sid):
            active = hooks.active_turns.get(child_sid)
            if (
                active is not None and not active.done
                or sess.session_is_deleting(source_sid)
                or sess.session_is_deleting(child_sid)
            ):
                return
            await hooks.get_client(
                child_sid,
                model,
                permission,
                **client_kwargs,
            )
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        sys.stderr.write(
            f"[chat] detached runtime prewarm deferred "
            f"sid={source_sid[:8]} status={exc.status_code}\n"
        )
    except Exception as exc:
        sys.stderr.write(
            f"[chat] detached runtime prewarm failed "
            f"sid={source_sid[:8]} exc={type(exc).__name__}\n"
        )
    finally:
        sys.stderr.flush()


def schedule_detached_successor_prewarm(source_sid: str) -> None:
    """Retain one eager rollover/prewarm owner for ``source_sid``."""
    hooks = _require_hooks()
    existing = RUNTIME_PREWARM_TASKS.get(source_sid)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        hooks.prepare_detached_successor_runtime(source_sid))
    RUNTIME_PREWARM_TASKS[source_sid] = task
    hooks.maintenance_tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        hooks.maintenance_tasks.discard(done)
        if RUNTIME_PREWARM_TASKS.get(source_sid) is done:
            RUNTIME_PREWARM_TASKS.pop(source_sid, None)
        if done.cancelled():
            return
        with suppress(Exception):
            done.exception()

    task.add_done_callback(_done)
