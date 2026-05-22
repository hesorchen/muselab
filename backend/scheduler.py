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
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .settings import ROOT, atomic_write_text, is_chinese_locale


def _scheduled_label_prefix() -> str:
    """Locale-aware prefix for scheduler-bound session names. Without this,
    English users saw `[定时] my task` in their tab strip because the prefix
    was hardcoded Chinese."""
    return "[定时] " if is_chinese_locale() else "[Scheduled] "


def _server_tz_offset_minutes() -> int:
    """Server's current UTC offset in minutes (east-positive, matching the
    cost-dashboard convention). Used as the fallback when a task was
    persisted before tz_offset_minutes existed."""
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() / 60) if off else 0

# Lazy import target — set at module load
_STATE_FILE: Path | None = (ROOT / ".muselab" / "scheduler.json") if ROOT else None

_state: dict[str, Any] = {
    "tasks": {},        # task_id -> task
    "history": [],      # list of run entries (capped to 200)
    "unread_count": 0,  # results since user last acked
}

_scheduler_task: asyncio.Task | None = None
# Per-task execution lock. Prevents the same scheduled task from running
# twice concurrently — e.g. user clicks "run now" while the scheduler
# loop also fires it, or a long-running task overlaps its own next tick.
# Two concurrent _execute_task calls against the same task share one
# ClaudeSDKClient (via get_client cache) and the CLI subprocess can only
# handle one in-flight conversation, so without serialisation the two
# replies interleave / drop messages. Lock is dict-resident keyed by
# task id; locks are never deleted (one per task max, negligible memory).
_task_locks: dict[str, asyncio.Lock] = {}


def _task_lock(tid: str) -> asyncio.Lock:
    lock = _task_locks.get(tid)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[tid] = lock
    return lock
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
        atomic_write_text(
            _STATE_FILE,
            json.dumps(_state, ensure_ascii=False, indent=2),
        )
    except Exception as e:
        sys.stderr.write(f"[scheduler] failed to save state: {e}\n")


# ---------- schedule math ----------

def _compute_next_run(schedule: dict, ref_ts: float | None = None) -> float | None:
    """Return the next epoch-time `schedule` fires (or None if invalid /
    in the past for a one-shot schedule).

    The schedule's hour/minute are interpreted in the user's timezone (passed
    as `tz_offset_minutes`, east-positive, browser supplies via
    `-Date.getTimezoneOffset()`). Falls back to the server's current TZ for
    schedules persisted before the field existed — keeps existing Docker/UTC
    users from getting their windows shifted overnight.

    Supported `kind` values:
      daily            — every day at hour:minute (user-local)
      weekly           — schedule["weekdays"] is a list of ints 0..6
                          (0=Mon, 6=Sun), at hour:minute
      monthly          — every month on schedule["day"] (1..31), at
                          hour:minute. Months without that day (Feb 31)
                          fall back to that month's last valid day.
      once             — schedule["year/month/day"] + hour:minute, fires
                          once. Returns None once the date is past, so
                          the scheduler stops trying to fire it.
    """
    kind = schedule.get("kind")
    try:
        h = int(schedule.get("hour", 0))
        m = int(schedule.get("minute", 0))
    except (ValueError, TypeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    # Resolve target TZ. tz_offset_minutes is east-positive minutes (Beijing
    # = +480, NYC = -240); 0 is UTC. When the field is missing on a legacy
    # schedule, mirror the previous behavior (server-local, via
    # _server_tz_offset_minutes()) so existing users don't see fire times
    # silently shift.
    tz_off = schedule.get("tz_offset_minutes")
    if tz_off is None:
        tz_off = _server_tz_offset_minutes()
    try:
        tz_off = int(tz_off)
    except (ValueError, TypeError):
        tz_off = _server_tz_offset_minutes()
    tz = timezone(timedelta(minutes=tz_off))
    base = datetime.fromtimestamp(
        ref_ts if ref_ts is not None else time.time(), tz=tz)

    if kind == "daily":
        target = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
        return target.timestamp()

    if kind == "weekly":
        wds = schedule.get("weekdays") or []
        try:
            wds = sorted({int(w) for w in wds if 0 <= int(w) <= 6})
        except (ValueError, TypeError):
            return None
        if not wds:
            return None
        # Probe today + next 7 days, take the first match.
        for delta in range(0, 8):
            cand = base.replace(hour=h, minute=m, second=0, microsecond=0) \
                       + timedelta(days=delta)
            if cand.weekday() in wds and cand > base:
                return cand.timestamp()
        return None

    if kind == "monthly":
        try:
            day = int(schedule["day"])
        except (KeyError, ValueError, TypeError):
            return None
        if not (1 <= day <= 31):
            return None
        # Try current month, then advance month-by-month until we find a
        # valid date. Cap at 12 iterations (a year) so we never loop on
        # bad input.
        cur = base
        for _ in range(12):
            try:
                cand = cur.replace(day=min(day, _month_max_day(cur.year, cur.month)),
                                    hour=h, minute=m, second=0, microsecond=0)
            except ValueError:
                cand = None
            if cand and cand > base:
                return cand.timestamp()
            # advance one month
            ny = cur.year + (1 if cur.month == 12 else 0)
            nm = 1 if cur.month == 12 else cur.month + 1
            cur = cur.replace(year=ny, month=nm, day=1)
        return None

    if kind == "once":
        try:
            y = int(schedule["year"])
            mo = int(schedule["month"])
            d = int(schedule["day"])
            target = datetime(y, mo, d, h, m, 0, tzinfo=tz)
        except (KeyError, ValueError, TypeError):
            return None
        if target <= base:
            return None
        return target.timestamp()

    return None


def _month_max_day(year: int, month: int) -> int:
    """Last calendar day of a given (year, month). Avoids importing calendar."""
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


# ---------- public CRUD ----------

def list_tasks() -> list[dict]:
    return sorted(
        _state["tasks"].values(),
        key=lambda t: (not t.get("enabled", True), t.get("created_at", 0)),
    )


def get_task(tid: str) -> dict | None:
    return _state["tasks"].get(tid)


def create_task(name: str, prompt: str, schedule: dict,
                 model: str = "") -> dict:
    """Create a task with the given schedule dict. The dict shape
    depends on schedule.kind — see _compute_next_run for valid forms.
    Auto-creates a dedicated muselab session bound to the task so every
    run appends to the same history. Session name is `[定时] <task name>`."""
    # Validate the schedule actually resolves to a future fire time —
    # the API surface (api_scheduler.TaskIn) already validates field
    # ranges; this catches "once-in-the-past" / "weekly with empty
    # weekdays" gracefully.
    next_run = _compute_next_run(schedule)
    if next_run is None and schedule.get("kind") != "once":
        # Allow `once` with no next_run only when explicitly so (past
        # date) — but the API layer should reject those upfront.
        raise ValueError(f"schedule does not produce a next fire time: {schedule}")
    # Lazy import to avoid backend.sessions ↔ backend.scheduler cycle
    from . import sessions as sess
    sess_meta = sess.create_session(
        name=f"{_scheduled_label_prefix()}{name}", model=model)
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
        "next_run": next_run,
        "created_at": time.time(),
    }
    _state["tasks"][tid] = task
    _save_state()
    return task


