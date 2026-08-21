"""Display-only chat overlays and durable runtime-continuation delivery.

Claude CLI JSONL remains the sole canonical/forkable conversation history.
This module owns only private presentation artifacts and imports no chat runtime;
runtime coordination is supplied through dynamic callbacks from ``backend.chat``.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import TYPE_CHECKING, Any, Callable
import uuid

from . import chat_history_window
from . import sessions as sess
from . import transcript_index as transcript_idx
from .settings import atomic_write_text

if TYPE_CHECKING:
    from .chat import TurnBroadcast


@dataclass(frozen=True)
class OverlayHooks:
    broadcast_to_ui_messages: Callable[[Any], list[dict]]
    ensure_transcript_index: Callable[[str], tuple[Path, dict] | None]
    turn_transcript_boundary: Callable[[str, str], tuple[Any, dict]]
    transcript_ts_ms: Callable[[dict], int | None]
    classify_stream_error: Callable[[Any], dict]
    indexed_ui_records: Callable[[Path, dict, list[int], dict], list[dict]]
    turn_uuids_from_boundary: Callable[..., tuple[str | None, str | None, bool]]
    delete_active_turn_sidecar: Callable[[str], None]
    turn_broadcast_factory: Callable[..., Any]
    interrupted_at_startup: dict[str, dict]
    persist_failed_turn_snapshot: Callable[..., bool]
    load_cancelled_turn_snapshots: Callable[[str], tuple[list[dict], str]]
    cancelled_snapshot_canonical_span: Callable[..., tuple[list[str], str]]
    persist_runtime_continuation_snapshot: Callable[[str, dict], bool]
    runtime_rollover_lock_for: Callable[[str], Any]
    session_runtime_lock_for: Callable[[str], Any]
    session_has_live_watcher: Callable[[str], bool]
    schedule_queue_drain: Callable[[str], Any]
    runtime_prewarm_tasks: dict[str, asyncio.Task]
    active_turns: dict[str, Any]
    maintenance_tasks: set[asyncio.Task]


_hooks: OverlayHooks | None = None


def configure_hooks(hooks: OverlayHooks) -> None:
    global _hooks
    _hooks = hooks


def _require_hooks() -> OverlayHooks:
    if _hooks is None:
        raise RuntimeError("chat overlay hooks are not configured")
    return _hooks


# Shared by chat deletion/drain code and this module's delivery scheduler.
RUNTIME_CONTINUATION_DELIVERY_TASKS: dict[tuple[str, str], asyncio.Task] = {}
# The runtime rollover lock registry stays behaviorally owned by chat.py; sharing
# this exact container preserves the existing per-session fence identities.
RUNTIME_CONTINUATION_FENCES: dict[str, asyncio.Lock] = {}

_CANCELLED_TURN_SNAPSHOT_SCHEMA = 1
_RUNTIME_CONTINUATION_OUTBOX_SCHEMA = 1
_RUNTIME_CONTINUATION_DISPLAY_KIND = "runtime_continuation"


def _canonical_uuid_component(value: str) -> str | None:
    """Return a filesystem-safe canonical UUID or ``None``."""
    try:
        parsed = uuid.UUID(str(value or ""))
    except (ValueError, AttributeError, TypeError):
        return None
    canonical = str(parsed)
    return canonical if canonical == str(value or "").lower() else None


def _cancelled_turn_session_dir(sid: str) -> Path | None:
    safe_sid = _canonical_uuid_component(sid)
    if safe_sid is None:
        return None
    return sess.SESS_DIR / "cancelled_turns" / safe_sid


def _cancelled_turn_snapshot_path(sid: str, turn_id: str) -> Path | None:
    directory = _cancelled_turn_session_dir(sid)
    safe_turn = _canonical_uuid_component(turn_id)
    if directory is None or safe_turn is None:
        return None
    return directory / f"{safe_turn}.json"


def _runtime_continuation_outbox_dir(source_sid: str) -> Path | None:
    safe_sid = _canonical_uuid_component(source_sid)
    if safe_sid is None:
        return None
    return sess.SESS_DIR / "runtime_continuation_outbox" / safe_sid


def _runtime_continuation_outbox_path(
    source_sid: str,
    event_id: str,
) -> Path | None:
    directory = _runtime_continuation_outbox_dir(source_sid)
    safe_event = _canonical_uuid_component(event_id)
    if directory is None or safe_event is None:
        return None
    return directory / f"{safe_event}.json"


def _delete_runtime_continuation_outboxes(source_sid: str) -> None:
    directory = _runtime_continuation_outbox_dir(source_sid)
    if directory is not None and directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def _load_runtime_continuation_outbox(
    source_sid: str,
    event_id: str,
) -> dict | None:
    path = _runtime_continuation_outbox_path(source_sid, event_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("outbox root must be an object")
        if data.get("schema") != _RUNTIME_CONTINUATION_OUTBOX_SCHEMA:
            raise ValueError("unsupported outbox schema")
        if data.get("source_sid") != source_sid:
            raise ValueError("outbox source mismatch")
        if data.get("event_id") != event_id:
            raise ValueError("outbox event mismatch")
        message = data.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("outbox message mismatch")
        if not isinstance(message.get("text"), str) or not message["text"].strip():
            raise ValueError("outbox message is empty")
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:
        # A corrupt/older-schema READY record must not pin every successor tab
        # forever.  Move it out of the ``*.json`` delivery namespace while
        # retaining the private artifact for local diagnosis.  The rename is
        # same-directory and therefore atomic; neither the filename nor the log
        # contains assistant prose.
        try:
            quarantine = path.with_name(f"{path.name}.invalid")
            if quarantine.exists():
                quarantine = path.with_name(
                    f"{path.name}.invalid.{uuid.uuid4().hex[:8]}"
                )
            os.replace(path, quarantine)
            with suppress(OSError):
                quarantine.chmod(0o600)
        except FileNotFoundError:
            pass
        except OSError:
            # A concurrent delete may own the directory.  The original error is
            # still reported below without exposing payload contents.
            pass
        # The payload contains private assistant text.  Log only stable local
        # identifiers and the exception class, never the content or a task
        # output path.
        sys.stderr.write(
            f"[chat] runtime continuation outbox skipped "
            f"sid={source_sid[:8]} event={event_id[:8]} "
            f"exc={type(exc).__name__}\n"
        )
        sys.stderr.flush()
        return None


def _runtime_continuation_outbox_entries() -> list[tuple[str, str]]:
    root = sess.SESS_DIR / "runtime_continuation_outbox"
    if not root.exists():
        return []
    entries: list[tuple[str, str]] = []
    try:
        source_dirs = list(root.iterdir())
    except OSError:
        return []
    for directory in source_dirs:
        source_sid = _canonical_uuid_component(directory.name)
        if source_sid is None or not directory.is_dir():
            continue
        try:
            paths = list(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            event_id = _canonical_uuid_component(path.stem)
            if event_id is not None:
                entries.append((source_sid, event_id))
    entries.sort()
    return entries


def _session_has_runtime_continuation_outbox(source_sid: str) -> bool:
    directory = _runtime_continuation_outbox_dir(source_sid)
    if directory is None or not directory.exists():
        return False
    try:
        return any(directory.glob("*.json"))
    except OSError:
        return False


def _runtime_continuation_outbox_event_ids(source_sid: str) -> list[str]:
    """Return validated READY ids for one owner without reading private text."""
    directory = _runtime_continuation_outbox_dir(source_sid)
    if directory is None or not directory.exists():
        return []
    try:
        event_ids = [
            event_id
            for path in directory.glob("*.json")
            if (event_id := _canonical_uuid_component(path.stem)) is not None
        ]
    except OSError:
        return []
    return sorted(set(event_ids))


def _runtime_lineage_has_ready_continuation(leaf_sid: str) -> bool:
    """Whether a visible leaf still has an ancestor READY projection."""
    lineage = sess.runtime_lineage(leaf_sid) or [leaf_sid]
    if not lineage or lineage[-1] != leaf_sid:
        return False
    return any(
        _runtime_continuation_outbox_event_ids(owner_sid)
        for owner_sid in lineage[:-1]
    )


def _persist_runtime_continuation_outbox(
    source_sid: str,
    broadcast: "TurnBroadcast",
    *,
    completed_at_ms: int,
    elapsed_s: float,
    terminal_status: str,
    incomplete_error: str = "",
) -> str:
    """Durably stage one hidden-runtime reply for its visible successor.

    The source transcript remains Claude's canonical model history.  This
    outbox contains only the final user-visible assistant prose and footer
    facts; it deliberately excludes thinking, tool protocol, task summaries,
    output paths, and the source AssistantMessage UUID.
    """
    _hooks = _require_hooks()
    # Write first, decide whether a successor needs the projection later while
    # actually holding the source rollover lock.  Merely sampling ``locked()``
    # here left a check-then-link gap: a fast continuation could decide it was
    # public, then a waiting rollover would fork at the earlier boundary and
    # hide the only durable reply.
    if terminal_status not in {"completed", "failed"}:
        return ""
    event_id = str(broadcast.turn_id or "")
    path = _runtime_continuation_outbox_path(source_sid, event_id)
    if path is None:
        return ""

    messages = _hooks.broadcast_to_ui_messages(broadcast)
    # A continuation can speak, use a tool, then speak again.  The successor
    # intentionally gets one simple Agent bubble, but it must retain every
    # user-visible prose segment in order rather than silently keeping only the
    # last one.  Thinking/tool protocol remains excluded.
    assistant_texts = [
        str(message.get("text") or "").strip()
        for message in messages
        if message.get("role") == "assistant"
        and str(message.get("text") or "").strip()
    ]
    text = "\n\n".join(assistant_texts)
    if incomplete_error:
        text = incomplete_error
    if not text.strip():
        return ""

    message = {
        "role": "assistant",
        "text": text,
        "model": broadcast.model,
        "ts": int(completed_at_ms),
        "elapsed": float(elapsed_s),
        "turn_status": terminal_status,
        "display_kind": _RUNTIME_CONTINUATION_DISPLAY_KIND,
        "runtime_event_id": event_id,
        "presentation_only": True,
        "forkable": False,
        "block_id": f"runtime-continuation:{event_id}:0:assistant",
        "_key": f"runtime-continuation:{event_id}:0:assistant",
    }
    payload = {
        "schema": _RUNTIME_CONTINUATION_OUTBOX_SCHEMA,
        "source_sid": source_sid,
        "event_id": event_id,
        "model": broadcast.model,
        "completed_at_ms": int(completed_at_ms),
        "terminal_status": terminal_status,
        "message": message,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            path.parent.parent.chmod(0o700)
        with suppress(OSError):
            path.parent.chmod(0o700)
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            mode=0o600,
        )
        with suppress(OSError):
            path.chmod(0o600)
        return event_id
    except Exception as exc:
        sys.stderr.write(
            f"[chat] runtime continuation outbox write failed "
            f"sid={source_sid[:8]} event={event_id[:8]} "
            f"exc={type(exc).__name__}\n"
        )
        sys.stderr.flush()
        return ""


def _sync_runtime_display_message_count(sid: str) -> None:
    """Refresh cached list counts after a presentation-only snapshot write."""
    _hooks = _require_hooks()
    snapshots, _ = _hooks.load_cancelled_turn_snapshots(sid)
    indexed = _hooks.ensure_transcript_index(sid)
    index = indexed[1] if indexed is not None else None
    total, turns = _interrupted_history_stats(index, snapshots, "normal")
    sess.bump_session(sid, message_count=total, turn_count=turns)


def _persist_runtime_continuation_snapshot(
    target_sid: str,
    outbox: dict,
) -> bool:
    """Project one staged reply under the target's deletion fence."""
    # ``asyncio.to_thread`` cancellation does not stop its worker.  Holding the
    # same lifecycle lock as purge means a writer that started first completes
    # before purge removes the directory, while a writer arriving after the
    # tombstone becomes a no-op and cannot recreate private text after DELETE.
    with sess.session_lifecycle_lock(target_sid):
        return _persist_runtime_continuation_snapshot_locked(target_sid, outbox)


