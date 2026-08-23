"""Persistent workspace file index, replayable deltas, and shared watchers."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from watchfiles import Change, awatch

from .auth import require_token
from .capability_tickets import tickets
from .files import INTERNAL_DIR_NAME, TRASH_DIR_NAME
from .observability import elapsed_ms, is_slow, monotonic, perf_event, short_id
from .workspace_store import (
    WorkspaceScanCancelled,
    WorkspaceStore,
    is_ignored_descendant,
)
from .workspaces import registry, resolve_workspace_root


router = APIRouter(prefix="/api/files", tags=["files"])

_QUEUE_LIMIT = 8
_WATCH_DEBOUNCE_MS = 350
_WATCH_STEP_MS = 100
_WATCH_RETRY_S = 1.5
_RECONCILE_RETRY_BASE_S = 0.25
_RECONCILE_RETRY_MAX_S = 30.0
_WATCH_LINGER_S = 30.0
_MAX_IDLE_WATCHERS = 3
_RECONCILE_BACKOFF_START_S = 0.25
_RECONCILE_BACKOFF_CAP_S = 5.0
_EVENT_TICKET_TTL_S = 45
_DATABASE_MAINTENANCE_DELAY_S = 30.0
_EXCLUDED_DIRS = frozenset({TRASH_DIR_NAME, INTERNAL_DIR_NAME})
_POLLING_ENV = os.getenv("WATCHFILES_FORCE_POLLING")
_FORCE_POLLING: bool | None = (
    None
    if _POLLING_ENV is None
    else _POLLING_ENV.strip().lower() in {"1", "true", "yes", "on"}
)


def _perf_event(event: str, /, **fields: Any) -> None:
    """Keep diagnostics from changing file-index lifecycle semantics."""
    try:
        perf_event(event, **fields)
    except Exception:
        pass


@dataclass
class _WatchState:
    root: Path
    workspace_id: str = ""
    name: str = ""
    primary: bool = False
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(
        default_factory=set,
    )
    task: asyncio.Task[None] | None = None
    reconcile_task: asyncio.Task[None] | None = None
    force_polling: bool | None = None
    initialized: bool = False
    reconcile_error: Exception | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watch_revision: int = 0
    watch_stop_event: asyncio.Event | None = None
    watch_ready: asyncio.Event = field(default_factory=asyncio.Event)
    watch_paths: tuple[Path, ...] = ()
    needs_closing_reconcile: bool = False
    reconcile_pending: bool = False
    reconcile_running: bool = False
    reconcile_attempts: int = 0
    reconcile_failures: int = 0
    reconcile_retry_at: float = 0.0
    reconcile_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    scan_progress: dict[str, Any] = field(default_factory=dict)
    scan_cancel: threading.Event = field(default_factory=threading.Event)
    stop_task: asyncio.Task[None] | None = None
    queue_overflow_active: bool = False


def _normalise_bootstrap_parents(
    parents: list[str] | None,
) -> list[str] | None:
    """Validate and canonicalize client-selected workspace-relative parents."""
    if parents is None:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in parents:
        if "\x00" in raw:
            raise ValueError("parent paths cannot contain NUL bytes")
        # File-tree paths use POSIX separators on the wire. Treat a backslash
        # as a separator as well so Windows-shaped traversal/absolute paths do
        # not become harmless-looking Linux filenames during validation.
        portable = raw.replace("\\", "/")
        relative = PurePosixPath(portable)
        if relative.is_absolute() or PureWindowsPath(raw).is_absolute():
            raise ValueError("parent paths must be relative")
        if ".." in relative.parts:
            raise ValueError("parent paths cannot contain '..'")
        normalized_path = "" if str(relative) == "." else relative.as_posix()
        if normalized_path not in seen:
            seen.add(normalized_path)
            normalized.append(normalized_path)
    return normalized


class WorkspaceBootstrapRequest(BaseModel):
    show_hidden: bool = False
    # POST is always the bounded contract. Omitting parents means root-only;
    # the backward-compatible full snapshot remains available through GET.
    parents: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("parents")
    @classmethod
    def validate_parents(
        cls,
        value: list[str],
    ) -> list[str]:
        return _normalise_bootstrap_parents(value) or []


def _normalise_changes(
    root: Path,
    changes: set[tuple[Change, str]],
) -> list[dict[str, str]]:
    """Convert native absolute paths to a stable, non-leaking wire format."""
    root = root.resolve()
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for change, raw_path in changes:
        try:
            relative = Path(raw_path).relative_to(root)
        except ValueError:
            continue
        path = relative.as_posix()
        if (
            not path
            or path == "."
            or any(part in _EXCLUDED_DIRS for part in relative.parts)
        ):
            continue
        kind = {
            Change.added: "added",
            Change.modified: "modified",
            Change.deleted: "deleted",
        }.get(change)
        if kind:
            rows[(kind, path)] = {"type": kind, "path": path}
    return sorted(
        rows.values(),
        key=lambda row: (row["path"], row["type"]),
    )


def _watch_filter_for(root: Path):
    root = root.resolve()

    def include(_change: Change, path: str) -> bool:
        try:
            relative = Path(path).relative_to(root)
        except ValueError:
            return False
        parts = relative.parts
        if any(part in _EXCLUDED_DIRS for part in parts):
            return False
        # Preserve the generated directory node itself, but ignore recursive
        # churn. Users may still expand it through the lazy `/list` fallback.
        return not is_ignored_descendant(relative)

    return include


def _is_watch_resource_error(exc: Exception) -> bool:
    if isinstance(exc, OSError) and exc.errno in {
        errno.ENOSPC,
        errno.EMFILE,
        errno.ENFILE,
    }:
        return True
    detail = str(exc).lower()
    return any(
        marker in detail
        for marker in (
            "watch limit",
            "maxfileswatch",
            "no space left on device",
            "too many open files",
            "os error 28",
            "os error 24",
        )
    )


class FileWatchManager:
    """Own durable indexes and one on-demand watcher per subscribed workspace."""

    def __init__(self, store: WorkspaceStore | None = None) -> None:
        self.store = store or WorkspaceStore(registry.primary)
        self._states: dict[Path, _WatchState] = {}
        self._idle_watchers: OrderedDict[Path, None] = OrderedDict()
        self._lock = asyncio.Lock()
        # Registration/state I/O intentionally runs outside `_lock`, but an
        # ensure and remove for the same path must still be one lifecycle.
        # Otherwise a slow first registration can reinstall an orphan state
        # and SQLite row after the registry/API deletion has completed.
        self._lifecycle_locks: dict[Path, asyncio.Lock] = {}
        self._started = False
        self._maintenance_task: asyncio.Task[None] | None = None

    def _cancel_idle_stop_locked(
        self,
        state: _WatchState,
    ) -> asyncio.Task[None] | None:
        """Mark a watcher active again while the manager lock is held."""
        self._idle_watchers.pop(state.root, None)
        stop_task = state.stop_task
        state.stop_task = None
        if (
            stop_task is not None
            and stop_task is not asyncio.current_task()
            and not stop_task.done()
        ):
            stop_task.cancel()
            return stop_task
        return None

    def _start_watcher_locked(self, state: _WatchState) -> bool:
        """Start one fresh watcher generation while the manager lock is held."""
        if state.task is not None and not state.task.done():
            return False
        state.task = None
        state.watch_revision += 1
        state.watch_ready.clear()
        state.watch_paths = ()
        state.task = asyncio.create_task(
            self._watch(state),
            name=f"muselab-files:{state.workspace_id}",
        )
        return True

    @staticmethod
    def _record_reconcile_retry(state: _WatchState) -> None:
        """Apply workspace-local exponential backoff after a failed/partial pass."""
        state.reconcile_failures += 1
        delay = min(
            _RECONCILE_RETRY_MAX_S,
            _RECONCILE_RETRY_BASE_S
            * (2 ** min(state.reconcile_failures - 1, 16)),
        )
        state.reconcile_retry_at = monotonic() + delay

    @staticmethod
    def _reset_reconcile_retry(state: _WatchState) -> None:
        state.reconcile_failures = 0
        state.reconcile_retry_at = 0.0

    def _queue_reconcile_locked(self, state: _WatchState) -> None:
        """Coalesce full scans, retaining a requested closing pass."""
        if (
            state.reconcile_task is not None
            and not state.reconcile_task.done()
        ):
            # This is important when a watcher is restarted while an older
            # reconciliation is already scanning. The current pass did not
            # necessarily start after the new watcher was armed, so require a
            # subsequent watch-first closing pass.
            if state.reconcile_running:
                state.reconcile_pending = True
            return
        if not state.initialized:
            state.ready.clear()
        state.reconcile_task = asyncio.create_task(
            self._reconcile_after_watch(state),
            name=f"muselab-files-reconcile:{state.workspace_id}",
        )

    def _prepare_workspace_locked(
        self,
        state: _WatchState,
        *,
        start_watcher: bool,
        rescan: bool,
        created: bool = False,
    ) -> asyncio.Task[None] | None:
        """Apply watcher/reconcile intent to a state under the manager lock."""
        cancelled_stop: asyncio.Task[None] | None = None
        watch_started = False
        if start_watcher:
            cancelled_stop = self._cancel_idle_stop_locked(state)
            watch_started = self._start_watcher_locked(state)
        if created or rescan or watch_started or not state.initialized:
            self._queue_reconcile_locked(state)
        return cancelled_stop

    @staticmethod
    def _detach_watcher_locked(
        state: _WatchState,
    ) -> asyncio.Task[None] | None:
        """Detach a watcher generation and invalidate its armed snapshot."""
        task = state.task
        state.task = None
        state.watch_revision += 1
        state.watch_ready.clear()
        state.watch_paths = ()
        if state.watch_stop_event is not None:
            state.watch_stop_event.set()
        return task if task is not None and not task.done() else None

    async def _maintain_database_after_ready(self) -> None:
        """Run bounded index maintenance after readiness, never before it."""
        try:
            await asyncio.sleep(_DATABASE_MAINTENANCE_DELAY_S)
            worker = asyncio.create_task(
                asyncio.to_thread(self.store.maintain_database),
                name="muselab-files-database-maintenance-worker",
            )
            try:
                maintenance = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A to_thread call cannot be stopped by cancelling its awaiter.
                # Keep ownership until the bounded incremental operation exits
                # so shutdown never closes the store underneath a live worker.
                await worker
                raise
            if maintenance["action"] != "none":
                before = maintenance["before"]
                after = maintenance["after"]
                sys.stderr.write(
                    "[files] workspace index maintenance "
                    f"action={maintenance['action']} "
                    f"free_pages={before['freelist_count']}->"
                    f"{after['freelist_count']} "
                    f"duration_ms={maintenance['duration_ms']}\n"
                )
                sys.stderr.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Indexing remains available when optional compaction cannot
            # acquire a lock or inspect filesystem headroom.
            sys.stderr.write(
                "[files] workspace index maintenance skipped "
                f"({type(exc).__name__})\n"
            )
            sys.stderr.flush()

    async def start(self) -> None:
        """Initialize durable metadata without recursively watching every root."""
        async with self._lock:
            if self._started:
                return
            self._started = True
        try:
            await asyncio.to_thread(self.store.initialize)
            for entry in registry.list():
                await asyncio.to_thread(
                    self.store.register_workspace,
                    entry.id,
                    Path(entry.path),
                    entry.name,
                    primary=entry.primary,
                )
            async with self._lock:
                if self._started:
                    self._maintenance_task = asyncio.create_task(
                        self._maintain_database_after_ready(),
                        name="muselab-files-database-maintenance",
                    )
        except Exception:
            async with self._lock:
                self._started = False
            raise

    @staticmethod
    def _resolved_lifecycle_root(root: Path) -> Path:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            candidate = registry.primary / candidate
        return candidate.resolve()

    async def register_workspace(
        self,
        root: Path,
        name: str | None = None,
    ):
        """Atomically register the registry and durable watcher generation."""
        resolved_root = self._resolved_lifecycle_root(root)
        lifecycle_lock = self._lifecycle_locks.setdefault(
            resolved_root, asyncio.Lock())
        async with lifecycle_lock:
            entry = registry.register(resolved_root, name)
            await self._ensure_workspace_serialized(resolved_root)
            return entry

    async def unregister_workspace(self, root: Path):
        """Atomically remove registry, watcher, and durable index state."""
        resolved_root = self._resolved_lifecycle_root(root)
        lifecycle_lock = self._lifecycle_locks.setdefault(
            resolved_root, asyncio.Lock())
        async with lifecycle_lock:
            entry = registry.entry_for(resolved_root)
            registry.remove(resolved_root)
            await self._remove_workspace_serialized(entry.id)
            return entry

    async def ensure_workspace(
        self,
        root: Path,
        *,
        start_watcher: bool | None = None,
        rescan: bool = False,
    ) -> _WatchState:
        root = root.resolve()
        lifecycle_lock = self._lifecycle_locks.setdefault(
            root, asyncio.Lock())
        async with lifecycle_lock:
            return await self._ensure_workspace_serialized(
                root,
                start_watcher=start_watcher,
                rescan=rescan,
            )

    async def _ensure_workspace_serialized(
        self,
        root: Path,
        *,
        start_watcher: bool | None = None,
        rescan: bool = False,
    ) -> _WatchState:
        """Ensure one root while holding its lifecycle lock."""
        entry = registry.entry_for(root)
        should_watch = bool(start_watcher)
        cancelled_stops: list[asyncio.Task[None]] = []

        # Bootstrap, delta, and SSE reconnect all pass through this method. Once
        # a state's registry metadata is current, keep that hot path entirely
        # in memory instead of opening SQLite twice per request.
        async with self._lock:
            state = self._states.get(root)
            if (
                state is not None
                and state.workspace_id == entry.id
                and state.name == entry.name
                and state.primary == entry.primary
            ):
                cancelled = self._prepare_workspace_locked(
                    state,
                    start_watcher=should_watch,
                    rescan=rescan,
                )
                if cancelled is not None:
                    cancelled_stops.append(cancelled)
                hot_state = state
            else:
                hot_state = None
        if hot_state is not None:
            if cancelled_stops:
                await asyncio.gather(
                    *cancelled_stops,
                    return_exceptions=True,
                )
            return hot_state

        # First use or changed registry metadata needs durable registration.
        # The recursive scan remains scheduled later and never runs under the
        # manager lock.
        await asyncio.to_thread(
            self.store.register_workspace,
            entry.id,
            root,
            entry.name,
            primary=entry.primary,
        )
        status = await asyncio.to_thread(self.store.state, entry.id)

        # Registry mutation happens before the workspace API calls into this
        # manager. Revalidate after slow SQLite I/O: a concurrent deletion must
        # not install state from the stale pre-I/O registry snapshot.
        try:
            current_entry = registry.entry_for(root)
        except ValueError:
            await asyncio.to_thread(
                self.store.remove_workspace,
                entry.id,
            )
            raise
        if current_entry != entry:
            entry = current_entry
            await asyncio.to_thread(
                self.store.register_workspace,
                entry.id,
                root,
                entry.name,
                primary=entry.primary,
            )
            status = await asyncio.to_thread(self.store.state, entry.id)

        async with self._lock:
            # Another concurrent first request may have installed the state
            # while SQLite I/O was in flight. Reuse it and only refresh metadata.
            state = self._states.get(root)
            created = state is None
            if state is None:
                state = _WatchState(
                    root=root,
                    workspace_id=entry.id,
                    name=entry.name,
                    primary=entry.primary,
                    force_polling=_FORCE_POLLING,
                    initialized=bool(status["initialized"]),
                )
                if state.initialized:
                    state.ready.set()
                self._states[root] = state
            else:
                state.workspace_id = entry.id
                state.name = entry.name
                state.primary = entry.primary
                if status["initialized"]:
                    state.initialized = True
                    state.ready.set()

            cancelled = self._prepare_workspace_locked(
                state,
                start_watcher=should_watch,
                rescan=rescan,
                created=created,
            )
            if cancelled is not None:
                cancelled_stops.append(cancelled)
        if cancelled_stops:
            await asyncio.gather(*cancelled_stops, return_exceptions=True)
        return state

    async def remove_workspace(
        self,
        workspace_id: str,
        root: Path | None = None,
    ) -> None:
        resolved_root = root.resolve() if root is not None else None
        if resolved_root is None:
            async with self._lock:
                resolved_root = next((
                    candidate
                    for candidate, state in self._states.items()
                    if state.workspace_id == workspace_id
                ), None)
        if resolved_root is None:
            await asyncio.to_thread(
                self.store.remove_workspace,
                workspace_id,
            )
            return
        lifecycle_lock = self._lifecycle_locks.setdefault(
            resolved_root, asyncio.Lock())
        async with lifecycle_lock:
            await self._remove_workspace_serialized(workspace_id)

    async def _remove_workspace_serialized(self, workspace_id: str) -> None:
        """Remove watcher and durable state under the root lifecycle lock."""
        cancel_tasks: list[asyncio.Task[None]] = []
        reconcile_task: asyncio.Task[None] | None = None
        async with self._lock:
            for root, state in tuple(self._states.items()):
                if state.workspace_id != workspace_id:
                    continue
                self._states.pop(root, None)
                self._idle_watchers.pop(root, None)
                cancel_tasks = [
                    task
                    for task in (
                        self._detach_watcher_locked(state),
                        state.stop_task,
                    )
                    if task is not None
                ]
                if (
                    state.reconcile_task is not None
                    and not state.reconcile_task.done()
                ):
                    reconcile_task = state.reconcile_task
                    state.reconcile_cancel.set()
                    state.scan_cancel.set()
                    if not state.reconcile_running:
                        reconcile_task.cancel()
                state.reconcile_pending = False
                state.reconcile_task = None
                state.stop_task = None
                self._close_subscribers(state)
                break
        for task in cancel_tasks:
            task.cancel()
        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
        if reconcile_task is not None:
            # Cancelling an asyncio task that is inside `to_thread()` does not
            # stop the worker thread. Await the mutation to settle, then delete
            # its durable row so a late reconcile cannot resurrect it.
            await asyncio.gather(reconcile_task, return_exceptions=True)
        await asyncio.to_thread(
            self.store.remove_workspace,
            workspace_id,
        )

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        root: Path,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        root = root.resolve()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_QUEUE_LIMIT,
        )
        cancelled_stops: list[asyncio.Task[None]] = []
        while True:
            state = await self.ensure_workspace(root)
            async with self._lock:
                # Workspace removal can interleave with the SQLite registration
                # above. Retry rather than attaching a queue to an orphan state.
                if self._states.get(root) is not state:
                    continue
                cancelled = self._cancel_idle_stop_locked(state)
                if cancelled is not None:
                    cancelled_stops.append(cancelled)
                # Adding the queue and starting/restarting its watcher are one
                # atomic transition. A late unsubscribe for an older queue can
                # therefore never stop this new subscriber's watcher.
                state.subscribers.add(queue)
                if self._start_watcher_locked(state):
                    self._queue_reconcile_locked(state)
                break
        if cancelled_stops:
            await asyncio.gather(*cancelled_stops, return_exceptions=True)
        try:
            yield queue
        finally:
            await self._unsubscribe(root, queue)

    async def _stop_after_linger(self, state: _WatchState) -> None:
        """Stop a still-idle watcher after the reconnect grace period."""
        await asyncio.sleep(_WATCH_LINGER_S)
        watcher: asyncio.Task[None] | None = None
        current = asyncio.current_task()
        async with self._lock:
            if (
                self._states.get(state.root) is not state
                or state.stop_task is not current
            ):
                return
            state.stop_task = None
            self._idle_watchers.pop(state.root, None)
            if state.subscribers:
                return
            watcher = self._detach_watcher_locked(state)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _schedule_idle_stop_locked(
        self,
        state: _WatchState,
    ) -> list[asyncio.Task[None]]:
        """Start linger and immediately enforce the bounded idle-watcher LRU."""
        cancelled: list[asyncio.Task[None]] = []
        if state.task is None or state.task.done():
            return cancelled
        if state.stop_task is None or state.stop_task.done():
            state.stop_task = asyncio.create_task(
                self._stop_after_linger(state),
                name=f"muselab-files-stop:{state.workspace_id}",
            )
        self._idle_watchers.pop(state.root, None)
        self._idle_watchers[state.root] = None

        while len(self._idle_watchers) > _MAX_IDLE_WATCHERS:
            stale_root, _ = self._idle_watchers.popitem(last=False)
            stale = self._states.get(stale_root)
            if stale is None or stale.subscribers:
                continue
            if stale.stop_task is not None:
                stop_task = stale.stop_task
                stale.stop_task = None
                if not stop_task.done():
                    stop_task.cancel()
                    cancelled.append(stop_task)
            watcher = self._detach_watcher_locked(stale)
            if watcher is not None:
                watcher.cancel()
                cancelled.append(watcher)
        return cancelled

    async def _unsubscribe(
        self,
        root: Path,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        cancelled: list[asyncio.Task[None]] = []
        async with self._lock:
            state = self._states.get(root.resolve())
            if state is None:
                return
            # A queue can belong to a removed/replaced state. Its delayed
            # context-manager cleanup must not affect the replacement watcher.
            if queue not in state.subscribers:
                return
            state.subscribers.discard(queue)
            if not state.subscribers:
                cancelled = self._schedule_idle_stop_locked(state)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    async def bootstrap(
        self,
        root: Path,
        *,
        show_hidden: bool = False,
        parents: list[str] | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        state: _WatchState | None = None
        try:
            state = await self.ensure_workspace(root)
            await self._await_baseline(state)
            payload = await asyncio.to_thread(
                self.store.bootstrap,
                state.workspace_id,
                show_hidden=show_hidden,
                parents=parents,
            )
        except asyncio.CancelledError:
            _perf_event(
                "files.bootstrap",
                workspace=(short_id(state.workspace_id) if state else None),
                status="cancelled",
                total_ms=elapsed_ms(started),
            )
            raise
        except Exception as exc:
            _perf_event(
                "files.bootstrap",
                workspace=(short_id(state.workspace_id) if state else None),
                status="error",
                error_type=type(exc).__name__,
                total_ms=elapsed_ms(started),
            )
            raise
        _perf_event(
            "files.bootstrap",
            workspace=short_id(state.workspace_id),
            status="ok",
            total_ms=elapsed_ms(started),
            entries=len(payload.get("entries") or ()),
            partial=bool(payload.get("partial", False)),
        )
        return payload

    async def current_cursor(self, root: Path) -> int:
        state = await self.ensure_workspace(root)
        await self._await_baseline(state)
        return await asyncio.to_thread(
            self.store.current_cursor,
            state.workspace_id,
        )

    async def ready_state(self, root: Path) -> dict[str, Any]:
        """Return one generation-bound cursor for an SSE ready event."""
        state = await self.ensure_workspace(root)
        await self._await_baseline(state)
        cursor = await asyncio.to_thread(
            self.store.current_cursor,
            state.workspace_id,
        )
        return {
            "workspace_id": state.workspace_id,
            "cursor": cursor,
        }

    async def delta(
        self,
        root: Path,
        cursor: int,
        *,
        limit: int = 2000,
    ) -> dict[str, Any]:
        started = monotonic()
        state: _WatchState | None = None
        try:
            state = await self.ensure_workspace(root)
            await self._await_baseline(state)
            payload = await asyncio.to_thread(
                self.store.delta,
                state.workspace_id,
                cursor,
                limit=limit,
            )
        except asyncio.CancelledError:
            _perf_event(
                "files.delta",
                workspace=(short_id(state.workspace_id) if state else None),
                status="cancelled",
                total_ms=elapsed_ms(started),
            )
            raise
        except Exception as exc:
            _perf_event(
                "files.delta",
                workspace=(short_id(state.workspace_id) if state else None),
                status="error",
                error_type=type(exc).__name__,
                total_ms=elapsed_ms(started),
            )
            raise
        total_ms = elapsed_ms(started)
        resync = bool(payload.get("resync"))
        has_more = bool(payload.get("has_more"))
        if is_slow(total_ms) or resync or has_more:
            _perf_event(
                "files.delta",
                workspace=short_id(state.workspace_id),
                status="ok",
                total_ms=total_ms,
                changes=len(payload.get("changes") or ()),
                resync=resync,
                has_more=has_more,
            )
        return payload

    async def _await_baseline(self, state: _WatchState) -> None:
        if not state.initialized:
            await state.ready.wait()
        if state.initialized:
            return
        raise HTTPException(
            status_code=503,
            detail="workspace index is temporarily unavailable",
        )

    async def _wait_for_reconcile_backoff(self, state: _WatchState) -> bool:
        delay = max(0.0, state.reconcile_retry_at - monotonic())
        if delay <= 0:
            return not state.reconcile_cancel.is_set()
        try:
            await asyncio.wait_for(state.reconcile_cancel.wait(), timeout=delay)
        except TimeoutError:
            return True
        return False

    async def _reconcile_after_watch(self, state: _WatchState) -> None:
        """Reconcile downtime after the watcher gets a chance to register."""
        while True:
            state.reconcile_pending = False
            retry_delay = max(0.0, state.reconcile_retry_at - monotonic())
            if retry_delay:
                await asyncio.sleep(retry_delay)
            try:
                if not await self._wait_for_reconcile_backoff(state):
                    return
                if state.task is not None:
                    # Never call a scan a closing reconciliation until the
                    # corresponding native watcher is genuinely armed. Waiting
                    # for the task as well avoids hanging if a generation dies
                    # before reaching that point.
                    armed = await self._wait_for_armed_watcher(state)
                    if not armed and state.task is not None:
                        return
                partial = await self._reconcile_and_broadcast(state)
                if partial:
                    self._record_reconcile_retry(state)
                    state.reconcile_pending = True
                else:
                    self._reset_reconcile_retry(state)
            except asyncio.CancelledError:
                raise
            except WorkspaceScanCancelled:
                return
            except Exception as exc:
                state.reconcile_error = exc
                self._broadcast(
                    state,
                    {"resync": True, "changes": []},
                )
            finally:
                state.ready.set()
                state.reconcile_running = False
            if not state.reconcile_pending:
                return

    async def _wait_for_armed_watcher(self, state: _WatchState) -> bool:
        """Wait for the current watcher generation, following safe restarts."""
        while True:
            async with self._lock:
                watcher = state.task
                if watcher is None:
                    return False
                if state.watch_ready.is_set():
                    return True
                ready_wait = asyncio.create_task(state.watch_ready.wait())
            try:
                done, _ = await asyncio.wait(
                    {ready_wait, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                ready_wait.cancel()
                await asyncio.gather(ready_wait, return_exceptions=True)
                raise
            if ready_wait not in done:
                ready_wait.cancel()
                await asyncio.gather(ready_wait, return_exceptions=True)
            async with self._lock:
                if state.task is not watcher:
                    continue
                if ready_wait in done and not ready_wait.cancelled():
                    return True
                return False

    async def _reconcile_and_broadcast(
        self,
        state: _WatchState,
    ) -> bool:
        started = monotonic()
        metrics: dict[str, int | bool | str | None] = {
            "mutation_lock_wait_ms": 0,
            "scan_ms": 0,
            "replay_ms": 0,
            "scanned_files": 0,
            "snapshot_files": 0,
            "changes": 0,
            "resync": False,
            "partial": False,
            "partial_reason": None,
        }
        status = "error"
        error_type: str | None = None
        backoff_ms = 0
        phase = "initial" if not state.initialized else "closing"
        state.reconcile_attempts += 1
        attempt = state.reconcile_attempts
        partial = False
        try:
            partial = await self._reconcile_and_broadcast_impl(state, metrics)
            if not partial:
                self._reset_reconcile_retry(state)
            status = "partial" if partial else "ok"
        except asyncio.CancelledError:
            status = "cancelled"
            error_type = "CancelledError"
            raise
        except WorkspaceScanCancelled:
            status = "cancelled"
            error_type = "WorkspaceScanCancelled"
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            state.reconcile_error = exc
            self._record_reconcile_retry(state)
            backoff_ms = round(min(
                _RECONCILE_BACKOFF_START_S
                * (2 ** min(state.reconcile_failures - 1, 20)),
                _RECONCILE_BACKOFF_CAP_S,
            ) * 1000)
            raise
        finally:
            # Keep this true through replay, broadcast, and watcher-path
            # refresh. A watcher generation that changes anywhere after the
            # snapshot begins must queue one more armed closing pass.
            state.reconcile_running = False
            _perf_event(
                "files.reconcile",
                workspace=short_id(state.workspace_id),
                status=status,
                error_type=error_type,
                phase=phase,
                attempt=attempt,
                failures=state.reconcile_failures,
                backoff_ms=backoff_ms,
                mutation_lock_wait_ms=metrics["mutation_lock_wait_ms"],
                scan_ms=metrics["scan_ms"],
                replay_ms=metrics["replay_ms"],
                scanned_files=metrics["scanned_files"],
                snapshot_files=metrics["snapshot_files"],
                changes=metrics["changes"],
                resync=metrics["resync"],
                partial=metrics["partial"],
                partial_reason=metrics["partial_reason"],
                total_ms=elapsed_ms(started),
            )
        return partial

    async def _reconcile_and_broadcast_impl(
        self,
        state: _WatchState,
        metrics: dict[str, int | bool | str | None],
    ) -> bool:
        broadcast_payload: dict[str, Any] | None = None
        partial = False
        while True:
            mutation_lock_started = monotonic()
            await state.mutation_lock.acquire()
            metrics["mutation_lock_wait_ms"] = int(
                metrics["mutation_lock_wait_ms"]
            ) + elapsed_ms(mutation_lock_started)
            retry_after_arm = False
            try:
                # A watcher may restart while this workspace waits for an older
                # mutation. Never scan inside the replacement generation's gap.
                if state.task is not None and not state.watch_ready.is_set():
                    retry_after_arm = True
                else:
                    state.reconcile_running = True
                    # Keep the replay window consistent with watcher mutations.
                    # Reading `before` or `delta` outside this lock lets a native
                    # batch commit between them and be broadcast twice.
                    before = await asyncio.to_thread(
                        self.store.current_cursor,
                        state.workspace_id,
                    )
                    scan_report: dict[str, Any] = {}
                    scan_started = monotonic()
                    try:
                        await asyncio.to_thread(
                            self.store.reconcile,
                            state.workspace_id,
                            state.root,
                            state.name,
                            primary=state.primary,
                            cancel_event=state.scan_cancel,
                            report=scan_report,
                            scan_progress=state.scan_progress,
                        )
                    finally:
                        metrics["scan_ms"] = int(
                            metrics["scan_ms"]
                        ) + elapsed_ms(scan_started)
                    partial = bool(scan_report.get("partial"))
                    metrics["partial"] = partial
                    metrics["partial_reason"] = scan_report.get(
                        "partial_reason"
                    )
                    metrics["scanned_files"] = int(
                        scan_report.get("scanned_files") or 0
                    )
                    metrics["snapshot_files"] = int(
                        scan_report.get("snapshot_files") or 0
                    )
                    replay_started = monotonic()
                    try:
                        replay = await asyncio.to_thread(
                            self.store.delta,
                            state.workspace_id,
                            before,
                            limit=5000,
                        )
                        metrics["changes"] = len(
                            replay.get("changes") or ()
                        )
                        metrics["resync"] = bool(
                            replay.get("resync") or replay.get("has_more")
                        )
                        if replay.get("resync") or replay.get("has_more"):
                            cursor = await asyncio.to_thread(
                                self.store.current_cursor,
                                state.workspace_id,
                            )
                            broadcast_payload = {
                                "resync": True,
                                "changes": [],
                                "cursor": cursor,
                            }
                        elif replay.get("changes"):
                            broadcast_payload = replay
                    finally:
                        metrics["replay_ms"] = int(
                            metrics["replay_ms"]
                        ) + elapsed_ms(replay_started)
            finally:
                state.mutation_lock.release()
            if not retry_after_arm:
                break
            if not await self._wait_for_armed_watcher(state):
                raise RuntimeError(
                    "watcher stopped before closing reconciliation"
                )
        state.initialized = True
        state.reconcile_error = None
        if broadcast_payload is not None:
            self._broadcast(state, broadcast_payload)
        if state.task is not None:
            latest_paths = await self._watch_directories(state)
            if latest_paths != state.watch_paths:
                self._request_watch_refresh(state)
                await self._schedule_reconcile(state)
        return partial

    async def _schedule_reconcile(self, state: _WatchState) -> None:
        async with self._lock:
            if self._states.get(state.root) is not state:
                return
            self._queue_reconcile_locked(state)

    @staticmethod
    def _request_watch_refresh(state: _WatchState) -> None:
        state.watch_revision += 1
        state.watch_ready.clear()
        if state.watch_stop_event is not None:
            state.watch_stop_event.set()

    async def _watch_directories(
        self,
        state: _WatchState,
    ) -> tuple[Path, ...]:
        loader = getattr(self.store, "watch_directories", None)
        if loader is None:
            return (state.root,)
        directories = await asyncio.to_thread(
            loader,
            state.workspace_id,
            state.root,
        )
        return directories or (state.root,)

    async def _watch(self, state: _WatchState) -> None:
        while True:
            stop_event: asyncio.Event | None = None
            stream = None
            next_batch: asyncio.Task | None = None
            try:
                revision = state.watch_revision
                directories = await self._watch_directories(state)
                if revision != state.watch_revision:
                    continue
                stop_event = asyncio.Event()
                state.watch_stop_event = stop_event
                if revision != state.watch_revision:
                    stop_event.set()
                    continue
                stream = awatch(
                    *directories,
                    watch_filter=_watch_filter_for(state.root),
                    debounce=_WATCH_DEBOUNCE_MS,
                    step=_WATCH_STEP_MS,
                    stop_event=stop_event,
                    force_polling=state.force_polling,
                    poll_delay_ms=500,
                    recursive=False,
                    ignore_permission_denied=True,
                )
                while True:
                    next_batch = asyncio.create_task(anext(stream))
                    # `awatch` constructs RustNotify before its first blocking
                    # await. Let that task reach the wait point. A task that has
                    # already failed must never make this generation look armed.
                    await asyncio.sleep(0)
                    if next_batch.done():
                        completed_batch = next_batch
                        next_batch = None
                        try:
                            changes = completed_batch.result()
                        except StopAsyncIteration:
                            break
                    else:
                        state.watch_paths = directories
                        state.watch_ready.set()
                        if state.needs_closing_reconcile:
                            state.needs_closing_reconcile = False
                            await self._schedule_reconcile(state)
                        try:
                            changes = await next_batch
                        except StopAsyncIteration:
                            break
                        finally:
                            next_batch = None

                    # An immediately available successful batch also proves the
                    # generator installed/entered this watcher generation.
                    state.watch_paths = directories
                    state.watch_ready.set()
                    if state.needs_closing_reconcile:
                        state.needs_closing_reconcile = False
                        await self._schedule_reconcile(state)
                    rows = _normalise_changes(state.root, changes)
                    if not rows:
                        continue
                    async with state.mutation_lock:
                        payload = await asyncio.to_thread(
                            self.store.apply_changes,
                            state.workspace_id,
                            state.root,
                            rows,
                        )
                    watch_refresh = bool(
                        payload.pop("_watch_refresh", False)
                    )
                    needs_reconcile = bool(
                        payload.pop("_reconcile", False)
                    )
                    if payload.get("resync") or payload["changes"]:
                        self._broadcast(state, payload)
                    if watch_refresh:
                        self._request_watch_refresh(state)
                    if needs_reconcile or watch_refresh:
                        await self._schedule_reconcile(state)
                    if watch_refresh:
                        break
                if revision != state.watch_revision:
                    continue
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Containers may exhaust the native watch budget permanently.
                # Polling keeps the durable contract instead of retrying the
                # same broken registration forever.
                if _is_watch_resource_error(exc):
                    changed_to_polling = state.force_polling is not True
                    state.force_polling = True
                    if changed_to_polling:
                        _perf_event(
                            "files.watcher_mode",
                            workspace=short_id(state.workspace_id),
                            mode="polling",
                            reason="resource_exhaustion",
                            error_type=type(exc).__name__,
                        )
                # There is an uncovered interval between this failed generation
                # and its replacement. Defer the closing reconciliation until
                # the retry is genuinely armed, coalescing with any pass already
                # running for this workspace.
                state.needs_closing_reconcile = True
                state.watch_ready.clear()
                state.watch_paths = ()
                await asyncio.sleep(_WATCH_RETRY_S)
            finally:
                if next_batch is not None and not next_batch.done():
                    next_batch.cancel()
                    await asyncio.gather(
                        next_batch,
                        return_exceptions=True,
                    )
                if stream is not None:
                    with contextlib.suppress(
                        RuntimeError,
                        asyncio.CancelledError,
                    ):
                        await stream.aclose()
                if state.watch_stop_event is stop_event:
                    state.watch_stop_event = None
                    state.watch_ready.clear()
                    state.watch_paths = ()

    @staticmethod
    def _broadcast(
        state: _WatchState,
        payload: dict[str, Any],
    ) -> None:
        overflowed = 0
        for queue in tuple(state.subscribers):
            if queue.full():
                overflowed += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    while True:
                        queue.get_nowait()
                payload_for_queue = {
                    "resync": True,
                    "changes": [],
                    **(
                        {"cursor": payload["cursor"]}
                        if "cursor" in payload
                        else {}
                    ),
                }
            else:
                payload_for_queue = payload
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload_for_queue)
        if overflowed:
            if not state.queue_overflow_active:
                _perf_event(
                    "files.watcher_queue_overflow",
                    workspace=short_id(state.workspace_id),
                    subscribers=len(state.subscribers),
                    overflowed=overflowed,
                )
            state.queue_overflow_active = True
        else:
            state.queue_overflow_active = False

    @staticmethod
    def _close_subscribers(state: _WatchState) -> None:
        for queue in tuple(state.subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    while True:
                        queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"close": True})
        state.subscribers.clear()

    async def shutdown(self) -> None:
        async with self._lock:
            states = list(self._states.values())
            self._states.clear()
            self._idle_watchers.clear()
            self._started = False
            maintenance_task = self._maintenance_task
            self._maintenance_task = None
            reconcile_tasks: list[asyncio.Task[None]] = []
            tasks = [
                task
                for state in states
                for task in (
                    self._detach_watcher_locked(state),
                    state.stop_task,
                )
                if task is not None
            ]
            for state in states:
                state.reconcile_cancel.set()
                state.scan_cancel.set()
                if (state.reconcile_task is not None
                        and not state.reconcile_task.done()):
                    reconcile_tasks.append(state.reconcile_task)
                    if not state.reconcile_running:
                        state.reconcile_task.cancel()
                self._close_subscribers(state)
                state.reconcile_task = None
                state.stop_task = None
        if maintenance_task is not None:
            maintenance_task.cancel()
            await asyncio.gather(maintenance_task, return_exceptions=True)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Do not cancel an outer to_thread await: cancellation would detach the
        # Python worker and let it keep scanning against a store we immediately
        # close. The cooperative event makes ordinary scans unwind promptly;
        # runtime_lifecycle still owns the hard shutdown budget for a blocked
        # kernel/network filesystem call.
        try:
            if reconcile_tasks:
                await asyncio.gather(*reconcile_tasks, return_exceptions=True)
        finally:
            # close() only resets lazy state and uses a short RLock section.
            # Run it synchronously in finally so an outer lifecycle deadline
            # cannot cancel the to_thread wrapper before this invariant lands.
            self.store.close()


manager = FileWatchManager()


@router.get("/bootstrap", dependencies=[Depends(require_token)])
async def workspace_bootstrap(
    show_hidden: bool = False,
    root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    """Return a filtered snapshot plus a cursor for the complete event log."""
    return await manager.bootstrap(root, show_hidden=show_hidden)


@router.post("/bootstrap", dependencies=[Depends(require_token)])
async def workspace_bootstrap_selected(
    body: WorkspaceBootstrapRequest,
    root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    """Return root/selected-parent rows without materializing the full tree."""
    return await manager.bootstrap(
        root,
        show_hidden=body.show_hidden,
        parents=body.parents,
    )


@router.get("/delta", dependencies=[Depends(require_token)])
async def workspace_delta(
    cursor: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=5000),
    root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    """Replay ordered file changes after a bootstrap or earlier delta cursor."""
    return await manager.delta(root, cursor, limit=limit)


@router.post("/events-ticket", dependencies=[Depends(require_token)])
def mint_file_event_ticket(
    root: Path = Depends(resolve_workspace_root),
) -> dict:
    ticket = tickets.mint(
        "files",
        (str(root.resolve()),),
        ttl=_EVENT_TICKET_TTL_S,
        single_use=True,
    )
    return {
        "ticket": ticket,
        "expires_in": _EVENT_TICKET_TTL_S,
    }


def _require_file_event_ticket(
    ticket: str = Query(""),
    root: Path = Depends(resolve_workspace_root),
) -> None:
    if not tickets.validate(
        ticket,
        "files",
        (str(root.resolve()),),
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid or expired file event ticket",
        )


async def _event_stream(
    root: Path,
    cursor: int | None,
) -> AsyncIterator[ServerSentEvent]:
    ready_sent = False
    try:
        async with manager.subscribe(root) as queue:
            # Do not call bootstrap here: on a home-directory workspace that used
            # to materialize and JSON-encode tens of MB for every SSE connection.
            ready_state = await manager.ready_state(root)
            ready_cursor = ready_state["cursor"]
            ready_sent = True
            yield ServerSentEvent(
                event="ready",
                data=json.dumps(
                    {"ready": True, **ready_state},
                    separators=(",", ":"),
                ),
            )

            delivered = ready_cursor if cursor is None else cursor
            if cursor is not None:
                while True:
                    replay = await manager.delta(root, delivered)
                    event = (
                        "resync"
                        if replay.get("resync")
                        else "changes"
                    )
                    if replay.get("resync") or replay["changes"]:
                        yield ServerSentEvent(
                            event=event,
                            data=json.dumps(
                                replay,
                                separators=(",", ":"),
                            ),
                        )
                    delivered = replay["cursor"]
                    if (
                        replay.get("resync")
                        or not replay.get("has_more")
                    ):
                        break

            while True:
                payload = await queue.get()
                if payload.get("close"):
                    return
                if (
                    "cursor" in payload
                    and payload["cursor"] <= delivered
                ):
                    continue
                event = (
                    "resync"
                    if payload.get("resync")
                    else "changes"
                )
                yield ServerSentEvent(
                    event=event,
                    data=json.dumps(
                        payload,
                        separators=(",", ":"),
                    ),
                )
                if "cursor" in payload:
                    delivered = payload["cursor"]
    except HTTPException:
        if not ready_sent:
            yield ServerSentEvent(
                event="unavailable",
                data='{"available":false,"retryable":true}',
            )
        return


@router.get(
    "/events",
    dependencies=[Depends(_require_file_event_ticket)],
)
async def file_events(
    cursor: int | None = Query(default=None, ge=0),
    root: Path = Depends(resolve_workspace_root),
) -> EventSourceResponse:
    """Stream live changes and optionally replay from a persistent cursor."""
    return EventSourceResponse(
        _event_stream(root, cursor),
        ping=20,
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
