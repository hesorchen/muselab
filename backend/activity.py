"""Durable cross-workspace task activity ledger."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import ROOT, atomic_write_text
from . import sessions

_MAX_EVENTS = 500
_TERMINAL = {"completed", "failed", "cancelled"}


class ActivityService:
    """Keep one current activity row per conversation."""

    def __init__(self, root: Path = ROOT):
        self.path = root / ".muselab" / "activity.json"
        self._lock = threading.RLock()
        self._events = self._load()
        changed = self._collapse_sessions()
        for item in self._events:
            if item.get("state") in {"running", "waiting_approval", "paused"}:
                item.update(state="failed", status_detail="Interrupted by service restart",
                            finished_at=time.time(), needs_attention=True, read=False)
                changed = True
        if changed:
            self._save()

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)][-_MAX_EVENTS:]
        except (OSError, ValueError):
            pass
        return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(
            self._events[-_MAX_EVENTS:], ensure_ascii=False, indent=2))

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

    def start(self, sid: str, *, summary: str = "") -> dict[str, Any]:
        now = time.time()
        name, workspace, workspace_name = self._metadata(sid)
        with self._lock:
            item = self._latest(sid)
            if item is None:
                item = {"id": uuid.uuid4().hex, "session_id": sid,
                        "kind": "turn", "turn_count": 0}
                self._events.append(item)
            item.update(
                workspace=workspace, workspace_name=workspace_name,
                session_name=name, state="running",
                task_summary=(summary or item.get("task_summary") or name)[:500],
                status_detail="", started_at=now, finished_at=None,
                needs_attention=False, read=True,
                turn_count=int(item.get("turn_count") or 0) + 1,
            )
            self._events = self._events[-_MAX_EVENTS:]
            self._save()
            return dict(item)

    def set_state(self, sid: str, state: str, *, detail: str = "") -> dict[str, Any]:
        if state not in {"running", "waiting_approval", "paused"}:
            raise ValueError("invalid activity state")
        with self._lock:
            item = self._latest(sid)
        if item is None or item.get("state") in _TERMINAL:
            self.start(sid)
        with self._lock:
            item = self._latest(sid)
            assert item is not None
            item.update(state=state, status_detail=detail[:500],
                        needs_attention=state != "running", read=state == "running")
            self._save()
            return dict(item)

    def finish(self, sid: str, status: str) -> dict[str, Any]:
        state = "completed" if status == "completed" else (
            "cancelled" if status in {"cancelled", "interrupted"} else "failed")
        with self._lock:
            item = self._latest(sid)
        if item is None:
            self.start(sid)
        with self._lock:
            item = self._latest(sid)
            assert item is not None
            item.update(state=state, finished_at=time.time(),
                        needs_attention=state != "cancelled", read=state == "cancelled",
                        status_detail={"completed": "Task completed",
                                       "failed": "Task failed",
                                       "cancelled": "Task cancelled"}[state])
            self._save()
            return dict(item)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(x) for x in reversed(self._events[-min(max(limit, 1), 500):])]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            events = [dict(x) for x in self._events]
        active = {"running", "waiting_approval", "paused"}
        workspaces: dict[str, dict[str, Any]] = {}
        for item in events:
            path = str(item.get("workspace") or "")
            row = workspaces.setdefault(path, {"path": path,
                "name": item.get("workspace_name") or Path(path).name or "Workspace",
                "running": 0, "unread": 0, "attention": 0})
            if item.get("state") in active:
                row["running"] += 1
            if item.get("needs_attention") and not item.get("read"):
                row["unread"] += 1
            if item.get("state") in {"failed", "waiting_approval", "paused"} and not item.get("read"):
                row["attention"] += 1
        return {"running": sum(x.get("state") in active for x in events),
                "unread": sum(bool(x.get("needs_attention")) and not x.get("read") for x in events),
                "attention": sum(x.get("state") in {"failed", "waiting_approval", "paused"}
                                 and not x.get("read") for x in events),
                "workspaces": list(workspaces.values())}

    def ack(self, event_id: str | None = None, *, sid: str | None = None) -> int:
        changed = 0
        with self._lock:
            for item in self._events:
                if event_id is not None and item.get("id") != event_id:
                    continue
                if sid is not None and item.get("session_id") != sid:
                    continue
                if item.get("needs_attention") and not item.get("read"):
                    item["read"] = True
                    changed += 1
            if changed:
                self._save()
        return changed


activity = ActivityService()