def _persist_runtime_continuation_snapshot_locked(
    target_sid: str,
    outbox: dict,
) -> bool:
    """Project one staged reply into ``target_sid``'s virtual UI history."""
    _hooks = _require_hooks()
    event_id = str(outbox.get("event_id") or "")
    path = _cancelled_turn_snapshot_path(target_sid, event_id)
    if path is None or sess.session_is_deleting(target_sid):
        return False
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and existing.get("kind") == _RUNTIME_CONTINUATION_DISPLAY_KIND
                and existing.get("runtime_event_id") == event_id
                and existing.get("sid") == target_sid
            ):
                # The first attempt may have committed the private snapshot and
                # then crashed/failed while refreshing the list-row cache.  A
                # replay must repair that cache before consuming the outbox.
                try:
                    _sync_runtime_display_message_count(target_sid)
                except Exception as exc:
                    sys.stderr.write(
                        f"[chat] runtime continuation existing count sync "
                        f"failed sid={target_sid[:8]} event={event_id[:8]} "
                        f"exc={type(exc).__name__}\n"
                    )
                    sys.stderr.flush()
                return True
        except Exception:
            pass

    target_meta = sess.get_session_meta(target_sid) or {}
    target_model = str(target_meta.get("model") or outbox.get("model") or "")
    _, boundary = _hooks.turn_transcript_boundary(target_sid, target_model)
    completed_at_ms = int(outbox.get("completed_at_ms") or 0)
    message = dict(outbox.get("message") or {})
    message.update({
        "display_kind": _RUNTIME_CONTINUATION_DISPLAY_KIND,
        "runtime_event_id": event_id,
        "presentation_only": True,
        "forkable": False,
        "block_id": f"runtime-continuation:{event_id}:0:assistant",
        "_key": f"runtime-continuation:{event_id}:0:assistant",
    })
    # This is a complete assistant-only display turn.  Never expose a source
    # transcript UUID (or a fork UUID) on the successor: the bubble is not a
    # legal Claude history boundary and must not be offered as a fork point.
    message.pop("uuid", None)
    message.pop("forkUuid", None)
    payload = {
        "schema": _CANCELLED_TURN_SNAPSHOT_SCHEMA,
        "kind": _RUNTIME_CONTINUATION_DISPLAY_KIND,
        "sid": target_sid,
        "turn_id": event_id,
        "runtime_event_id": event_id,
        # Snapshot ordering is completion ordering, not the source user turn's
        # start time (which can be hours earlier for a long-running task).
        "started_at_ms": completed_at_ms,
        "terminal_at_ms": completed_at_ms,
        "anchors": {
            "normal": {
                "uuid": boundary.get("normal_uuid") or "",
                "total": int(boundary.get("normal_total") or 0),
            },
            "full": {
                "uuid": boundary.get("full_uuid") or "",
                "total": int(boundary.get("full_total") or 0),
            },
        },
        "hidden_uuids": [],
        "messages": [message],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            path.parent.chmod(0o700)
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            mode=0o600,
        )
        with suppress(OSError):
            path.chmod(0o600)
        _sync_runtime_display_message_count(target_sid)
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[chat] runtime continuation snapshot write failed "
            f"sid={target_sid[:8]} event={event_id[:8]} "
            f"exc={type(exc).__name__}\n"
        )
        sys.stderr.flush()
        return False


