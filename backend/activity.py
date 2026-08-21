"""Durable cross-workspace task activity ledger."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .settings import ROOT, atomic_write_text
from . import sessions

_MAX_EVENTS = 500
_MAX_CUSTOM_GROUPS = 40
_MAX_GROUP_NAME = 48
_UNGROUPED_GROUP_ID = "__ungrouped__"
_GROUP_COLORS = frozenset({
    "blue", "violet", "cyan", "green", "amber", "rose", "gray",
})
_TERMINAL = {"completed", "failed", "cancelled"}
_ACTIVE = {"running", "waiting_approval", "paused"}


def _activity_at(item: dict[str, Any]) -> float:
    """Return the latest task transition timestamp without letting ACK reorder rows."""
    return float(item.get("updated_at") or item.get("finished_at")
                 or item.get("started_at") or 0)


def _is_unread_result(item: dict[str, Any]) -> bool:
    return item.get("state") in {"completed", "failed"} and not item.get("read")


def _requires_action(item: dict[str, Any]) -> bool:
    return item.get("state") in {"waiting_approval", "paused"}


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {
        "review": sum(x.get("state") == "completed" and not x.get("read")
                      for x in events),
        "running": sum(x.get("state") in _ACTIVE for x in events),
        "failed": sum(x.get("state") == "failed" for x in events),
        "history": sum((x.get("state") == "completed" and bool(x.get("read")))
                       or x.get("state") == "cancelled" for x in events),
    }
    group_unread = {
        "review": groups["review"],
        "running": sum(_requires_action(x) for x in events),
        "failed": sum(x.get("state") == "failed" and not x.get("read")
                      for x in events),
        "history": 0,
    }
    workspaces: dict[str, dict[str, Any]] = {}
    for item in events:
        path = str(item.get("workspace") or "")
        row = workspaces.setdefault(path, {"path": path,
            "name": item.get("workspace_name") or Path(path).name or "Workspace",
            "running": 0, "unread": 0, "attention": 0})
        if item.get("state") in _ACTIVE:
            row["running"] += 1
        if _is_unread_result(item):
            row["unread"] += 1
        if _requires_action(item) or (item.get("state") == "failed" and not item.get("read")):
            row["attention"] += 1
    return {
        "running": groups["running"],
        "unread": sum(_is_unread_result(x) for x in events),
        "attention": sum(_requires_action(x)
                         or (x.get("state") == "failed" and not x.get("read"))
                         for x in events),
        "groups": groups,
        "group_unread": group_unread,
        "workspaces": list(workspaces.values()),
    }


class ActivityService:
    """Keep one current activity row per conversation."""

    def __init__(
        self,
        root: Path = ROOT,
        *,
        initialize_runtime_state: bool = True,
    ):
        self.path = root / ".muselab" / "activity.json"
        self.groups_path = root / ".muselab" / "activity-groups.json"
        self.transaction_path = root / ".muselab" / "activity-transaction.json"
        self._group_event_transaction_active = False
        self._lock = threading.RLock()
        self._generation = uuid.uuid4().hex
        self._revision = 0
        self._subscribers: dict[
            asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop
        ] = {}
        self._initialized = False
        self._custom_groups: list[dict[str, str]] = []
        self._group_assignments: dict[str, str] = {}
        self._group_order: list[str] = [_UNGROUPED_GROUP_ID]
        self._events: list[dict[str, Any]] = []
        if initialize_runtime_state:
            self.initialize_runtime_state()

    def initialize_runtime_state(self) -> None:
        """Load and reconcile the ledger exactly once, on first runtime use.

        The module-level service defers this step so importing API routers during
        test collection cannot read, rewrite, unlink or chmod the real ledger.
        Production calls it from the lifespan; public methods keep a lazy
        fallback for direct and TestClient use.
        """
        with self._lock:
            if self._initialized:
                return
            self.ensure_private_storage()
            self._recover_group_event_transaction()
            (self._custom_groups, self._group_assignments,
             self._group_order) = self._load_group_state()
            self._events = self._load()
            changed = self._collapse_sessions()
            changed = self._reconcile_event_groups() or changed
            for item in self._events:
                if item.get("state") in _ACTIVE:
                    now = time.time()
                    item.update(
                        state="failed",
                        status_detail="Interrupted by service restart",
                        finished_at=now,
                        updated_at=now,
                        needs_attention=False,
                        read=False,
                    )
                    changed = True
            if changed:
                self._save()
            # Mark initialization complete only after any reconciliation write
            # succeeds.  A transient disk failure must leave the service
            # retryable instead of pinning an unsaved in-memory view forever.
            self._initialized = True

    def ensure_private_storage(self) -> None:
        """Keep task prompts and workspace paths private on shared hosts."""
        storage_dir = self.path.parent
        if storage_dir.is_symlink():
            raise RuntimeError("activity storage directory must not be a symlink")
        storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage_dir.chmod(0o700)
        for private_path in (
            self.path, self.groups_path, self.transaction_path,
        ):
            if private_path.is_symlink():
                raise RuntimeError(
                    f"activity state must not be a symlink: {private_path.name}"
                )
            if private_path.exists():
                private_path.chmod(0o600)

    @staticmethod
    def _group_name(value: Any) -> str:
        name = " ".join(str(value or "").split())
        if not name:
            raise ValueError("group name is required")
        if len(name) > _MAX_GROUP_NAME:
            raise ValueError(f"group name exceeds {_MAX_GROUP_NAME} characters")
        return name

    @staticmethod
    def _group_color(value: Any) -> str:
        color = str(value or "blue").strip().lower()
        if color not in _GROUP_COLORS:
            raise ValueError("invalid group color")
        return color

    def _recover_group_event_transaction(self) -> None:
        try:
            payload = json.loads(self.transaction_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"cannot parse activity transaction: {self.transaction_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("activity transaction must be an object")
        events = payload.get("events")
        group_state = payload.get("group_state")
        if not isinstance(events, list) or not isinstance(group_state, dict):
            raise RuntimeError("invalid activity transaction")
        self.ensure_private_storage()
        atomic_write_text(
            self.groups_path,
            json.dumps(group_state, ensure_ascii=False, indent=2),
            mode=0o600,
        )
        atomic_write_text(
            self.path,
            json.dumps(events[-_MAX_EVENTS:], ensure_ascii=False, indent=2),
            mode=0o600,
        )
        self.transaction_path.unlink(missing_ok=True)

    def _load_group_state(
        self,
    ) -> tuple[list[dict[str, str]], dict[str, str], list[str]]:
        try:
            raw = json.loads(self.groups_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], {}, [_UNGROUPED_GROUP_ID]
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"cannot parse activity groups: {self.groups_path}"
            ) from exc
        if not isinstance(raw, dict):
            raise RuntimeError("activity group state must be an object")

        source_groups = raw.get("groups", [])
        source_assignments = raw.get("assignments", {})
        source_order = raw.get("order", [])
        if (not isinstance(source_groups, list)
                or not isinstance(source_assignments, dict)
                or not isinstance(source_order, list)):
            raise RuntimeError("invalid activity group state")

        groups: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in source_groups[:_MAX_CUSTOM_GROUPS]:
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("id") or "").strip()
            if not group_id or group_id in seen or group_id == _UNGROUPED_GROUP_ID:
                continue
            try:
                name = self._group_name(row.get("name"))
                color = self._group_color(row.get("color"))
            except ValueError:
                continue
            seen.add(group_id)
            groups.append({"id": group_id, "name": name, "color": color})

        assignments = {
            str(sid): str(group_id)
            for sid, group_id in source_assignments.items()
            if str(sid) and str(group_id) in seen
        }
        order: list[str] = []
        for value in source_order:
            group_id = str(value or "").strip()
            if group_id in seen and group_id not in order:
                order.append(group_id)
        for group in groups:
            if group["id"] not in order:
                order.append(group["id"])
        # Ungrouped is a system-managed inbox lane and always stays last.
        order.append(_UNGROUPED_GROUP_ID)
        lookup = {group["id"]: group for group in groups}
        groups = [lookup[group_id] for group_id in order if group_id in lookup]
        return groups, assignments, order

    def _ensure_writes_available(self) -> None:
        if (self.transaction_path.exists()
                and not self._group_event_transaction_active):
            raise RuntimeError(
                "activity writes are blocked by an unrecovered transaction"
            )

    def _save_group_state(self) -> None:
        self._ensure_writes_available()
        self.ensure_private_storage()
        atomic_write_text(self.groups_path, json.dumps({
            "version": 2,
            "groups": self._custom_groups,
            "assignments": self._group_assignments,
            "order": self._group_order,
        }, ensure_ascii=False, indent=2), mode=0o600)

    def _group_payload_locked(self) -> list[dict[str, str]]:
        return [dict(group) for group in self._custom_groups]

    def _group_order_payload_locked(self) -> list[str]:
        return list(self._group_order)

    @staticmethod
    def _event_group_id(item: dict[str, Any]) -> str:
        return str(item.get("group_id") or "")

    @staticmethod
    def _manual_group_order(item: dict[str, Any]) -> float | None:
        value = item.get("group_order")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _ordered_group_items_locked(
        self,
        group_id: str,
        *,
        exclude_id: str = "",
    ) -> list[dict[str, Any]]:
        items = [
            item for item in self._events
            if self._event_group_id(item) == group_id
            and str(item.get("id") or "") != exclude_id
        ]

        def order_key(item: dict[str, Any]) -> tuple[float, float, str]:
            manual = self._manual_group_order(item)
            if manual is None:
                return (0, -_activity_at(item), str(item.get("id") or ""))
            return (1, manual, str(item.get("id") or ""))

        return sorted(items, key=order_key)

    @staticmethod
    def _renumber_group_locked(items: list[dict[str, Any]]) -> None:
        for index, item in enumerate(items):
            item["group_order"] = index

    def _group_event_snapshot_locked(self) -> tuple[
        list[dict[str, Any]], list[dict[str, str]], dict[str, str], list[str]
    ]:
        return (
            copy.deepcopy(self._events),
            copy.deepcopy(self._custom_groups),
            dict(self._group_assignments),
            list(self._group_order),
        )

    def _save_group_event_state_locked(
        self,
        snapshot: tuple[
            list[dict[str, Any]], list[dict[str, str]], dict[str, str], list[str]
        ],
    ) -> None:
        old_events, old_groups, old_assignments, old_order = snapshot
        self._ensure_writes_available()
        transaction = {
            "version": 1,
            "events": old_events,
            "group_state": {
                "version": 2,
                "groups": old_groups,
                "assignments": old_assignments,
                "order": old_order,
            },
        }
        self._group_event_transaction_active = True
        try:
            try:
                self.ensure_private_storage()
                atomic_write_text(
                    self.transaction_path,
                    json.dumps(transaction, ensure_ascii=False, indent=2),
                    mode=0o600,
                )
                self._save_group_state()
                self._save()
                self.transaction_path.unlink(missing_ok=True)
            except Exception:
                (self._events, self._custom_groups,
                 self._group_assignments, self._group_order) = snapshot
                rollback_ok = True
                try:
                    self._save_group_state()
                    self._save()
                except Exception:
                    rollback_ok = False
                if rollback_ok:
                    self.transaction_path.unlink(missing_ok=True)
                # If rollback also fails, leave the journal in place. The next
                # service start restores both files from the same pre-mutation state.
                raise
        finally:
            self._group_event_transaction_active = False

    def _apply_assignment_locked(self, item: dict[str, Any]) -> bool:
        sid = str(item.get("session_id") or item.get("thread_id") or "")
        assigned = self._group_assignments.get(sid, "")
        current = str(item.get("group_id") or "")
        if assigned == current:
            return False
        if assigned:
            item["group_id"] = assigned
        else:
            item.pop("group_id", None)
        item.pop("group_order", None)
        return True

    def _reconcile_event_groups(self) -> bool:
        valid = {group["id"] for group in self._custom_groups}
        self._group_assignments = {
            sid: group_id
            for sid, group_id in self._group_assignments.items()
            if group_id in valid
        }
        changed = False
        for item in self._events:
            changed = self._apply_assignment_locked(item) or changed
        return changed

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)][-_MAX_EVENTS:]
        except (OSError, ValueError):
            pass
        return []

    def _save(self) -> None:
        self._ensure_writes_available()
        self.ensure_private_storage()
        atomic_write_text(self.path, json.dumps(
            self._events[-_MAX_EVENTS:], ensure_ascii=False, indent=2),
            mode=0o600)

    @staticmethod
    def _enqueue_update(
        queue: asyncio.Queue[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        """Deliver the latest ledger change without an unbounded SSE backlog."""
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            payload = {
                "generation": payload["generation"],
                "revision": payload["revision"],
                "summary": payload["summary"],
                "resync": True,
            }
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(payload)

    def _publish_locked(
        self,
        *,
        item: dict[str, Any] | None = None,
        acked_ids: list[str] | None = None,
        resync: bool = False,
    ) -> None:
        """Fan out a compact transition to SSE subscribers.

        Activity mutations run in FastAPI worker threads (and chat explicitly
        uses ``asyncio.to_thread``), so subscribers retain their owning event
        loops and delivery crosses the boundary via ``call_soon_threadsafe``.
        """
        self._revision += 1
        summary = _summarize([dict(x) for x in self._events])
        summary["generation"] = self._generation
        summary["revision"] = self._revision
        payload: dict[str, Any] = {
            "generation": self._generation,
            "revision": self._revision,
            "summary": summary,
            "custom_groups": self._group_payload_locked(),
            "group_order": self._group_order_payload_locked(),
        }
        if resync:
            payload["resync"] = True
        if item is not None:
            payload["item"] = dict(item)
        if acked_ids:
            payload["acked_ids"] = list(acked_ids)
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue, loop in tuple(self._subscribers.items()):
            try:
                loop.call_soon_threadsafe(self._enqueue_update, queue, payload)
            except RuntimeError:
                stale.append(queue)
        for queue in stale:
            self._subscribers.pop(queue, None)

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Subscribe to future task transitions from the current event loop."""
        self.initialize_runtime_state()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers[queue] = loop
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers.pop(queue, None)

    @property
    def revision(self) -> int:
        self.initialize_runtime_state()
        with self._lock:
            return self._revision

    @property
    def generation(self) -> str:
        self.initialize_runtime_state()
        with self._lock:
            return self._generation

    def _collapse_sessions(self) -> bool:
        latest: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for item in self._events:
            sid = str(item.get("session_id") or item.get("thread_id") or "")
            if not sid:
                anonymous.append(item)
                continue
            item["session_id"] = sid
            old = latest.pop(sid, None)
            if old:
                item["turn_count"] = max(int(item.get("turn_count") or 1),
                                         int(old.get("turn_count") or 1) + 1)
            latest[sid] = item
        collapsed = anonymous + list(latest.values())
        changed = len(collapsed) != len(self._events)
        self._events = collapsed[-_MAX_EVENTS:]
        return changed

    def _metadata(self, sid: str) -> tuple[str, str, str]:
        meta = sessions.get_session(sid) or {}
        cwd = str(meta.get("cwd") or ROOT)
        return (str(meta.get("name") or "Muse task"), cwd,
                Path(cwd).name or "Workspace")

    def _latest(self, sid: str) -> dict[str, Any] | None:
        return next((x for x in reversed(self._events)
                     if x.get("session_id") == sid), None)

    def _live_session_ids(self) -> set[str]:
        """Session ids the frontend can still open.

        ``sessions.list_sessions()`` already excludes deleted sessions and rows
        owned by a removed workspace — exactly the sessions a task-center click
        would fail to open. Matching it here keeps the activity center from
        showing (and erroring on) phantom rows.
        """
        return {
            str(s.get("id"))
            for s in sessions.list_sessions()
            if s.get("id")
        }

    def _filter_live(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop ledger rows whose backing session no longer opens. Anonymous
        rows (no session_id) are always kept."""
        if not events:
            return []
        live = self._live_session_ids()
        return [
            x for x in events
            if not x.get("session_id") or str(x.get("session_id")) in live
        ]

    def start(
        self,
        sid: str,
        *,
        summary: str = "",
        kind: str = "turn",
        source_id: str = "",
        activity_source: str = "",
        owner_id: str = "",
    ) -> dict[str, Any]:
        self.initialize_runtime_state()
        now = time.time()
        name, workspace, workspace_name = self._metadata(sid)
        with self._lock:
            item = self._latest(sid)
            if item is None:
                item = {"id": uuid.uuid4().hex, "session_id": sid,
                        "turn_count": 0}
                self._events.append(item)
            item.update(
                kind=(kind or "turn")[:40],
                # Chat turns use this delivery-class field to distinguish a
                # reply the user is already watching from work that completed
                # independently (durable queue / SDK background task).  Keep
                # it on the ledger row so foreground boot/tab-switch ACKs do
                # not have to infer the source from a transient SSE frame.
                activity_source=(activity_source or (
                    "direct" if (kind or "turn") == "turn" else kind
                ))[:40],
                workspace=workspace, workspace_name=workspace_name,
                session_name=name, state="running",
                task_summary=(summary or item.get("task_summary") or name)[:500],
                status_detail="", started_at=now, finished_at=None,
                updated_at=now, needs_attention=False, read=True,
                turn_count=int(item.get("turn_count") or 0) + 1,
            )
            self._apply_assignment_locked(item)
            if source_id:
                item["source_id"] = source_id[:200]
            else:
                item.pop("source_id", None)
            if owner_id:
                item["owner_id"] = owner_id[:200]
            else:
                item.pop("owner_id", None)
            self._events = self._events[-_MAX_EVENTS:]
            self._save()
            self._publish_locked(item=item)
            return dict(item)

    def set_state(self, sid: str, state: str, *, detail: str = "") -> dict[str, Any]:
        self.initialize_runtime_state()
        if state not in _ACTIVE:
            raise ValueError("invalid activity state")
        with self._lock:
            item = self._latest(sid)
        if item is None or item.get("state") in _TERMINAL:
            self.start(sid)
        with self._lock:
            item = self._latest(sid)
            assert item is not None
            item.update(state=state, status_detail=detail[:500],
                        updated_at=time.time(),
                        needs_attention=state in {"waiting_approval", "paused"}, read=True)
            self._save()
            self._publish_locked(item=item)
            return dict(item)

    def resume(self, sid: str) -> bool:
        """Resume only the currently waiting turn; never revive a terminal row."""
        self.initialize_runtime_state()
        with self._lock:
            item = self._latest(sid)
            if item is None or item.get("state") not in {"waiting_approval", "paused"}:
                return False
            item.update(state="running", status_detail="", updated_at=time.time(),
                        needs_attention=False, read=True)
            self._save()
            self._publish_locked(item=item)
            return True

    def finish(
        self,
        sid: str,
        status: str,
        *,
        activity_source: str = "",
        owner_id: str = "",
        mark_read: bool | None = None,
    ) -> dict[str, Any]:
        self.initialize_runtime_state()
        state = "completed" if status == "completed" else (
            "cancelled" if status in {"cancelled", "interrupted"} else "failed")
        with self._lock:
            item = self._latest(sid)
            if item is None:
                # A normal owner may only close the Activity incarnation it
                # successfully started. If its start failed before persisting
                # a row, synthesizing one here would create a ghost completed
                # task. Ownerless deletion/repair remains authoritative and
                # may create a terminal repair row when no ledger entry exists.
                if owner_id:
                    return {}
                # RLock makes the synthesized start atomic with the owner
                # check below. A concurrent newer start can only happen after
                # this terminal write, never in the old check/start gap.
                self.start(
                    sid,
                    activity_source=activity_source,
                    owner_id=owner_id,
                )
                item = self._latest(sid)
            assert item is not None
            # A detached watcher can finish after a newer foreground turn has
            # reused this session row. Only the owner that started the current
            # incarnation may close it; deletion intentionally omits owner_id
            # to force a terminal state for whichever incarnation remains.
            if owner_id and item.get("owner_id") != owner_id:
                return dict(item)
            owner_revoked = False
            if not owner_id:
                # Ownerless finish is reserved for explicit deletion/repair.
                # Revoke the current incarnation so its late ordinary owner can
                # no longer overwrite this authoritative terminal state.
                owner_revoked = item.pop("owner_id", None) is not None
            # Terminal delivery can be observed by more than one cleanup path
            # (for example the Result boundary and an outer pump fallback).
            # Repeating the same terminal state must be a true no-op: rewriting
            # it would reset a user's read ACK, reorder the row, and emit a
            # duplicate SSE revision. A different terminal state is still
            # allowed to correct later authoritative information.
            if item.get("state") == state and state in _TERMINAL:
                mark_read_changed = bool(
                    mark_read is True and not item.get("read")
                )
                if mark_read_changed:
                    # A retry may observe a terminal row written by an earlier
                    # cleanup before the background path supplied its atomic-read
                    # intent.  Repair only the read bit; retain the original
                    # terminal timestamp/order and publish one coherent state.
                    item.update(read=True, needs_attention=False)
                    self._save()
                    self._publish_locked(item=item)
                elif owner_revoked:
                    # Persist only the ownership revocation. Do not change the
                    # timestamp/read bit or publish another SSE revision: the
                    # terminal state itself is still an idempotent no-op.
                    self._save()
                return dict(item)
            now = time.time()
            if activity_source:
                item["activity_source"] = activity_source[:40]
            # Background logical turns surface their result as a normal Agent
            # bubble in chat.  Their Activity row remains useful history, but
            # must become terminal+read in this same locked mutation: publishing
            # an unread finish and ACKing it afterwards exposes a transient red
            # badge to SSE clients.  Other callers retain the established unread
            # completion/failure semantics by leaving ``mark_read`` unspecified.
            terminal_read = (
                bool(mark_read) if mark_read is not None
                else state == "cancelled"
            )
            item.update(state=state, finished_at=now, updated_at=now,
                        needs_attention=False, read=terminal_read,
                        status_detail={"completed": "Task completed",
                                       "failed": "Task failed",
                                       "cancelled": "Task cancelled"}[state])
            self._save()
            self._publish_locked(item=item)
            return dict(item)

    def list(self, limit: int = 100, *, filter_live: bool = False) -> list[dict[str, Any]]:
        self.initialize_runtime_state()
        with self._lock:
            events = [dict(x) for x in self._events]
        if filter_live:
            events = self._filter_live(events)
        events.sort(key=_activity_at, reverse=True)
        return events[:min(max(limit, 1), _MAX_EVENTS)]

    def summary(self, *, filter_live: bool = False) -> dict[str, Any]:
        self.initialize_runtime_state()
        with self._lock:
            events = [dict(x) for x in self._events]
            generation = self._generation
            revision = self._revision
        if filter_live:
            events = self._filter_live(events)
        result = _summarize(events)
        result["generation"] = generation
        result["revision"] = revision
        return result

    def snapshot(self, limit: int = 100, *, filter_live: bool = False) -> dict[str, Any]:
        """Return rows and counters from the same locked ledger snapshot."""
        self.initialize_runtime_state()
        with self._lock:
            events = [dict(x) for x in self._events]
            custom_groups = self._group_payload_locked()
            group_order = self._group_order_payload_locked()
            generation = self._generation
            revision = self._revision
        if filter_live:
            events = self._filter_live(events)
        summary = _summarize(events)
        summary["generation"] = generation
        summary["revision"] = revision
        ordered = sorted(events, key=_activity_at, reverse=True)
        return {
            "events": ordered[:min(max(limit, 1), _MAX_EVENTS)],
            "summary": summary,
            "custom_groups": custom_groups,
            "group_order": group_order,
        }

    def list_groups(self) -> list[dict[str, str]]:
        self.initialize_runtime_state()
        with self._lock:
            return self._group_payload_locked()

    def group_state(self) -> dict[str, Any]:
        self.initialize_runtime_state()
        with self._lock:
            return {
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def create_group(self, name: str, color: str = "blue") -> dict[str, Any]:
        self.initialize_runtime_state()
        clean_name = self._group_name(name)
        clean_color = self._group_color(color)
        with self._lock:
            if len(self._custom_groups) >= _MAX_CUSTOM_GROUPS:
                raise ValueError("too many custom groups")
            if any(group["name"].casefold() == clean_name.casefold()
                   for group in self._custom_groups):
                raise ValueError("group name already exists")
            group = {
                "id": uuid.uuid4().hex,
                "name": clean_name,
                "color": clean_color,
            }
            self._custom_groups.append(group)
            ungrouped_at = self._group_order.index(_UNGROUPED_GROUP_ID)
            self._group_order.insert(ungrouped_at, group["id"])
            self._save_group_state()
            self._publish_locked()
            return {
                "generation": self._generation,
                "revision": self._revision,
                "group": dict(group),
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
    ) -> dict[str, Any] | None:
        self.initialize_runtime_state()
        with self._lock:
            group = next((row for row in self._custom_groups
                          if row["id"] == group_id), None)
            if group is None:
                return None
            clean_name = (
                group["name"] if name is None else self._group_name(name)
            )
            clean_color = (
                group["color"] if color is None else self._group_color(color)
            )
            if any(row["id"] != group_id
                   and row["name"].casefold() == clean_name.casefold()
                   for row in self._custom_groups):
                raise ValueError("group name already exists")
            changed = group["name"] != clean_name or group["color"] != clean_color
            group.update(name=clean_name, color=clean_color)
            if changed:
                self._save_group_state()
                self._publish_locked()
            return {
                "generation": self._generation,
                "revision": self._revision,
                "group": dict(group),
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def reorder_groups(self, group_ids: list[str]) -> dict[str, Any]:
        self.initialize_runtime_state()
        with self._lock:
            custom_ids = [group["id"] for group in self._custom_groups]
            full_ids = custom_ids + [_UNGROUPED_GROUP_ID]
            requested = [str(group_id or "").strip() for group_id in group_ids]
            if len(requested) == len(full_ids) and set(requested) == set(full_ids):
                requested = [
                    group_id for group_id in requested
                    if group_id != _UNGROUPED_GROUP_ID
                ]
            elif len(requested) != len(custom_ids) or set(requested) != set(custom_ids):
                raise ValueError("group order must contain every group exactly once")
            if len(requested) != len(set(requested)):
                raise ValueError("group order must contain every group exactly once")
            requested.append(_UNGROUPED_GROUP_ID)

            if requested != self._group_order:
                self._group_order = requested
                lookup = {group["id"]: group for group in self._custom_groups}
                self._custom_groups = [
                    lookup[group_id] for group_id in requested if group_id in lookup
                ]
                self._save_group_state()
                self._publish_locked()
            return {
                "generation": self._generation,
                "revision": self._revision,
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def delete_group(self, group_id: str) -> dict[str, Any] | None:
        self.initialize_runtime_state()
        with self._lock:
            if not any(group["id"] == group_id for group in self._custom_groups):
                return None
            snapshot = self._group_event_snapshot_locked()
            self._custom_groups = [
                group for group in self._custom_groups if group["id"] != group_id
            ]
            self._group_order = [
                value for value in self._group_order if value != group_id
            ]
            cleared_sessions = {
                sid for sid, assigned in self._group_assignments.items()
                if assigned == group_id
            }
            self._group_assignments = {
                sid: assigned
                for sid, assigned in self._group_assignments.items()
                if assigned != group_id
            }
            ungrouped = self._ordered_group_items_locked("")
            moved = self._ordered_group_items_locked(group_id)
            for item in moved:
                item.pop("group_id", None)
            ordered_ungrouped = ungrouped + moved
            self._renumber_group_locked(ordered_ungrouped)
            self._save_group_event_state_locked(snapshot)
            self._publish_locked(resync=True)
            return {
                "generation": self._generation,
                "revision": self._revision,
                "cleared_sessions": len(cleared_sessions),
                "items": [dict(row) for row in ordered_ungrouped],
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def set_group(
        self,
        event_id: str,
        group_id: str,
        *,
        before_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        self.initialize_runtime_state()
        target = str(group_id or "").strip()
        before = None if before_event_id is None else str(before_event_id or "").strip()
        with self._lock:
            if target and not any(group["id"] == target
                                  for group in self._custom_groups):
                raise ValueError("activity group not found")
            item = next((row for row in self._events
                         if row.get("id") == event_id), None)
            if item is None:
                return None
            snapshot = self._group_event_snapshot_locked()
            sid = str(item.get("session_id") or item.get("thread_id") or "")
            current = self._event_group_id(item)

            # Old clients only assign a lane. Preserve that API while making a
            # freshly moved row float above an existing manual order.
            if before is None:
                if target == current:
                    return {
                        "generation": self._generation,
                        "revision": self._revision,
                        "item": dict(item),
                        "custom_groups": self._group_payload_locked(),
                        "group_order": self._group_order_payload_locked(),
                    }
                if target:
                    if sid:
                        self._group_assignments[sid] = target
                    item["group_id"] = target
                else:
                    if sid:
                        self._group_assignments.pop(sid, None)
                    item.pop("group_id", None)
                item.pop("group_order", None)
                self._save_group_event_state_locked(snapshot)
                self._publish_locked(item=item)
                return {
                    "generation": self._generation,
                    "revision": self._revision,
                    "item": dict(item),
                    "custom_groups": self._group_payload_locked(),
                    "group_order": self._group_order_payload_locked(),
                }

            if before == event_id and target == current:
                return {
                    "generation": self._generation,
                    "revision": self._revision,
                    "item": dict(item),
                    "items": [dict(item)],
                    "custom_groups": self._group_payload_locked(),
                    "group_order": self._group_order_payload_locked(),
                }

            source_items = self._ordered_group_items_locked(
                current, exclude_id=event_id,
            )
            target_items = (
                source_items if target == current
                else self._ordered_group_items_locked(target, exclude_id=event_id)
            )
            if before:
                insert_at = next((
                    index for index, row in enumerate(target_items)
                    if str(row.get("id") or "") == before
                ), -1)
                if insert_at < 0:
                    raise ValueError("activity placement anchor not found in target group")
            else:
                insert_at = len(target_items)

            if target:
                if sid:
                    self._group_assignments[sid] = target
                item["group_id"] = target
            else:
                if sid:
                    self._group_assignments.pop(sid, None)
                item.pop("group_id", None)
            target_items.insert(insert_at, item)
            if target != current:
                self._renumber_group_locked(source_items)
            self._renumber_group_locked(target_items)
            changed_items = source_items + (
                target_items if target != current else []
            )
            if target == current:
                changed_items = target_items

            self._save_group_event_state_locked(snapshot)
            self._publish_locked(resync=True)
            return {
                "generation": self._generation,
                "revision": self._revision,
                "item": dict(item),
                "items": [dict(row) for row in changed_items],
                "custom_groups": self._group_payload_locked(),
                "group_order": self._group_order_payload_locked(),
            }

    def inherit_session(
        self,
        source_sid: str,
        child_sid: str,
        *,
        successor: bool = False,
    ) -> dict[str, Any]:
        """Inherit a fork's durable group placement and optional activity row.

        Ordinary forks copy only the custom-group assignment: the source remains
        an independent conversation and the child gets its own row on first use.
        A true successor (for example compact recovery) moves the existing row,
        preserving its id, ordering, pin/read state and turn lineage.  The shared
        group/event journal makes the mutation atomic, while the child-key checks
        make a committed retry a no-op.
        """
        self.initialize_runtime_state()
        source = str(source_sid or "").strip()
        child = str(child_sid or "").strip()
        if not source or not child or source == child:
            raise ValueError("distinct source and child sessions are required")
        child_name, _, _ = self._metadata(child)
        with self._lock:
            source_item = self._latest(source)
            child_item = self._latest(child)
            source_group = self._group_assignments.get(source, "")
            child_group = self._group_assignments.get(child, "")

            if child_group and source_group and child_group != source_group:
                raise ValueError("child activity group already differs from source")
            if successor and source_item is not None and child_item is not None:
                raise ValueError("child activity lineage already exists")

            snapshot = self._group_event_snapshot_locked()
            changed = False
            if source_group and not child_group:
                self._group_assignments[child] = source_group
                changed = True
            if successor:
                if source_item is not None:
                    source_item["session_id"] = child
                    source_item.pop("thread_id", None)
                    source_item["session_name"] = child_name
                    child_item = source_item
                    changed = True
                if source in self._group_assignments:
                    self._group_assignments.pop(source, None)
                    changed = True

            if changed:
                self._save_group_event_state_locked(snapshot)
                if successor and child_item is not None:
                    self._publish_locked(item=child_item)
            return {
                "generation": self._generation,
                "revision": self._revision,
                "item": dict(child_item) if child_item is not None else None,
                "group_id": self._group_assignments.get(child, ""),
                "successor": successor,
            }

    def discard_session(self, sid: str) -> bool:
        """Remove a provisional child's Activity projection transactionally."""
        self.initialize_runtime_state()
        target = str(sid or "").strip()
        if not target:
            return False
        with self._lock:
            has_assignment = target in self._group_assignments
            has_event = any(
                str(item.get("session_id") or item.get("thread_id") or "") == target
                for item in self._events
            )
            if not has_assignment and not has_event:
                return False
            snapshot = self._group_event_snapshot_locked()
            self._group_assignments.pop(target, None)
            self._events = [
                item for item in self._events
                if str(item.get("session_id") or item.get("thread_id") or "") != target
            ]
            self._save_group_event_state_locked(snapshot)
            self._publish_locked(resync=True)
            return True

    def rename_session(self, sid: str, name: str) -> dict[str, Any] | None:
        """Update only the mutable display name for a conversation row.

        A rename is session metadata, not a task transition.  In particular it
        must not touch ``updated_at``, read state, or the task summary: doing so
        would move an old row to the front of the timeline or make a completed
        result look new.  Publishing the otherwise unchanged row lets every
        open Activity Center converge immediately through its existing SSE
        connection.
        """
        self.initialize_runtime_state()
        with self._lock:
            item = self._latest(sid)
            if item is None:
                return None
            target = str(name)
            if str(item.get("session_name") or "") != target:
                item["session_name"] = target
                self._save()
                self._publish_locked(item=item)
            return {
                "generation": self._generation,
                "revision": self._revision,
                "item": dict(item),
            }

    def migrate_group_to_successor(
        self,
        source_sid: str,
        successor_sid: str,
    ) -> bool:
        """Carry a runtime rollover's activity-group lane onto its fork.

        When a session with pending background work forks a same-named
        successor (runtime rollover), the source is hidden and the fork keeps
        running.  If the source had been assigned to a custom activity group,
        the fork must inherit that lane so the rollover stays invisible to the
        user instead of surfacing a new ungrouped row.
        """
        self.initialize_runtime_state()
        source_sid = str(source_sid or "")
        successor_sid = str(successor_sid or "")
        if not source_sid or not successor_sid or source_sid == successor_sid:
            return False
        with self._lock:
            assigned = self._group_assignments.get(source_sid, "")
            if not assigned:
                return False
            self._group_assignments.pop(source_sid, None)
            self._group_assignments[successor_sid] = assigned
            events_changed = self._reconcile_event_groups()
            if events_changed:
                self._save()
            self._save_group_state()
            self._publish_locked(resync=True)
            return True

    def set_pin(self, event_id: str, pinned: bool) -> dict[str, Any] | None:
        """Persist a pin and return the exact ledger revision it belongs to."""
        self.initialize_runtime_state()
        with self._lock:
            item = next(
                (row for row in self._events if row.get("id") == event_id),
                None,
            )
            if item is None:
                return None
            target = bool(pinned)
            if bool(item.get("pinned")) != target:
                item["pinned"] = target
                self._save()
                self._publish_locked(item=item)
            return {
                "generation": self._generation,
                "revision": self._revision,
                "item": dict(item),
            }

    def ack(self, event_id: str | None = None, *, sid: str | None = None) -> int:
        self.initialize_runtime_state()
        changed = 0
        acked_ids: list[str] = []
        with self._lock:
            for item in self._events:
                if event_id is not None and item.get("id") != event_id:
                    continue
                if sid is not None and item.get("session_id") != sid:
                    continue
                if not item.get("read"):
                    item["read"] = True
                    changed += 1
                    if item.get("id"):
                        acked_ids.append(str(item["id"]))
            if changed:
                self._save()
                self._publish_locked(acked_ids=acked_ids)
        return changed


# Importing the API surface must stay read-only for hermetic test collection.
# The application lifespan initializes it before accepting requests; public
# methods retain a lazy fallback for direct and TestClient use.
activity = ActivityService(initialize_runtime_state=False)