def update_task(tid: str, **changes: Any) -> dict | None:
    t = _state["tasks"].get(tid)
    if not t:
        return None
    # Capture the old name BEFORE applying the change — used to detect a
    # rename so we can keep the bound session's name in sync. Without
    # this the history picker kept showing the old `[定时] xxx` label
    # while the scheduler list showed the new task name.
    old_name = t.get("name")
    for k in ("name", "prompt", "model"):
        if k in changes and changes[k] is not None:
            t[k] = str(changes[k])
    if "enabled" in changes and changes["enabled"] is not None:
        t["enabled"] = bool(changes["enabled"])
    if "schedule" in changes and changes["schedule"] is not None:
        t["schedule"] = changes["schedule"]
        t["next_run"] = _compute_next_run(t["schedule"])
    # Sync bound session name if the task was renamed. Best-effort —
    # failure to find the session shouldn't roll back the task update.
    new_name = t.get("name")
    sid = t.get("session_id")
    if sid and new_name and new_name != old_name:
        try:
            from . import sessions as sess
            sess.rename_session(
                sid, f"{_scheduled_label_prefix()}{new_name}")
        except Exception as e:
            sys.stderr.write(
                f"[scheduler] update_task({tid}): bound session {sid} "
                f"rename failed: {e}\n")
    _save_state()
    return t


def delete_task(tid: str) -> bool:
    """Delete a task AND its bound session by default. Leaving the
    session behind used to create orphan `[定时] xxx` entries in the
    history picker that the user couldn't easily tell apart from active
    ones — so we now clean both. The session's on-disk JSONL is removed
    too (sess.delete_session handles it). Returns True if the task
    existed and got removed."""
    t = _state["tasks"].pop(tid, None)
    if not t:
        return False
    sid = t.get("session_id")
    if sid:
        try:
            from . import sessions as sess
            sess.delete_session(sid)
        except Exception as e:
            sys.stderr.write(
                f"[scheduler] delete_task({tid}): bound session {sid} "
                f"cleanup failed: {e}\n")
    _save_state()
    return True


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