def _copy_runtime_continuation_snapshots(
    source_sid: str,
    target_sid: str,
    uuid_mapping: dict[str, str],
) -> int:
    """Copy presentation snapshots under the target deletion fence."""
    # Rollover copies run in a worker thread.  Cancellation of the awaiting
    # coroutine cannot stop that worker, so the same target lifecycle lock used
    # by DELETE must cover the tombstone check and every write/count refresh.
    with sess.session_lifecycle_lock(target_sid):
        if sess.session_is_deleting(target_sid):
            return 0
        return _copy_runtime_continuation_snapshots_locked(
            source_sid, target_sid, uuid_mapping)


def _copy_runtime_continuation_snapshots_locked(
    source_sid: str,
    target_sid: str,
    uuid_mapping: dict[str, str],
) -> int:
    """Copy already-visible UI continuations across a later SDK fork.

    Anchors are translated one edge at a time.  If a CLI build omits a UUID
    backlink, retain the exact bubble-total coordinate with an empty UUID;
    the fork boundary is later than every copied event, so that coordinate is
    still a safe placement fallback.
    """
    _hooks = _require_hooks()
    snapshots, _ = _hooks.load_cancelled_turn_snapshots(source_sid)
    copied = 0
    for snapshot in snapshots:
        if snapshot.get("kind") != _RUNTIME_CONTINUATION_DISPLAY_KIND:
            continue
        event_id = str(snapshot.get("runtime_event_id") or "")
        target_path = _cancelled_turn_snapshot_path(target_sid, event_id)
        if target_path is None or target_path.exists():
            continue
        anchors: dict[str, dict] = {}
        for order in ("normal", "full"):
            source_anchor = dict(
                ((snapshot.get("anchors") or {}).get(order) or {}))
            source_uuid = str(source_anchor.get("uuid") or "")
            anchors[order] = {
                "uuid": str(uuid_mapping.get(source_uuid) or "")
                if source_uuid else "",
                "total": int(source_anchor.get("total") or 0),
            }
        payload = {
            **snapshot,
            "sid": target_sid,
            "anchors": anchors,
            "messages": [
                dict(message) for message in (snapshot.get("messages") or [])
            ],
        }
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                target_path.parent.chmod(0o700)
            atomic_write_text(
                target_path,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                mode=0o600,
            )
            with suppress(OSError):
                target_path.chmod(0o600)
            copied += 1
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime continuation snapshot copy failed "
                f"sid={source_sid[:8]} child={target_sid[:8]} "
                f"event={event_id[:8]} exc={type(exc).__name__}\n"
            )
            sys.stderr.flush()
    if copied:
        try:
            _sync_runtime_display_message_count(target_sid)
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime continuation count sync failed "
                f"sid={target_sid[:8]} exc={type(exc).__name__}\n"
            )
            sys.stderr.flush()
    return copied


