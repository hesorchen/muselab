"""Scheduled prompt tasks — daemonized inside muselab's asyncio loop.

Each task: a fixed prompt that fires on a daily schedule, dispatches
against the same muselab session every time (so history accumulates),
and the user gets a "X tasks ran" bell badge in the top bar.

Persistence: archive/.muselab/scheduler.json — same shape as muselab's
other sidecar metadata. Survives muselab restart; next_run is
recomputed on startup in case the process was down through a fire
window.

Wire-up: main.py's startup hook awaits start_scheduler(); CRUD
endpoints in backend/api_scheduler.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .settings import ROOT

# Lazy import target — set at module load
_STATE_FILE: Path | None = (ROOT / ".muselab" / "scheduler.json") if ROOT else None

_state: dict[str, Any] = {
    "tasks": {},        # task_id -> task
    "history": [],      # list of run entries (capped to 200)
    "unread_count": 0,  # results since user last acked
}

_scheduler_task: asyncio.Task | None = None
_HISTORY_CAP = 200
_PREVIEW_CAP_CHARS = 240


def _load_state() -> None:
    global _state
    if not _STATE_FILE or not _STATE_FILE.exists():
        return
    try:
        loaded = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            _state = {
                "tasks": loaded.get("tasks", {}),
                "history": loaded.get("history", []),
                "unread_count": loaded.get("unread_count", 0),
            }
    except Exception as e:
        sys.stderr.write(f"[scheduler] failed to load state: {e}\n")


def _save_state() -> None:
    if not _STATE_FILE:
        return
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        sys.stderr.write(f"[scheduler] failed to save state: {e}\n")


# ---------- schedule math ----------

def _compute_next_run(schedule: dict, ref_ts: float | None = None) -> float | None:
    """Return the next epoch-time `schedule` fires (or None if invalid).
    Only `daily` is supported in this iteration; extend here for weekly /
    monthly / once."""
    if schedule.get("kind") != "daily":
        return None
    try:
        h = int(schedule["hour"])
        m = int(schedule["minute"])
    except (KeyError, ValueError, TypeError):
        return None
    base = datetime.fromtimestamp(ref_ts if ref_ts is not None else time.time())
    target = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= base:
        target += timedelta(days=1)
    return target.timestamp()


# ---------- public CRUD ----------

def list_tasks() -> list[dict]:
    return sorted(
        _state["tasks"].values(),
        key=lambda t: (not t.get("enabled", True), t.get("created_at", 0)),
    )


def get_task(tid: str) -> dict | None:
    return _state["tasks"].get(tid)


def create_task(name: str, prompt: str, hour: int, minute: int,
                 model: str = "") -> dict:
    """Create a daily task. Auto-creates a dedicated muselab session
    bound to the task so every run appends to the same history. Session
    name is `[定时] <task name>` so it's easy to find in the picker."""
    # Lazy import to avoid backend.sessions ↔ backend.scheduler cycle
    from . import sessions as sess
    sess_meta = sess.create_session(name=f"[定时] {name}", model=model)
    schedule = {"kind": "daily", "hour": int(hour), "minute": int(minute)}
    tid = str(uuid.uuid4())
    task = {
        "id": tid,
        "name": name,
        "prompt": prompt,
        "model": model,
        "session_id": sess_meta["id"],
        "schedule": schedule,
        "enabled": True,
        "last_run": None,
        "next_run": _compute_next_run(schedule),
        "created_at": time.time(),
    }
    _state["tasks"][tid] = task
    _save_state()
    return task


def update_task(tid: str, **changes: Any) -> dict | None:
    t = _state["tasks"].get(tid)
    if not t:
        return None
    for k in ("name", "prompt", "model"):
        if k in changes and changes[k] is not None:
            t[k] = str(changes[k])
    if "enabled" in changes and changes["enabled"] is not None:
        t["enabled"] = bool(changes["enabled"])
    if changes.get("hour") is not None or changes.get("minute") is not None:
        sched = dict(t["schedule"])
        if changes.get("hour") is not None:
            sched["hour"] = int(changes["hour"])
        if changes.get("minute") is not None:
            sched["minute"] = int(changes["minute"])
        t["schedule"] = sched
        t["next_run"] = _compute_next_run(sched)
    _save_state()
    return t


def delete_task(tid: str) -> bool:
    if tid in _state["tasks"]:
        del _state["tasks"][tid]
        _save_state()
        return True
    return False


