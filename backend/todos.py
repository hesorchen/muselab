"""Server-authoritative user to-do board (cross-device sync)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .settings import ROOT, atomic_write_text

_MAX_ITEMS = 100
_MAX_TEXT = 240
_PRIORITIES = {"high", "medium", "low"}


class TodosService:
    """One global to-do list shared across devices and workspaces.

    Mirrors the ``ActivityService`` shape: a single JSON file under
    ``ROOT/.muselab`` guarded by an RLock, atomic writes, and an SSE
    fan-out for live cross-device updates.
    """

    def __init__(
        self,
        root: Path = ROOT,
        *,
        initialize_runtime_state: bool = True,
    ):
        self.path = root / ".muselab" / "todos.json"
        self._lock = threading.RLock()
        self._revision = 0
        self._items: list[dict[str, Any]] = []
        self._subscribers: dict[
            asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop
        ] = {}
        self._initialized = False
        if initialize_runtime_state:
            self.initialize_runtime_state()

    def initialize_runtime_state(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.ensure_private_storage()
            self._load()
            self._initialized = True

    def ensure_private_storage(self) -> None:
        storage_dir = self.path.parent
        if storage_dir.is_symlink():
            raise RuntimeError("todos storage directory must not be a symlink")
        storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage_dir.chmod(0o700)
        if self.path.is_symlink():
            raise RuntimeError("todos state must not be a symlink")
        if self.path.exists():
            self.path.chmod(0o600)

    @staticmethod
    def _normalize_item(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        text = str(item.get("text") or "").strip()
        if not text:
            return None
        priority = item.get("priority")
        if priority not in _PRIORITIES:
            priority = "medium"
        return {
            "id": str(item.get("id") or ""),
            "text": text[:_MAX_TEXT],
            "completed": bool(item.get("completed")),
            "priority": priority,
        }

    def _normalize_items(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for raw in value:
            item = self._normalize_item(raw)
            if item is None or not item["id"] or item["id"] in seen:
                continue
            seen.add(item["id"])
            out.append(item)
        return out[-_MAX_ITEMS:]

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self._revision = int(raw.get("revision") or 0)
        self._items = self._normalize_items(raw.get("items"))

    def _save(self) -> None:
        self.ensure_private_storage()
        atomic_write_text(
            self.path,
            json.dumps({
                "version": 1,
                "revision": self._revision,
                "items": self._items,
            }, ensure_ascii=False, indent=2),
            mode=0o600,
        )

    def get(self) -> dict[str, Any]:
        self.initialize_runtime_state()
        with self._lock:
            return {
                "revision": self._revision,
                "items": [dict(x) for x in self._items],
            }

    def replace(
        self,
        items: list[dict[str, Any]],
        base_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Replace the whole list, guarded by an optimistic revision check.

        Returns ``None`` when ``base_revision`` is stale so the caller can
        re-fetch and reconcile instead of silently clobbering a newer write.
        """
        self.initialize_runtime_state()
        with self._lock:
            if base_revision is not None and base_revision != self._revision:
                return None
            self._items = self._normalize_items(items)
            self._revision += 1
            self._save()
            self._publish_locked()
            return {
                "revision": self._revision,
                "items": [dict(x) for x in self._items],
            }

    def _publish_locked(self) -> None:
        payload: dict[str, Any] = {
            "revision": self._revision,
            "items": [dict(x) for x in self._items],
        }
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue, loop in tuple(self._subscribers.items()):
            try:
                loop.call_soon_threadsafe(self._enqueue_update, queue, payload)
            except RuntimeError:
                stale.append(queue)
        for queue in stale:
            self._subscribers.pop(queue, None)

    @staticmethod
    def _enqueue_update(
        queue: asyncio.Queue[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(payload)

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
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


# Importing the API surface must stay read-only for hermetic test collection.
todos = TodosService(initialize_runtime_state=False)