async def _deliver_runtime_continuation_outbox(
    source_sid: str,
    event_id: str,
) -> bool:
    """Deliver a READY outbox to the newest idle lineage leaf exactly once."""
    _hooks = _require_hooks()
    while True:
        outbox = await asyncio.to_thread(
            _load_runtime_continuation_outbox, source_sid, event_id)
        if outbox is None:
            return False
        # The durable latest link is published before its rollover
        # postlude/queue migration has reached the commit boundary.  Observe the
        # chain, acquire the lock owned by that exact latest edge, then re-read
        # under the lock.  For A -> B -> C this must be B's lock, not always A's;
        # otherwise C can still be a provisional child that later rolls back.
        observed_lineage = await asyncio.to_thread(
            sess.runtime_lineage, source_sid)
        barrier_sid = (
            observed_lineage[-2]
            if len(observed_lineage) >= 2
            else source_sid
        )
        wait_for_rollover = False
        lineage_changed = False
        async with _hooks.runtime_rollover_lock_for(barrier_sid):
            source_meta = await asyncio.to_thread(
                sess.get_session_meta, source_sid)
            if source_meta is None or sess.session_is_deleting(source_sid):
                path = _runtime_continuation_outbox_path(source_sid, event_id)
                if path is not None:
                    with suppress(OSError):
                        path.unlink(missing_ok=True)
                return False
            lineage = await asyncio.to_thread(sess.runtime_lineage, source_sid)
            if lineage != observed_lineage:
                lineage_changed = True
            elif len(lineage) < 2 or lineage[-1] == source_sid:
                # The watcher is the last authority capable of initiating a
                # boundary fork.  Keep the unconditional write-ahead record
                # until that owner exits (or a registered eager prewarm commits).
                prewarm = _hooks.runtime_prewarm_tasks.get(source_sid)
                wait_for_rollover = bool(
                    _hooks.session_has_live_watcher(source_sid)
                    or (prewarm is not None and not prewarm.done())
                    or source_meta.get("runtime_shadow")
                )
                if not wait_for_rollover:
                    path = _runtime_continuation_outbox_path(
                        source_sid, event_id)
                    if path is not None:
                        with suppress(OSError):
                            path.unlink(missing_ok=True)
                    return False
        if lineage_changed:
            await asyncio.sleep(0)
            continue
        if wait_for_rollover:
            await asyncio.sleep(0.2)
            continue
        leaf_sid = lineage[-1]
        if sess.session_is_deleting(leaf_sid):
            await asyncio.sleep(0.2)
            continue

        # A new rollover and this projection must agree on one latest leaf.
        # The leaf runtime lock also places the bubble after any turn currently
        # writing JSONL and before a later turn can establish its own boundary.
        delivered = False
        retry_delay = 0.05
        async with _hooks.runtime_rollover_lock_for(leaf_sid):
            fresh_lineage = await asyncio.to_thread(
                sess.runtime_lineage, source_sid)
            if not fresh_lineage or fresh_lineage[-1] != leaf_sid:
                retry_delay = 0.05
            else:
                active = _hooks.active_turns.get(leaf_sid)
                if active is not None and not active.done:
                    retry_delay = 0.2
                else:
                    runtime_lock = _hooks.session_runtime_lock_for(leaf_sid)
                    async with runtime_lock:
                        active = _hooks.active_turns.get(leaf_sid)
                        fresh_lineage = await asyncio.to_thread(
                            sess.runtime_lineage, source_sid)
                        if (
                            (active is not None and not active.done)
                            or not fresh_lineage
                            or fresh_lineage[-1] != leaf_sid
                        ):
                            retry_delay = 0.2
                        else:
                            delivered = await asyncio.to_thread(
                                _hooks.persist_runtime_continuation_snapshot,
                                leaf_sid,
                                outbox,
                            )
                            retry_delay = 0.5
        if delivered:
            path = _runtime_continuation_outbox_path(source_sid, event_id)
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            # A queued turn may have deferred itself while this READY record was
            # waiting for the leaf boundary. Re-kick after the presentation
            # commit; the drain's own active/paused checks remain authoritative.
            with suppress(Exception):
                queued = sess.get_queue(leaf_sid)
                if queued.get("items") or queued.get("inflight"):
                    _hooks.schedule_queue_drain(leaf_sid)
            return True
        await asyncio.sleep(retry_delay)


async def _flush_runtime_continuations_at_turn_boundary(
    leaf_sid: str,
    *,
    expected_active: "TurnBroadcast | None" = None,
) -> int:
    """Commit every READY ancestor reply before releasing a leaf turn slot.

    The source-lock pass is a commit barrier for initial forks: once each lock
    has been acquired and released, a visible link cannot still roll back.  The
    leaf rollover/runtime locks then serialize this display boundary with a
    later fork or query.  Outbox-directory rescans are synchronous and tiny so
    there is no event-loop yield between observing "empty" and returning to the
    caller that will release/reserve ``_hooks.active_turns``.
    """
    _hooks = _require_hooks()
    lineage = await asyncio.to_thread(sess.runtime_lineage, leaf_sid)
    if not lineage or lineage[-1] != leaf_sid:
        return 0
    owners = list(dict.fromkeys(lineage[:-1]))
    if not owners:
        return 0

    # Never nest source locks under the leaf lock; delivery uses source -> leaf
    # as two separate phases and this preserves the same acyclic order.
    for owner_sid in owners:
        async with _hooks.runtime_rollover_lock_for(owner_sid):
            pass

    delivered = 0
    async with _hooks.runtime_rollover_lock_for(leaf_sid):
        fresh_lineage = await asyncio.to_thread(sess.runtime_lineage, leaf_sid)
        if not fresh_lineage or fresh_lineage[-1] != leaf_sid:
            return 0
        fresh_owners = list(dict.fromkeys(fresh_lineage[:-1]))
        if not fresh_owners:
            return 0
        async with _hooks.session_runtime_lock_for(leaf_sid):
            active = _hooks.active_turns.get(leaf_sid)
            if expected_active is None:
                if active is not None and not active.done:
                    return 0
            elif active is not expected_active:
                return 0

            while True:
                ready = [
                    (owner_sid, event_id)
                    for owner_sid in fresh_owners
                    for event_id in _runtime_continuation_outbox_event_ids(
                        owner_sid)
                ]
                if not ready:
                    return delivered
                progressed = False
                for owner_sid, event_id in ready:
                    outbox = await asyncio.to_thread(
                        _load_runtime_continuation_outbox,
                        owner_sid,
                        event_id,
                    )
                    if outbox is None:
                        # Invalid records are quarantined by the loader, so a
                        # rescan can make forward progress without busy-looping.
                        progressed = True
                        continue
                    persisted = await asyncio.to_thread(
                        _hooks.persist_runtime_continuation_snapshot,
                        leaf_sid,
                        outbox,
                    )
                    if not persisted:
                        # Keep the write-ahead record. Delivery/restart can retry,
                        # and queue drain will be re-kicked after a later commit.
                        continue
                    path = _runtime_continuation_outbox_path(
                        owner_sid, event_id)
                    if path is not None:
                        with suppress(OSError):
                            path.unlink(missing_ok=True)
                    delivered += 1
                    progressed = True
                if not progressed:
                    return delivered


def _schedule_runtime_continuation_delivery(
    source_sid: str,
    event_id: str,
) -> asyncio.Task | None:
    _hooks = _require_hooks()
    key = (source_sid, event_id)
    existing = RUNTIME_CONTINUATION_DELIVERY_TASKS.get(key)
    if existing is not None and not existing.done():
        return existing
    try:
        task = asyncio.create_task(
            _deliver_runtime_continuation_outbox(source_sid, event_id))
    except RuntimeError:
        return None
    RUNTIME_CONTINUATION_DELIVERY_TASKS[key] = task
    _hooks.maintenance_tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        if RUNTIME_CONTINUATION_DELIVERY_TASKS.get(key) is done:
            RUNTIME_CONTINUATION_DELIVERY_TASKS.pop(key, None)
        _hooks.maintenance_tasks.discard(done)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception as exc:
            sys.stderr.write(
                f"[chat] runtime continuation delivery failed "
                f"sid={source_sid[:8]} event={event_id[:8]} "
                f"exc={type(exc).__name__}\n"
            )
            sys.stderr.flush()

    task.add_done_callback(_done)
    return task