def list_history(limit: int = 50) -> list[dict]:
    """Most-recent first, capped at `limit`."""
    h = _state.get("history", [])
    return h[-limit:][::-1]


def get_unread() -> int:
    return _state.get("unread_count", 0)


def ack_unread() -> int:
    _state["unread_count"] = 0
    _save_state()
    return 0


# ---------- task execution ----------

async def _execute_task(task: dict) -> None:
    """One full run: send the prompt against the bound session, collect
    the assistant reply, store a history entry. Robust to ANY error in
    the SDK or model — failures are logged into history with the error
    string so the user sees them in the bell drawer."""
    from .chat import get_client  # local import — avoids startup cycle

    tid = task["id"]
    sid = task["session_id"]
    reply_text = ""
    error: str | None = None

    try:
        client = await get_client(
            session_id=sid,
            model=task.get("model") or "",
            permission="bypassPermissions",
            show_thinking=False,
        )
        await client.query(task["prompt"])
        async for msg in client.receive_response():
            tname = type(msg).__name__
            if tname == "AssistantMessage":
                # SDK gives a list of TextBlock / ToolUseBlock etc.
                for block in getattr(msg, "content", []) or []:
                    bt = getattr(block, "type", None) or \
                         (block.get("type") if isinstance(block, dict) else None)
                    if bt == "text":
                        txt = getattr(block, "text", None) or \
                              (block.get("text", "") if isinstance(block, dict) else "")
                        reply_text += txt
            elif tname == "ResultMessage":
                # End of this turn — receive_response() will return None next
                break
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[scheduler] task {tid} ({task['name']}) failed: {error}\n")
    finally:
        now = time.time()
        task["last_run"] = now
        task["next_run"] = _compute_next_run(task["schedule"])
        preview = reply_text.strip()
        if len(preview) > _PREVIEW_CAP_CHARS:
            preview = preview[:_PREVIEW_CAP_CHARS] + "…"
        entry = {
            "task_id": tid,
            "task_name": task["name"],
            "session_id": sid,
            "ts": now,
            "ok": error is None,
            "error": error,
            "reply_preview": preview if error is None else None,
        }
        _state["history"].append(entry)
        # Successful runs bump unread; errors also bump so the user
        # notices them — but they show as red in the UI.
        _state["unread_count"] = _state.get("unread_count", 0) + 1
        if len(_state["history"]) > _HISTORY_CAP:
            _state["history"] = _state["history"][-_HISTORY_CAP:]
        _save_state()
        # Fire Web Push to every subscribed device. Errors swallowed —
        # push is best-effort, must never break the scheduler loop.
        try:
            from . import push as _push
            title = task["name"]
            if error:
                body = f"Failed: {error[:120]}"
            else:
                body = (preview[:120] + "…") if len(preview) > 120 else preview
            _push.send_to_all(title=title, body=body or "(no reply)",
                              url="/", tag=f"task-{tid}")
        except Exception as e:
            sys.stderr.write(f"[scheduler] push notify failed for {tid}: {e}\n")


# ---------- daemon loop ----------

async def _scheduler_loop() -> None:
    """Tick every 60 seconds. Any enabled task whose next_run is in the
    past gets fired (concurrently via asyncio.create_task so a slow one
    doesn't hold up the others)."""
    sys.stderr.write("[scheduler] loop started\n")
    while True:
        try:
            now = time.time()
            for task in list(_state["tasks"].values()):
                if not task.get("enabled", True):
                    continue
                nr = task.get("next_run")
                if nr and nr <= now:
                    # Advance next_run optimistically so a long-running
                    # task doesn't fire twice if we tick again before it
                    # finishes.
                    task["next_run"] = _compute_next_run(task["schedule"])
                    _save_state()
                    asyncio.create_task(_execute_task(task))
        except Exception as e:
            sys.stderr.write(f"[scheduler] loop error: {e}\n")
        await asyncio.sleep(60)


async def start_scheduler() -> None:
    """Idempotent — main.py startup awaits this. Loads persisted state,
    recomputes next_run on every task (in case muselab was down past
    a fire window), and starts the tick loop."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _load_state()
    for task in _state["tasks"].values():
        if task.get("schedule"):
            task["next_run"] = _compute_next_run(task["schedule"])
    _save_state()
    _scheduler_task = asyncio.create_task(_scheduler_loop())