async def run_task_now(tid: str) -> bool:
    """Fire-and-forget out-of-schedule run. Returns True if the task exists
    and got scheduled; False if not found. Does NOT advance next_run — this
    is a one-off, the regular schedule keeps ticking.

    Useful as a "retry" affordance after a failure, and as a smoke test
    after editing a task without having to wait for the next fire window."""
    task = _state["tasks"].get(tid)
    if not task:
        return False
    asyncio.create_task(_execute_task(task))
    return True


async def _execute_task(task: dict) -> None:
    """One full run: send the prompt against the bound session, collect
    the assistant reply, store a history entry. Robust to ANY error in
    the SDK or model — failures are logged into history with the error
    string so the user sees them in the bell drawer.

    Serialised per-task: a second concurrent run on the same task id
    waits for the first to finish. This guards against the "run now"
    button overlapping the scheduler tick on the same task — both share
    one ClaudeSDKClient + CLI subprocess and concurrent receive_response
    calls would interleave / drop messages."""
    from .chat import get_client  # local import — avoids startup cycle

    tid = task["id"]
    sid = task["session_id"]
    reply_text = ""
    error: str | None = None

    async with _task_lock(tid):
        try:
            # Tasks created before the scheduler UI had a model picker stored
            # model=""; SDK then silently fell back to its built-in default
            # (which differs from whatever the user has selected in the chat
            # UI), so the bound session's reply style + capability didn't
            # match what the user expected. Fall back to muselab's MODEL
            # default when task.model is empty.
            from .settings import MODEL as _DEFAULT_MODEL
            model = task.get("model") or _DEFAULT_MODEL
            client = await get_client(
                session_id=sid,
                model=model,
                permission="bypassPermissions",
            )
            # SDK 0.2.x AssistantMessage.content is a list of dataclass
            # blocks (TextBlock / ToolUseBlock / ThinkingBlock / …), NOT
            # plain dicts — so `block.type` doesn't exist as a string. The
            # original implementation was checking that string and silently
            # never matching, which left reply_text empty and made every
            # push notification say "(no reply)". isinstance check is what
            # chat.py uses too — mirror it here.
            from claude_agent_sdk import TextBlock
            await client.query(task["prompt"])
            async for msg in client.receive_response():
                tname = type(msg).__name__
                if tname == "AssistantMessage":
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, TextBlock):
                            reply_text += getattr(block, "text", "") or ""
                elif tname == "ResultMessage":
                    break
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[scheduler] task {tid} ({task['name']}) failed: {error}\n")
        finally:
            # Don't touch next_run here — the scheduler_loop already advanced
            # it before firing, and run_task_now() is explicitly an out-of-band
            # run that mustn't disturb the regular cadence.
            now = time.time()
            task["last_run"] = now
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
            # Fire Web Push to every subscribed device. Gated by the
            # `notify_scheduled` setting so users can mute scheduled-task pushes
            # independently from normal turn-done pushes (see api_settings.py).
            # Errors swallowed — push is best-effort, must never break the loop.
            if os.environ.get("MUSELAB_NOTIFY_SCHEDULED", "true").lower() != "false":
                try:
                    from . import push as _push
                    # Prefix with ⏰ so the notification banner is universally
                    # recognizable as muselab scheduler output across both zh
                    # and en users. (Pushes are server-side rendered; we don't
                    # know the user's lang preference, so language-neutral
                    # icon beats either zh or en prose.)
                    title = f"⏰ {task['name']}"
                    if error:
                        body = f"❌ {error[:120]}"
                    else:
                        body = (preview[:120] + "…") if len(preview) > 120 else preview
                    _push.send_to_all(title=title, body=body or "—",
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
    fires any task whose previous window was missed while muselab was
    down (one catch-up run per task — not N for multi-day outages, to
    avoid burning N× the tokens on the same prompt), then recomputes
    next_run and starts the tick loop.

    User-visible behavior: if you scheduled a "daily 09:00" and muselab
    was offline at 09:00, restarting at 09:30 fires the task once
    immediately and schedules the next one for tomorrow 09:00 — instead
    of silently skipping today as the old code did."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _load_state()
    now = time.time()
    missed: list[dict] = []
    for task in _state["tasks"].values():
        sched = task.get("schedule")
        if not sched:
            continue
        nr = task.get("next_run")
        # If next_run is in the past AND the task is enabled, the window was
        # missed while we were down. Disabled tasks just get next_run rolled
        # forward (no catch-up), matching their "don't fire" intent.
        if nr and nr <= now and task.get("enabled", True):
            missed.append(task)
        task["next_run"] = _compute_next_run(sched)
    _save_state()
    # Kick off catch-up runs concurrently — same path as scheduler_loop uses.
    for task in missed:
        sys.stderr.write(
            f"[scheduler] catching up missed window for task "
            f"{task['id']} ({task.get('name','?')})\n")
        asyncio.create_task(_execute_task(task))
    _scheduler_task = asyncio.create_task(_scheduler_loop())