async def recover_runtime_continuation_outboxes_at_startup() -> int:
    """Schedule crash-surviving READY continuations before serving traffic."""
    entries = await asyncio.to_thread(_runtime_continuation_outbox_entries)
    scheduled = 0
    for source_sid, event_id in entries:
        if _schedule_runtime_continuation_delivery(source_sid, event_id):
            scheduled += 1
    return scheduled


def _runtime_continuation_projection_state(sid: str) -> tuple[bool, str]:
    """Return lineage-wide pending state and the visible leaf's UI revision."""
    _hooks = _require_hooks()
    lineage = sess.runtime_lineage(sid) or [sid]
    leaf_sid = lineage[-1]
    pending = False
    for owner_sid in lineage[:-1]:
        if (
            _hooks.session_has_live_watcher(owner_sid)
            or _session_has_runtime_continuation_outbox(owner_sid)
        ):
            pending = True
            break
    try:
        _, revision = _hooks.load_cancelled_turn_snapshots(leaf_sid)
    except Exception:
        revision = ""
    return pending, revision


def _delete_cancelled_turn_snapshots(sid: str) -> None:
    directory = _cancelled_turn_session_dir(sid)
    if directory is None or not directory.exists():
        return
    shutil.rmtree(directory, ignore_errors=True)


def _cancelled_snapshot_canonical_span(
    transcript_path: Path,
    index: dict,
    snapshot: dict,
) -> tuple[list[str], str]:
    """Resolve a late JSONL flush back to its interrupted turn.

    A force-stopped CLI can acknowledge the terminal boundary before its
    AssistantMessage UUID is observable, then append that canonical record
    several seconds later.  The snapshot's pre-query record coordinate plus
    the original user-record timestamp identifies that turn without inspecting
    or logging private message text.  Return all UUIDs in the logical turn and
    the last assistant UUID that can own its durable footer.
    """
    _hooks = _require_hooks()
    boundary = snapshot.get("transcript_boundary") or {}
    records = index.get("records") or []
    try:
        start = int(boundary.get("record_count"))
    except (TypeError, ValueError):
        return [], ""
    if start < 0 or start >= len(records):
        return [], ""

    source = index.get("source") or {}
    for field, current_field in (
        ("source_dev", "dev"), ("source_inode", "inode"),
    ):
        try:
            expected = int(boundary.get(field) or 0)
            current = int(source.get(current_field) or 0)
        except (TypeError, ValueError):
            return [], ""
        if expected and current and expected != current:
            return [], ""

    real_user_ids = [
        record_i for record_i in range(start, len(records))
        if records[record_i].get("real_user_prompt")
    ]
    if not real_user_ids:
        return [], ""
    entries = transcript_idx.read_records(
        transcript_path, index, real_user_ids)
    entry_by_uuid = {
        str(entry.get("uuid") or ""): entry for entry in entries
    }
    started_ms = int(snapshot.get("started_at_ms") or 0)
    interrupted_ms = int(snapshot.get("interrupted_at_ms") or 0)
    canonical_terminal = bool(snapshot.get("canonical_terminal_published"))
    terminal_status = str(snapshot.get("terminal_status") or "")
    origin_i: int | None = None
    for candidate_position, record_i in enumerate(real_user_ids):
        record_uuid = str(records[record_i].get("uuid") or "")
        ts_ms = _hooks.transcript_ts_ms(entry_by_uuid.get(record_uuid) or {})
        if ts_ms is None:
            # A canonical ResultMessage means this query definitely reached
            # the transcript even on legacy rows without timestamps.  The
            # first real prompt after the pre-query boundary is therefore it.
            # Failed display snapshots are stricter: they may represent a
            # gateway rejection before the user row was flushed. Treating the
            # first later legacy/no-timestamp prompt as that failed turn would
            # hide a legitimate resend and relabel its assistant failed.
            if (canonical_terminal and candidate_position == 0
                    and snapshot.get("terminal_status") != "failed"):
                origin_i = record_i
            break
        if started_ms and ts_ms < started_ms - 5_000:
            continue
        # Failed snapshots can be created before the rejected user row ever
        # reaches the transcript. A deliberate retry may happen immediately;
        # never absorb a timestamped prompt *after* the failure merely because
        # it arrived inside the normal 250 ms late-flush tolerance. Interrupted
        # turns still retain that tolerance because their original user row is
        # known to have entered the live SDK stream.
        terminal_slack_ms = 0 if terminal_status == "failed" else 250
        if interrupted_ms and ts_ms <= interrupted_ms + terminal_slack_ms:
            origin_i = record_i
            break
        # A post-interrupt prompt belongs to a resend/new turn.  Never borrow
        # its assistant merely because the user repeated the exact same text.
        if interrupted_ms and ts_ms > interrupted_ms + terminal_slack_ms:
            break

    if origin_i is None:
        return [], ""
    origin_uuid = str(records[origin_i].get("uuid") or "")
    by_uuid = {
        str(record.get("uuid") or ""): record_i
        for record_i, record in enumerate(records)
        if record.get("uuid")
    }
    origin_cache: dict[int, str] = {}

    def logical_origin(record_i: int) -> str:
        cached = origin_cache.get(record_i)
        if cached is not None:
            return cached
        current_i = record_i
        seen: set[str] = set()
        result = ""
        while 0 <= current_i < len(records):
            current = records[current_i]
            current_uuid = str(current.get("uuid") or "")
            if current_uuid in seen:
                break
            if current_uuid:
                seen.add(current_uuid)
            # A task notification starts a separate headless continuation;
            # cancelling the launch turn must not relabel that later reaction.
            if current.get("task_notifications"):
                break
            if current.get("real_user_prompt"):
                result = current_uuid
                break
            parent = str(current.get("parent") or "")
            parent_i = by_uuid.get(parent) if parent else None
            if parent_i is None:
                break
            current_i = parent_i
        origin_cache[record_i] = result
        return result

    span: list[str] = []
    tail_assistant_uuid = ""
    for record_i in range(origin_i, len(records)):
        if logical_origin(record_i) != origin_uuid:
            continue
        record = records[record_i]
        record_uuid = str(record.get("uuid") or "")
        if record_uuid:
            span.append(record_uuid)
        if (record.get("type") == "assistant"
                and int(record.get("bubble_count") or 0) > 0):
            tail_assistant_uuid = record_uuid
    return span, tail_assistant_uuid


