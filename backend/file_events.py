"""Persistent workspace file index, replayable deltas, and shared watchers."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from watchfiles import Change, awatch

from .auth import require_token
from .capability_tickets import tickets
from .files import INTERNAL_DIR_NAME, TRASH_DIR_NAME
from .workspace_store import WorkspaceStore, is_ignored_descendant
from .workspaces import registry, resolve_workspace_root


router = APIRouter(prefix="/api/files", tags=["files"])

_QUEUE_LIMIT = 8
_WATCH_DEBOUNCE_MS = 350
_WATCH_STEP_MS = 100
_WATCH_RETRY_S = 1.5
_EVENT_TICKET_TTL_S = 45
_EXCLUDED_DIRS = frozenset({TRASH_DIR_NAME, INTERNAL_DIR_NAME})
_POLLING_ENV = os.getenv("WATCHFILES_FORCE_POLLING")
_FORCE_POLLING: bool | None = (
    None
    if _POLLING_ENV is None
    else _POLLING_ENV.strip().lower() in {"1", "true", "yes", "on"}
)


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
    reconcile_pending: bool = False


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
        self._lock = asyncio.Lock()
        self._started = False

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
        except Exception:
            async with self._lock:
                self._started = False
            raise

    async def ensure_workspace(
        self,
        root: Path,
        *,
        start_watcher: bool | None = None,
        rescan: bool = False,
    ) -> _WatchState:
        root = root.resolve()
        entry = registry.entry_for(root)
        # These are bounded SQLite metadata operations. The expensive recursive
        # scan is always scheduled below and never held under the manager lock.
        await asyncio.to_thread(
            self.store.register_workspace,
            entry.id,
            root,
            entry.name,
            primary=entry.primary,
        )
        status = await asyncio.to_thread(self.store.state, entry.id)

        async with self._lock:
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
                state.name = entry.name
                state.primary = entry.primary
                if status["initialized"]:
                    state.initialized = True
                    state.ready.set()

            # A recursive native watcher can consume thousands of inotify/FSEvent
            # registrations. Keep it only while at least one SSE client needs
            # live updates; the SQLite index remains process-persistent.
            should_watch = bool(start_watcher)
            watch_started = False
            if should_watch and (
                state.task is None
                or state.task.done()
            ):
                state.task = asyncio.create_task(
                    self._watch(state),
                    name=f"muselab-files:{entry.id}",
                )
                watch_started = True

            needs_reconcile = (
                created
                or rescan
                or watch_started
                or not state.initialized
            )
            if needs_reconcile and (
                state.reconcile_task is None
                or state.reconcile_task.done()
            ):
                if not state.initialized:
                    state.ready.clear()
                state.reconcile_task = asyncio.create_task(
                    self._reconcile_after_watch(state),
                    name=f"muselab-files-reconcile:{entry.id}",
                )
            return state

    async def remove_workspace(self, workspace_id: str) -> None:
        tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            for root, state in tuple(self._states.items()):
                if state.workspace_id != workspace_id:
                    continue
                self._states.pop(root, None)
                tasks = [
                    task
                    for task in (state.task, state.reconcile_task)
                    if task is not None
                ]
                state.task = None
                state.reconcile_task = None
                self._close_subscribers(state)
                break
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(
            self.store.remove_workspace,
            workspace_id,
        )

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        root: Path,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        state = await self.ensure_workspace(
            root,
            start_watcher=True,
        )
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_QUEUE_LIMIT,
        )
        async with self._lock:
            state.subscribers.add(queue)
        try:
            yield queue
        finally:
            await self._unsubscribe(root, queue)

    async def _unsubscribe(
        self,
        root: Path,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        task: asyncio.Task[None] | None = None
        async with self._lock:
            state = self._states.get(root.resolve())
            if state is None:
                return
            state.subscribers.discard(queue)
            if not state.subscribers and state.task is not None:
                task = state.task
                state.task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def bootstrap(
        self,
        root: Path,
        *,
        show_hidden: bool = False,
    ) -> dict[str, Any]:
        state = await self.ensure_workspace(root)
        await self._await_baseline(state)
        return await asyncio.to_thread(
            self.store.bootstrap,
            state.workspace_id,
            show_hidden=show_hidden,
        )

    async def current_cursor(self, root: Path) -> int:
        state = await self.ensure_workspace(root)
        await self._await_baseline(state)
        return await asyncio.to_thread(
            self.store.current_cursor,
            state.workspace_id,
        )

    async def delta(
        self,
        root: Path,
        cursor: int,
        *,
        limit: int = 2000,
    ) -> dict[str, Any]:
        state = await self.ensure_workspace(root)
        await self._await_baseline(state)
        return await asyncio.to_thread(
            self.store.delta,
            state.workspace_id,
            cursor,
            limit=limit,
        )

    async def _await_baseline(self, state: _WatchState) -> None:
        if not state.initialized:
            await state.ready.wait()
        if state.initialized:
            return
        detail = "workspace index is temporarily unavailable"
        if state.reconcile_error is not None:
            detail = f"{detail}: {state.reconcile_error}"
        raise HTTPException(status_code=503, detail=detail)

    async def _reconcile_after_watch(self, state: _WatchState) -> None:
        """Reconcile downtime after the watcher gets a chance to register."""
        while True:
            state.reconcile_pending = False
            try:
                if state.task is not None:
                    # `_watch` primes the async generator before setting this
                    # event, so native shallow watches are installed before the
                    # closing reconciliation scan.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            state.watch_ready.wait(),
                            timeout=2.0,
                        )
                await self._reconcile_and_broadcast(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.reconcile_error = exc
                self._broadcast(
                    state,
                    {"resync": True, "changes": []},
                )
            finally:
                state.ready.set()
            if not state.reconcile_pending:
                return

    async def _reconcile_and_broadcast(
        self,
        state: _WatchState,
    ) -> None:
        before = await asyncio.to_thread(
            self.store.current_cursor,
            state.workspace_id,
        )
        async with state.mutation_lock:
            await asyncio.to_thread(
                self.store.reconcile,
                state.workspace_id,
                state.root,
                state.name,
                primary=state.primary,
            )
        state.initialized = True
        state.reconcile_error = None
        if state.task is not None:
            latest_paths = await self._watch_directories(state)
            if latest_paths != state.watch_paths:
                self._request_watch_refresh(state)
                await self._schedule_reconcile(state)
        replay = await asyncio.to_thread(
            self.store.delta,
            state.workspace_id,
            before,
            limit=5000,
        )
        if replay.get("resync") or replay.get("has_more"):
            cursor = await asyncio.to_thread(
                self.store.current_cursor,
                state.workspace_id,
            )
            self._broadcast(
                state,
                {
                    "resync": True,
                    "changes": [],
                    "cursor": cursor,
                },
            )
        elif replay.get("changes"):
            self._broadcast(state, replay)

    async def _schedule_reconcile(self, state: _WatchState) -> None:
        async with self._lock:
            if (
                state.reconcile_task is not None
                and not state.reconcile_task.done()
            ):
                state.reconcile_pending = True
                return
            if not state.initialized:
                state.ready.clear()
            state.reconcile_task = asyncio.create_task(
                self._reconcile_after_watch(state),
                name=f"muselab-files-reconcile:{state.workspace_id}",
            )

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
                    # await. Let that task reach the wait point, then declare
                    # this directory generation armed for reconciliation.
                    await asyncio.sleep(0)
                    state.watch_paths = directories
                    state.watch_ready.set()
                    try:
                        changes = await next_batch
                    except StopAsyncIteration:
                        break
                    finally:
                        next_batch = None
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
                    state.force_polling = True
                try:
                    await self._reconcile_and_broadcast(state)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._broadcast(
                        state,
                        {"resync": True, "changes": []},
                    )
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

    @staticmethod
    def _broadcast(
        state: _WatchState,
        payload: dict[str, Any],
    ) -> None:
        for queue in tuple(state.subscribers):
            if queue.full():
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
            self._started = False
            tasks = [
                task
                for state in states
                for task in (state.task, state.reconcile_task)
                if task is not None
            ]
            for state in states:
                self._close_subscribers(state)
                state.task = None
                state.reconcile_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.store.close)


manager = FileWatchManager()


@router.get("/bootstrap", dependencies=[Depends(require_token)])
async def workspace_bootstrap(
    show_hidden: bool = False,
    root: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    """Return a filtered snapshot plus a cursor for the complete event log."""
    return await manager.bootstrap(root, show_hidden=show_hidden)


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
    async with manager.subscribe(root) as queue:
        # Do not call bootstrap here: on a home-directory workspace that used
        # to materialize and JSON-encode tens of MB for every SSE connection.
        ready_cursor = await manager.current_cursor(root)
        yield ServerSentEvent(
            event="ready",
            data=json.dumps(
                {"ready": True, "cursor": ready_cursor},
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
