"""On-demand filesystem change events for the active workspace.

One native watcher is shared by every browser subscribed to the same root.
The watcher only exists while at least one SSE client is connected, so an
idle muselab process does not retain recursive OS watches indefinitely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from watchfiles import Change, DefaultFilter, awatch

from .auth import require_token_query
from .files import TRASH_DIR_NAME
from .workspaces import resolve_workspace_root


router = APIRouter(prefix="/api/files", tags=["files"])

_QUEUE_LIMIT = 8
_WATCH_DEBOUNCE_MS = 350
_WATCH_STEP_MS = 100
_IGNORED_DIRS = tuple(DefaultFilter.ignore_dirs) + (
    TRASH_DIR_NAME,
    ".muselab",
)


@dataclass
class _WatchState:
    root: Path
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None


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
            # A symlink/backend quirk must never leak an absolute path outside
            # the registered workspace to the browser.
            continue
        path = relative.as_posix()
        if not path or path == ".":
            continue
        kind = {
            Change.added: "added",
            Change.modified: "modified",
            Change.deleted: "deleted",
        }.get(change)
        if not kind:
            continue
        rows[(kind, path)] = {"type": kind, "path": path}
    return sorted(rows.values(), key=lambda row: (row["path"], row["type"]))


class FileWatchManager:
    """Share one recursive watchfiles task per registered workspace."""

    def __init__(self) -> None:
        self._states: dict[Path, _WatchState] = {}
        self._lock = asyncio.Lock()
        self._filter = DefaultFilter(ignore_dirs=_IGNORED_DIRS)

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        root: Path,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        root = root.resolve()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        async with self._lock:
            state = self._states.get(root)
            if state is None:
                state = _WatchState(root=root)
                self._states[root] = state
            state.subscribers.add(queue)
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    self._watch(state),
                    name=f"muselab-files:{root.name or 'root'}",
                )
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
            state = self._states.get(root)
            if state is None:
                return
            state.subscribers.discard(queue)
            if not state.subscribers:
                self._states.pop(root, None)
                task = state.task
                state.task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _watch(self, state: _WatchState) -> None:
        while True:
            try:
                async for changes in awatch(
                    state.root,
                    watch_filter=self._filter,
                    debounce=_WATCH_DEBOUNCE_MS,
                    step=_WATCH_STEP_MS,
                    recursive=True,
                    ignore_permission_denied=True,
                ):
                    rows = _normalise_changes(state.root, changes)
                    if rows:
                        self._broadcast(state, {"changes": rows})
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A native watcher can fail transiently when a mount is
                # replaced. Tell clients to re-baseline, then retry while the
                # workspace still has subscribers.
                self._broadcast(state, {"resync": True, "changes": []})
                await asyncio.sleep(1.5)

    @staticmethod
    def _broadcast(state: _WatchState, payload: dict[str, Any]) -> None:
        for queue in tuple(state.subscribers):
            if queue.full():
                # Once a slow client misses even one structural event, a full
                # refresh is cheaper and more correct than replaying a partial
                # tail. Collapse the entire backlog into one resync marker.
                with contextlib.suppress(asyncio.QueueEmpty):
                    while True:
                        queue.get_nowait()
                payload_for_queue = {"resync": True, "changes": []}
            else:
                payload_for_queue = payload
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload_for_queue)

    async def shutdown(self) -> None:
        async with self._lock:
            states = list(self._states.values())
            self._states.clear()
            tasks = [state.task for state in states if state.task is not None]
            for state in states:
                state.subscribers.clear()
                state.task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


manager = FileWatchManager()


@router.get("/events", dependencies=[Depends(require_token_query)])
async def file_events(
    root: Path = Depends(resolve_workspace_root),
) -> EventSourceResponse:
    """Stream coalesced relative-path changes for one registered workspace."""

    async def events() -> AsyncIterator[ServerSentEvent]:
        async with manager.subscribe(root) as queue:
            yield ServerSentEvent(
                event="ready",
                data=json.dumps({"ready": True}, separators=(",", ":")),
            )
            while True:
                payload = await queue.get()
                event = "resync" if payload.get("resync") else "changes"
                yield ServerSentEvent(
                    event=event,
                    data=json.dumps(payload, separators=(",", ":")),
                )

    return EventSourceResponse(
        events(),
        ping=20,
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