def _heal_cancelled_snapshot_from_canonical(
    sid: str,
    path: Path,
    snapshot: dict,
    transcript_path: Path,
    index: dict,
) -> bool:
    """Promote a late canonical assistant to the snapshot's terminal truth.

    Until an assistant exists, dynamically hide any duplicate canonical user
    rows and keep rendering the private snapshot.  An interrupted snapshot can
    retire once the assistant arrives because its missing truth is only footer
    metadata.  A failed snapshot also owns a separate terminal error bubble;
    an arbitrary partial assistant does not prove that bubble was persisted,
    so the display snapshot must remain authoritative.
    """
    _hooks = _require_hooks()
    span, assistant_uuid = _hooks.cancelled_snapshot_canonical_span(
        transcript_path, index, snapshot)
    if span:
        snapshot["hidden_uuids"] = list(dict.fromkeys([
            *(snapshot.get("hidden_uuids") or []), *span,
        ]))
    if not assistant_uuid:
        return False
    started_ms = int(snapshot.get("started_at_ms") or 0)
    terminal_ms = int(
        snapshot.get("terminal_at_ms")
        or snapshot.get("interrupted_at_ms")
        or 0
    )
    elapsed_s = round(max(0, terminal_ms - started_ms) / 1000, 1)
    terminal_status = str(snapshot.get("terminal_status") or "cancelled")
    try:
        sess.set_message_annotation(
            sid,
            assistant_uuid,
            model=str(snapshot.get("model") or ""),
            ts=terminal_ms or None,
            turn_status=terminal_status,
            elapsed_s=elapsed_s,
            memory_recall=snapshot.get("memory_recall") or None,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[chat] late cancelled footer heal failed sid={sid[:8]} "
            f"exc={type(exc).__name__}\n")
        return False
    if terminal_status == "failed":
        # Keep replacing the whole canonical turn with the display snapshot.
        # Otherwise a partial AssistantMessage causes the healer to delete the
        # only durable copy of the terminal error row on the first quiet reload.
        return False

    # Exact path comes from the already-validated canonical sid/turn snapshot
    # directory.  Failure to unlink is harmless: subsequent reads resolve and
    # exclude it again, while the sidecar remains authoritative.
    with suppress(OSError):
        path.unlink(missing_ok=True)
    return True


def _load_cancelled_turn_snapshots(sid: str) -> tuple[list[dict], str]:
    """Load private display-only interrupted-turn snapshots.

    The generation hashes filenames and stat data only; private message text is
    never copied into an ETag, log line, or other externally observable cache
    key.
    """
    _hooks = _require_hooks()
    directory = _cancelled_turn_session_dir(sid)
    if directory is None or not directory.exists():
        return [], ""
    loaded: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("snapshot root must be an object")
            if data.get("schema") != _CANCELLED_TURN_SNAPSHOT_SCHEMA:
                raise ValueError("unsupported snapshot schema")
            if data.get("sid") != sid or not isinstance(data.get("messages"), list):
                raise ValueError("snapshot ownership mismatch")
            loaded.append((path, data))
        except Exception as exc:
            # Never log payloads: a cancelled turn can contain private source,
            # prompts, or tool output. Filename + exception class is enough for
            # an operator to find a corrupt local artifact.
            sys.stderr.write(
                f"[chat] cancelled snapshot skipped file={path.name} "
                f"exc={type(exc).__name__}\n")
    indexed: tuple[Path, dict] | None = None
    if any((data.get("transcript_boundary") or {}).get("record_count") is not None
           for _, data in loaded):
        try:
            indexed = _hooks.ensure_transcript_index(sid)
        except Exception as exc:
            sys.stderr.write(
                f"[chat] cancelled snapshot heal skipped sid={sid[:8]} "
                f"exc={type(exc).__name__}\n")

    snapshots: list[dict] = []
    kept_paths: list[Path] = []
    for path, data in loaded:
        healed = False
        if indexed is not None and data.get("transcript_boundary"):
            try:
                healed = _heal_cancelled_snapshot_from_canonical(
                    sid, path, data, indexed[0], indexed[1])
            except Exception as exc:
                # Recovery must fail closed to the already-valid private
                # snapshot; a malformed/temporarily changing transcript must
                # never turn session history into a 500 response.
                sys.stderr.write(
                    f"[chat] cancelled snapshot heal deferred sid={sid[:8]} "
                    f"exc={type(exc).__name__}\n")
        if not healed:
            snapshots.append(data)
            kept_paths.append(path)
    snapshots.sort(key=lambda item: (
        int(item.get("started_at_ms") or 0), str(item.get("turn_id") or "")))
    digest = hashlib.blake2b(digest_size=12)
    for path in kept_paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("ascii", errors="ignore"))
        digest.update(f":{stat.st_mtime_ns}:{stat.st_size};".encode("ascii"))
    return snapshots, digest.hexdigest() if snapshots else ""


def _combined_history_generation(base: str, snapshot_generation: str) -> str:
    return chat_history_window.combined_generation(base, snapshot_generation)


def _persist_cancelled_turn_snapshot(bc: "TurnBroadcast") -> bool:
    """Serialize the pump/watchdog race and persist one stable snapshot."""
    with bc.cancelled_snapshot_lock:
        return _persist_cancelled_turn_snapshot_locked(bc)


def _cancelled_footer_values(bc: "TurnBroadcast") -> tuple[int, float]:
    """Return one stable terminal timestamp/duration for an interrupted turn."""
    now_ms = int(getattr(bc, "cancelled_at_ms", 0) or 0)
    if now_ms <= 0:
        now_ms = int(time.time() * 1000)
        bc.cancelled_at_ms = now_ms
    elapsed_s = max(0.0, now_ms / 1000.0 - float(bc.started_at or 0))
    return now_ms, round(elapsed_s, 1)


def _persist_cancelled_footer_annotation_locked(
    bc: "TurnBroadcast", now_ms: int, elapsed_s: float,
) -> bool:
    """Persist footer truth even when an interrupted turn reached JSONL late.

    ResultMessage normally owns this annotation.  A forced CLI teardown can
    still emit and persist an AssistantMessage without ever producing that
    terminal ResultMessage, which used to leave a refresh with only the raw
    text.  AssistantMessage already gave us its exact UUID; writing a private
    sidecar annotation is safe even if the JSONL line itself flushes shortly
    afterwards.
    """
    assistant_uuid = str(getattr(bc, "last_assistant_uuid", "") or "")
    if not assistant_uuid:
        return False
    try:
        sess.set_message_annotation(
            bc.session_id,
            assistant_uuid,
            model=bc.model,
            ts=now_ms,
            turn_status="cancelled",
            elapsed_s=elapsed_s,
        )
        return True
    except Exception as exc:
        # No prompt/reply text in logs: UUID ownership + exception class is
        # enough to diagnose a local sidecar failure without leaking content.
        sys.stderr.write(
            f"[chat] cancelled footer annotation failed "
            f"sid={bc.session_id[:8]} exc={type(exc).__name__}\n")
        sys.stderr.flush()
        return False


def _persist_cancelled_turn_snapshot_locked(bc: "TurnBroadcast") -> bool:
    """Atomically persist the browser-visible part of a non-canonical turn."""
    _hooks = _require_hooks()
    if bc.cancelled_snapshot_persisted:
        return True
    if bc.cancelled_snapshot_suppressed or not bc.cancelled:
        return False
    now_ms, elapsed_s = _cancelled_footer_values(bc)
    annotation_ready = _persist_cancelled_footer_annotation_locked(
        bc, now_ms, elapsed_s)
    # ResultMessage made the canonical transcript authoritative.  Do not layer
    # a duplicate display snapshot over it; the exact UUID annotation above is
    # the only missing persistence step in this narrow done/interrupt race.
    if bc.canonical_terminal_published and annotation_ready:
        return annotation_ready
    path = _cancelled_turn_snapshot_path(bc.session_id, bc.turn_id)
    if path is None:
        return False
    messages = _hooks.broadcast_to_ui_messages(bc)
    if not messages:
        return False

    for index, message in enumerate(messages):
        block_id = f"snapshot:{bc.turn_id}:{index}:{message.get('role') or 'unknown'}"
        message["block_id"] = block_id
        message["_key"] = block_id
        message["_interrupted"] = True
        message["_interrupted_turn_id"] = bc.turn_id
    for message in reversed(messages):
        if message.get("role") != "user":
            message.setdefault("ts", now_ms)
            if elapsed_s >= 1:
                message.setdefault("elapsed", elapsed_s)
            message.setdefault("model", bc.model)
            message.setdefault("turn_status", "cancelled")
            break

    boundary = dict(bc.transcript_boundary or {})

    payload = {
        "schema": _CANCELLED_TURN_SNAPSHOT_SCHEMA,
        "sid": bc.session_id,
        "turn_id": bc.turn_id,
        "model": bc.model,
        "started_at_ms": int(float(bc.started_at or 0) * 1000),
        "interrupted_at_ms": now_ms,
        "canonical_terminal_published": bool(
            bc.canonical_terminal_published),
        "transcript_boundary": {
            "record_count": int(boundary.get("record_count") or 0),
            "source_dev": int(boundary.get("source_dev") or 0),
            "source_inode": int(boundary.get("source_inode") or 0),
        },
        "anchors": {
            "normal": {
                "uuid": boundary.get("normal_uuid") or "",
                "total": int(boundary.get("normal_total") or 0),
            },
            "full": {
                "uuid": boundary.get("full_uuid") or "",
                "total": int(boundary.get("full_total") or 0),
            },
        },
        "hidden_uuids": [],
        "messages": messages,
    }
    try:
        # Keep the atomic writer's temporary file and the final snapshot in a
        # session-private directory. Interrupted prompts and tool output must
        # not be readable by another local account during the rename-sized
        # window before the final file chmod below.
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            path.parent.chmod(0o700)
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            mode=0o600,
        )
        with suppress(OSError):
            path.chmod(0o600)
    except Exception as exc:
        sys.stderr.write(
            f"[chat] cancelled snapshot write failed sid={bc.session_id[:8]} "
            f"exc={type(exc).__name__}\n")
        sys.stderr.flush()
        return False

    # The durable display record is already committed. Metadata is a
    # self-healing cache (the history endpoint recomputes it), so a failure to
    # bump the list row must not tell the browser that snapshot persistence
    # failed and leave the just-rendered pane exposed to an older reload.
    bc.cancelled_snapshot_persisted = True
    try:
        snapshots, _ = _hooks.load_cancelled_turn_snapshots(bc.session_id)
        indexed = _hooks.ensure_transcript_index(bc.session_id)
        index = indexed[1] if indexed is not None else None
        total, turns = _interrupted_history_stats(index, snapshots, "normal")
        sess.bump_session(
            bc.session_id,
            message_count=total,
            turn_count=turns,
            auto_rename_from=bc.user_text,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[chat] cancelled snapshot metadata sync failed "
            f"sid={bc.session_id[:8]} "
            f"exc={type(exc).__name__}\n")
        sys.stderr.flush()
    return True


def _persist_failed_turn_snapshot(
    bc: "TurnBroadcast",
    error_text: str,
    *,
    terminal_at_ms: int | None = None,
    elapsed_s: float | None = None,
    memory_recall: dict | None = None,
    canonical_terminal_published: bool = False,
) -> bool:
    """Persist a refreshable display record for a terminal turn failure.

    The SDK normally writes an AssistantMessage that can own footer metadata.
    Some Gateway/API/transport failures emit only a ResultMessage (or no
    ResultMessage at all). In that shape there is no safe UUID to annotate:
    choosing the transcript's latest assistant would mutate the previous turn.
    Even when a legitimate partial assistant exists, it cannot own the separate
    terminal error bubble shown live. Store the exact browser-visible failure
    in the same private, display-only snapshot channel used for interrupted
    turns instead. It is never fed back into model context and historical files
    remain mode 0600.
    """
    _hooks = _require_hooks()
    with bc.cancelled_snapshot_lock:
        if bc.failed_snapshot_persisted:
            return True
        if bc.cancelled_snapshot_suppressed or bc.cancelled:
            return False
        path = _cancelled_turn_snapshot_path(bc.session_id, bc.turn_id)
        if path is None:
            return False

        now_ms = int(terminal_at_ms or time.time() * 1000)
        duration = (
            max(0.0, float(elapsed_s))
            if elapsed_s is not None
            else max(0.0, now_ms / 1000.0 - float(bc.started_at or 0))
        )
        duration = round(duration, 1)
        visible_error = str(error_text or "Turn failed without an assistant response.")
        error_meta = _hooks.classify_stream_error(visible_error)
        messages = _hooks.broadcast_to_ui_messages(bc)

        # Match the live UI: retain any legitimate partial answer, then show
        # the terminal failure as its own assistant row. Avoid a duplicate if
        # the provider already streamed the exact same text before dying.
        last_assistant = next(
            (message for message in reversed(messages)
             if message.get("role") == "assistant"),
            None,
        )
        if not last_assistant or str(last_assistant.get("text") or "") != visible_error:
            messages.append({
                "role": "assistant",
                "text": visible_error,
                "model": bc.model,
                "error": visible_error,
            })
        if not messages:
            return False

        for index, message in enumerate(messages):
            block_id = f"snapshot:{bc.turn_id}:{index}:{message.get('role') or 'unknown'}"
            message["block_id"] = block_id
            message["_key"] = block_id
            message["_terminalTurnId"] = bc.turn_id
            if message.get("role") == "user":
                message["_failed"] = True
                message["_error_text"] = visible_error
                message["_error_kind"] = error_meta["kind"]
                message["_error_cta"] = error_meta["cta"]
                message["_error_retryable"] = error_meta["retryable"]
        for message in reversed(messages):
            if message.get("role") == "user":
                continue
            message.setdefault("ts", now_ms)
            if duration >= 1:
                message.setdefault("elapsed", duration)
            message.setdefault("model", bc.model)
            message.setdefault("turn_status", "failed")
            if memory_recall:
                message.setdefault("memoryRecall", memory_recall)
            break

        boundary = dict(bc.transcript_boundary or {})
        payload = {
            "schema": _CANCELLED_TURN_SNAPSHOT_SCHEMA,
            "sid": bc.session_id,
            "turn_id": bc.turn_id,
            "model": bc.model,
            "terminal_status": "failed",
            "started_at_ms": int(float(bc.started_at or 0) * 1000),
            "terminal_at_ms": now_ms,
            # Retain this legacy coordinate so the existing canonical-span
            # resolver can safely pair/hide the just-written user JSONL row.
            "interrupted_at_ms": now_ms,
            "canonical_terminal_published": bool(
                canonical_terminal_published),
            "memory_recall": memory_recall,
            "transcript_boundary": {
                "record_count": int(boundary.get("record_count") or 0),
                "source_dev": int(boundary.get("source_dev") or 0),
                "source_inode": int(boundary.get("source_inode") or 0),
            },
            "anchors": {
                "normal": {
                    "uuid": boundary.get("normal_uuid") or "",
                    "total": int(boundary.get("normal_total") or 0),
                },
                "full": {
                    "uuid": boundary.get("full_uuid") or "",
                    "total": int(boundary.get("full_total") or 0),
                },
            },
            "hidden_uuids": [],
            "messages": messages,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                path.parent.chmod(0o700)
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                mode=0o600,
            )
            with suppress(OSError):
                path.chmod(0o600)
        except Exception as exc:
            sys.stderr.write(
                f"[chat] failed snapshot write failed sid={bc.session_id[:8]} "
                f"exc={type(exc).__name__}\n")
            sys.stderr.flush()
            return False

        bc.failed_snapshot_persisted = True
        try:
            snapshots, _ = _hooks.load_cancelled_turn_snapshots(bc.session_id)
            indexed = _hooks.ensure_transcript_index(bc.session_id)
            index = indexed[1] if indexed is not None else None
            total, turns = _interrupted_history_stats(index, snapshots, "normal")
            sess.bump_session(
                bc.session_id,
                message_count=total,
                turn_count=turns,
                auto_rename_from=bc.user_text,
            )
        except Exception as exc:
            sys.stderr.write(
                f"[chat] failed snapshot metadata sync failed "
                f"sid={bc.session_id[:8]} exc={type(exc).__name__}\n")
            sys.stderr.flush()
        return True


def _recover_interrupted_turn_snapshot(sid: str) -> bool:
    """Move a process-crash orphan from pending intent to durable history.

    The active-turn sidecar is the only record guaranteed to exist before the
    CLI commits a UserMessage.  On restart, convert it to the same private
    failed-turn snapshot used by live terminal failures before deleting the
    sidecar.  This keeps JSONL authoritative while ensuring a reload or a later
    send cannot silently erase the user's original text.
    """
    _hooks = _require_hooks()
    data = _hooks.interrupted_at_startup.get(sid)
    if not isinstance(data, dict):
        return True

    turn = _hooks.turn_broadcast_factory(
        session_id=sid,
        model=str(data.get("model") or ""),
    )
    turn_id = str(data.get("turn_id") or "")
    try:
        turn.turn_id = str(uuid.UUID(turn_id))
    except (ValueError, AttributeError):
        # Old sidecars predate turn_id. A fresh stable ID is sufficient because
        # the orphan has no canonical turn identity to preserve.
        pass
    turn.user_text = str(data.get("user_text") or "")
    images = data.get("user_images")
    docs = data.get("user_docs")
    boundary = data.get("transcript_boundary")
    turn.user_images = list(images) if isinstance(images, list) else []
    turn.user_docs = list(docs) if isinstance(docs, list) else []
    turn.transcript_boundary = dict(boundary) if isinstance(boundary, dict) else {}
    try:
        turn.started_at = float(data.get("started_at") or turn.started_at)
    except (TypeError, ValueError):
        pass

    # A process can die after the CLI append but before sidecar cleanup. If this
    # exact pre-query boundary already owns a canonical user+assistant pair,
    # JSONL has won ownership and no failed display snapshot should be layered
    # over the successful turn.
    if turn.transcript_boundary.get("capture_ok"):
        assistant_uuid, user_uuid, _ = _hooks.turn_uuids_from_boundary(
            sid,
            turn.transcript_boundary,
            started_at_ms=int(turn.started_at * 1000),
            terminal_at_ms=int(time.time() * 1000),
        )
        if assistant_uuid and user_uuid:
            _hooks.delete_active_turn_sidecar(sid)
            return True

    recovered = _hooks.persist_failed_turn_snapshot(
        turn,
        "MuseLab restarted before this turn reached canonical conversation history.",
        terminal_at_ms=int(time.time() * 1000),
        canonical_terminal_published=False,
    )
    if recovered:
        _hooks.delete_active_turn_sidecar(sid)
    return recovered


def _interrupted_history_segments(
    index: dict | None,
    snapshots: list[dict],
    order: str,
) -> tuple[list[dict], int]:
    return chat_history_window.history_segments(index, snapshots, order)


def _interrupted_history_stats(
    index: dict | None,
    snapshots: list[dict],
    order: str,
) -> tuple[int, int]:
    return chat_history_window.history_stats(index, snapshots, order)


def _interrupted_history_window(
    transcript_path: Path | None,
    index: dict | None,
    snapshots: list[dict],
    annotations: dict[str, dict],
    order: str,
    *,
    tail: int = 0,
    offset: int = -1,
    limit: int = 0,
) -> tuple[list[dict], int, int, bool]:
    return chat_history_window.history_window(
        transcript_path,
        index,
        snapshots,
        annotations,
        order,
        shape_records=_require_hooks().indexed_ui_records,
        tail=tail,
        offset=offset,
        limit=limit,
    )


def _interrupted_history_window_around_uuid(
    transcript_path: Path,
    index: dict,
    snapshots: list[dict],
    annotations: dict[str, dict],
    uuid_value: str,
    before: int,
    after: int,
    *,
    limit: int = 0,
) -> tuple[list[dict], int, int, bool] | None:
    return chat_history_window.history_window_around_uuid(
        transcript_path,
        index,
        snapshots,
        annotations,
        uuid_value,
        before,
        after,
        shape_records=_require_hooks().indexed_ui_records,
        limit=limit,
    )
