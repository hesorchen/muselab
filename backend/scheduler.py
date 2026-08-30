"""Scheduled prompt tasks — daemonized inside muselab's asyncio loop.

Each task: a fixed prompt that fires on a daily schedule, dispatches
against the same muselab session every time (so history accumulates),
and the user gets a "X tasks ran" bell badge in the top bar.

Persistence: workspace/.muselab/scheduler.json — same shape as muselab's
other sidecar metadata. Survives muselab restart; next_run is
recomputed on startup in case the process was down through a fire
window.

Wire-up: main.py's startup hook awaits start_scheduler(); CRUD
endpoints in backend/api_scheduler.py.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from .settings import ROOT, atomic_write_text, env_int, is_chinese_locale
from . import observability as obs


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


def _resolve_tz(schedule: dict) -> Any:
    """Resolve a schedule's timezone to a tzinfo.

    Priority:
      1. schedule["tz"] — IANA name (e.g. "America/New_York"), supplied by
         the browser via `Intl.DateTimeFormat().resolvedOptions().timeZone`.
         Resolved with ZoneInfo so the fire time tracks DST: a task set for
         09:00 local keeps firing at 09:00 wall-clock across spring-forward /
         fall-back, instead of drifting by an hour.
      2. schedule["tz_offset_minutes"] — fixed UTC offset (east-positive).
         Legacy field for tasks created before `tz` existed; NOT DST-aware,
         but preserves the exact pre-upgrade behavior so nobody's windows
         shift on the day they upgrade.
      3. server-local TZ — last resort when neither field is usable.
    """
    name = schedule.get("tz")
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
            sys.stderr.write(
                f"[scheduler] unknown IANA tz {name!r}; "
                f"falling back to tz_offset_minutes\n")
    # Fixed-offset fallback (east-positive minutes; Beijing=+480, NYC=-240).
    tz_off = schedule.get("tz_offset_minutes")
    if tz_off is None:
        tz_off = _server_tz_offset_minutes()
    try:
        tz_off = int(tz_off)
    except (ValueError, TypeError):
        tz_off = _server_tz_offset_minutes()
    # API pydantic limits to [-1440, 1440] but a hand-edited scheduler.json
    # can persist anything. Real-world TZ range is [-720 (UTC-12),
    # +840 (UTC+14)]; values past that produce OverflowError in
    # fromtimestamp on some platforms and crash the whole tick. Clamp + log.
    if not (-720 <= tz_off <= 840):
        sys.stderr.write(
            f"[scheduler] tz_offset_minutes={tz_off} out of range "
            f"[-720, 840]; using server-local instead\n")
        tz_off = _server_tz_offset_minutes()
    return timezone(timedelta(minutes=tz_off))

# Lazy import target — set at module load
_STATE_FILE: Path | None = (ROOT / ".muselab" / "scheduler.json") if ROOT else None

_state: dict[str, Any] = {
    "tasks": {},        # task_id -> task
    "history": [],      # list of run entries (capped to 200)
    "unread_count": 0,  # results since user last acked
    # task_id -> immutable deletion cleanup intent.  The task and intent are
    # committed in one scheduler.json replacement; external runtime/session
    # cleanup removes the intent only after every owner reaches terminal state.
    "cleanup_pending": {},
}


class SchedulerPersistenceError(RuntimeError):
    """The durable scheduler state is unavailable or could not be saved."""


# A corrupt/unreadable state file is never equivalent to an empty scheduler.
# Once this fence is raised, all public reads and mutations fail explicitly so
# an unrelated later write cannot replace the user's original file.
_STATE_ERROR = ""

_scheduler_task: asyncio.Task | None = None
# Strong references to fire-and-forget execution tasks (tick-loop fires,
# run-now clicks, startup catch-up). asyncio holds only a weak reference to
# a task, so a bare `create_task(...)` whose handle goes out of scope can be
# garbage-collected mid-run, silently cancelling a scheduled run. Each task
# is added here and removed by its done-callback so the set stays bounded.
_RUN_TASKS: set[asyncio.Task] = set()
# Runtime ownership for each tracked execution.  DELETE needs more than the
# strong-reference set above: it must be able to revoke an already-running
# task by scheduler id or by the session being removed, then join that exact
# owner before chat deletes the transcript.  These maps are event-loop owned;
# the durable revocation fence lives under _STATE_LOCK because delete_task()
# also has synchronous callers.
_RUN_TASK_IDS: dict[asyncio.Task, str] = {}
_RUN_SESSION_IDS: dict[asyncio.Task, str] = {}
_RUN_ACTIVITY_STARTED: set[asyncio.Task] = set()
_REVOKED_TASK_IDS: set[str] = set()
_RUN_REGISTRY_LOCK = threading.Lock()


def _forget_tracked_task(t: asyncio.Task) -> None:
    with _RUN_REGISTRY_LOCK:
        _RUN_TASKS.discard(t)
        _RUN_TASK_IDS.pop(t, None)
        _RUN_SESSION_IDS.pop(t, None)
        _RUN_ACTIVITY_STARTED.discard(t)


def _track_task(
    t: asyncio.Task,
    *,
    task_id: str = "",
    session_id: str = "",
) -> asyncio.Task:
    """Hold a strong ref to a fire-and-forget task until it completes."""
    with _RUN_REGISTRY_LOCK:
        _RUN_TASKS.add(t)
        if task_id:
            _RUN_TASK_IDS[t] = task_id
        if session_id:
            _RUN_SESSION_IDS[t] = session_id
    t.add_done_callback(_forget_tracked_task)
    return t


def _bind_current_run_session(task_id: str, session_id: str) -> None:
    """Attach the resolved session to the current tracked scheduler run."""
    current = asyncio.current_task()
    with _RUN_REGISTRY_LOCK:
        if current in _RUN_TASKS and _RUN_TASK_IDS.get(current) == task_id:
            _RUN_SESSION_IDS[current] = session_id


def _mark_current_run_activity_started() -> None:
    current = asyncio.current_task()
    with _RUN_REGISTRY_LOCK:
        if current in _RUN_TASKS:
            _RUN_ACTIVITY_STARTED.add(current)


def _task_is_revoked(task_id: str) -> bool:
    with _STATE_LOCK:
        return task_id in _REVOKED_TASK_IDS


def _cancel_runs(
    *, task_id: str = "", session_id: str = ""
) -> tuple[list[asyncio.Task], bool, set[str]]:
    """Mark matching tracked runs cancelled and return stable join handles.

    A session deletion first installs the sessions tombstone, so a run that
    has not bound its session yet will fail its later storage gate.  A task
    deletion installs _REVOKED_TASK_IDS synchronously in delete_task(), which
    also fences a delayed/catch-up run before this coroutine gets CPU.
    """
    current = asyncio.current_task()
    with _RUN_REGISTRY_LOCK:
        matches = [
            task for task in tuple(_RUN_TASKS)
            if task is not current and not task.done()
            and (not task_id or _RUN_TASK_IDS.get(task) == task_id)
            and (not session_id or _RUN_SESSION_IDS.get(task) == session_id)
        ]
        had_activity = any(task in _RUN_ACTIVITY_STARTED for task in matches)
        session_ids = {
            sid for task in matches
            if (sid := _RUN_SESSION_IDS.get(task))
        }
        for task in matches:
            task.cancel()
    return matches, had_activity, session_ids


async def join_cancelled_runs(
    tasks: list[asyncio.Task],
    *,
    timeout: float = 5.0,
) -> bool:
    """Boundedly join a stable snapshot returned by a cancel helper.

    Revocation/tombstone fences make any late owner unable to commit state.
    Returning False lets destructive API cleanup continue rather than waiting
    forever on a cancellation-resistant SDK coroutine.
    """
    if not tasks:
        return True
    done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
    for task in pending:
        task.cancel()
    return not pending


async def _cancel_and_join_runs(
    *, task_id: str = "", session_id: str = ""
) -> tuple[int, bool]:
    matches, had_activity, _ = _cancel_runs(
        task_id=task_id, session_id=session_id)
    if matches:
        await join_cancelled_runs(matches)
    return len(matches), had_activity


def cancel_runs_for_task_now(
    task_id: str,
) -> tuple[list[asyncio.Task], bool, set[str]]:
    return _cancel_runs(task_id=task_id)


def cancel_runs_for_session_now(
    session_id: str,
) -> tuple[list[asyncio.Task], bool, set[str]]:
    return _cancel_runs(session_id=session_id)


async def cancel_runs_for_task(task_id: str) -> tuple[int, bool]:
    """Cancel and join every in-flight incarnation of a deleted task."""
    return await _cancel_and_join_runs(task_id=task_id)


async def cancel_runs_for_session(session_id: str) -> tuple[int, bool]:
    """Cancel and join scheduled work that owns a deleting session."""
    return await _cancel_and_join_runs(session_id=session_id)
# Serializes every read/write of the module-global _state. The scheduler
# loop + _execute_task run on the event-loop thread, but the CRUD endpoints
# in api_scheduler.py are plain `def` handlers → FastAPI runs them in its
# threadpool. So `ack_unread()` (=0) can race `_execute_task`'s
# `unread_count += 1`, and a create/delete that restructures `_state["tasks"]`
# can race `_save_state()`'s `json.dumps(_state)` → "dictionary changed size
# during iteration". This coarse lock guards both the compound mutations and
# the serialization snapshot. RULE: never call _save_state() (or iterate a
# _state collection) without holding it; _save_state itself stays lock-free so
# lock-holders don't deadlock (threading.Lock is non-reentrant).
_STATE_LOCK = threading.Lock()
# Per-task execution lock. Prevents the same scheduled task from running
# twice concurrently — e.g. user clicks "run now" while the scheduler
# loop also fires it, or a long-running task overlaps its own next tick.
# Two concurrent _execute_task calls against the same task share one
# ClaudeSDKClient (via get_client cache) and the CLI subprocess can only
# handle one in-flight conversation, so without serialisation the two
# replies interleave / drop messages. Lock is dict-resident keyed by
# task id; locks are never deleted (one per task max, negligible memory).
_task_locks: dict[str, asyncio.Lock] = {}
_cleanup_locks: dict[str, asyncio.Lock] = {}


def _task_lock(tid: str) -> asyncio.Lock:
    lock = _task_locks.get(tid)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[tid] = lock
    return lock


def _cleanup_lock(tid: str) -> asyncio.Lock:
    """Serialize idempotent cleanup retries for one deleted task."""
    lock = _cleanup_locks.get(tid)
    if lock is None:
        lock = asyncio.Lock()
        _cleanup_locks[tid] = lock
    return lock

_HISTORY_CAP = 200
_PREVIEW_CAP_CHARS = 240
_CLEANUP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")


def _valid_cleanup_id(value: Any, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return bool(_CLEANUP_ID_RE.fullmatch(value))


def _normalize_cleanup_intents(raw: Any, tasks: dict[str, Any]) -> dict[str, dict]:
    """Validate cleanup records before any startup recovery can act on them.

    scheduler.json is user-editable and may also be truncated/corrupted. A
    malformed deletion record must fence the scheduler instead of turning an
    arbitrary string into a session filesystem target.
    """
    if not isinstance(raw, dict):
        raise ValueError("scheduler cleanup_pending must be an object")
    normalized: dict[str, dict] = {}
    for tid, intent in raw.items():
        if not _valid_cleanup_id(tid):
            raise ValueError("scheduler cleanup task id is invalid")
        if tid in tasks:
            raise ValueError("scheduler task cannot also be pending cleanup")
        if not isinstance(intent, dict) or intent.get("task_id") != tid:
            raise ValueError("scheduler cleanup intent has an invalid task id")
        cleanup_id = intent.get("cleanup_id")
        if not _valid_cleanup_id(cleanup_id):
            raise ValueError("scheduler cleanup intent id is invalid")
        mode = intent.get("session_mode")
        if mode not in ("reuse", "fresh"):
            raise ValueError("scheduler cleanup session mode is invalid")
        sid = intent.get("session_id", "")
        if not _valid_cleanup_id(sid, allow_empty=True):
            raise ValueError("scheduler cleanup session id is invalid")
        runtime_sids = intent.get("runtime_session_ids", [])
        if not isinstance(runtime_sids, list) or len(runtime_sids) > 64:
            raise ValueError("scheduler cleanup runtime sessions are invalid")
        if any(not _valid_cleanup_id(item) for item in runtime_sids):
            raise ValueError("scheduler cleanup runtime session id is invalid")
        created_at = intent.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(created_at)
            or created_at < 0
        ):
            raise ValueError("scheduler cleanup created_at is invalid")
        normalized[tid] = {
            "task_id": tid,
            "cleanup_id": cleanup_id,
            "session_mode": mode,
            "session_id": sid,
            "runtime_session_ids": sorted(set(runtime_sids)),
            "created_at": float(created_at),
        }
    return normalized


def _load_state() -> None:
    global _state, _STATE_ERROR
    if not _STATE_FILE or not _STATE_FILE.exists():
        _STATE_ERROR = ""
        return
    try:
        loaded = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("scheduler state root must be an object")
        tasks = loaded.get("tasks", {})
        history = loaded.get("history", [])
        unread = loaded.get("unread_count", 0)
        if not isinstance(tasks, dict) or not isinstance(history, list):
            raise ValueError("scheduler tasks/history have invalid types")
        if isinstance(unread, bool) or not isinstance(unread, int) or unread < 0:
            raise ValueError("scheduler unread_count must be a non-negative integer")
        cleanup_pending = _normalize_cleanup_intents(
            loaded.get("cleanup_pending", {}), tasks
        )
        _state = {
            "tasks": tasks,
            "history": history,
            "unread_count": unread,
            "cleanup_pending": cleanup_pending,
        }
        _REVOKED_TASK_IDS.clear()
        _REVOKED_TASK_IDS.update(cleanup_pending)
        _STATE_ERROR = ""
    except Exception as e:
        _STATE_ERROR = "scheduler state could not be loaded; original file preserved"
        sys.stderr.write(
            f"[scheduler] failed to load state ({type(e).__name__}); "
            "writes are disabled and the original file was preserved\n"
        )
        raise SchedulerPersistenceError(_STATE_ERROR) from e


def _save_state() -> None:
    global _STATE_ERROR
    if _STATE_ERROR:
        raise SchedulerPersistenceError(_STATE_ERROR)
    if not _STATE_FILE:
        return
    try:
        atomic_write_text(
            _STATE_FILE,
            json.dumps(_state, ensure_ascii=False, indent=2),
            mode=0o600,
        )
    except Exception as e:
        _STATE_ERROR = "scheduler state could not be saved; change was not committed"
        sys.stderr.write(
            f"[scheduler] failed to save state ({type(e).__name__}); "
            "scheduler entered read-only degraded mode\n"
        )
        raise SchedulerPersistenceError(_STATE_ERROR) from e


def ensure_available() -> None:
    """Fail clearly instead of exposing an empty or unsaved scheduler view."""
    if _STATE_ERROR:
        raise SchedulerPersistenceError(_STATE_ERROR)


def persistence_status() -> dict[str, Any]:
    return {"available": not bool(_STATE_ERROR), "error": _STATE_ERROR}


def _restore_state(snapshot: dict[str, Any]) -> None:
    global _state
    _state = snapshot


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
      daily            — every day at hour:minute (user-local). If
                          schedule["times"] is a non-empty list of
                          {hour, minute} dicts, fires at EACH of those
                          times per day instead of the single hour:minute
                          (multi-time-per-day support).
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
    # Resolve target TZ — prefer the DST-aware IANA name, fall back to the
    # fixed offset (legacy) then server-local. See _resolve_tz().
    tz = _resolve_tz(schedule)
    base = datetime.fromtimestamp(
        ref_ts if ref_ts is not None else time.time(), tz=tz)

    if kind == "daily":
        # Collect candidate (h, m) slots. Multi-time path: schedule["times"]
        # non-empty list of dicts. Single-time fallback: just (h, m) from
        # the top-level fields — preserves pre-multi-time tasks unchanged.
        raw = schedule.get("times") or []
        slots: list[tuple[int, int]] = []
        for entry in raw:
            try:
                th = int(entry.get("hour"))
                tm = int(entry.get("minute"))
            except (AttributeError, TypeError, ValueError):
                continue
            if 0 <= th <= 23 and 0 <= tm <= 59:
                slots.append((th, tm))
        if not slots:
            slots = [(h, m)]
        # Find the earliest candidate strictly > base. Probe today + tomorrow
        # since with multiple slots the next fire might still be today even
        # if the first slot is already past (e.g. slots = [08, 14, 22], now
        # is 15:00 → next is today 22:00).
        best: datetime | None = None
        for delta in (0, 1):
            day_base = base + timedelta(days=delta)
            for th, tm in slots:
                cand = day_base.replace(hour=th, minute=tm,
                                         second=0, microsecond=0)
                if cand > base and (best is None or cand < best):
                    best = cand
        return best.timestamp() if best else None

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
    ensure_available()
    with _STATE_LOCK:
        return copy.deepcopy(sorted(
            _state["tasks"].values(),
            key=lambda t: (not t.get("enabled", True), t.get("created_at", 0)),
        ))


def get_task(tid: str) -> dict | None:
    ensure_available()
    with _STATE_LOCK:
        task = _state["tasks"].get(tid)
        return copy.deepcopy(task) if task is not None else None


def create_task(name: str, prompt: str, schedule: dict,
                 model: str = "", session_mode: str = "fresh") -> dict:
    """Create a task with the given schedule dict. The dict shape
    depends on schedule.kind — see _compute_next_run for valid forms.

    `session_mode` (added 2026-05-28):
      * "reuse" — auto-create one dedicated session at task-creation; every
        run appends to that single JSONL. Muse sees the prior runs as
        history. Good for "续写日报" / long-running threads.
      * "fresh" — DON'T pre-create a session. Each `_execute_task` call
        creates a brand-new session named `[定时] <task> · MM-DD HH:MM`
        and runs in isolation. `task["session_id"]` holds the MOST RECENT
        run's session (so "open session" links to the latest); past runs
        live as independent sessions and can be reached via the
        `list_task_history(tid)` listing.
      * Default: "fresh" — matches the cronjob mental model (each run is
        independent unless you ask otherwise). Old tasks that lack the
        field at all fall back to "reuse" in _execute_task() so we don't
        retroactively break their behavior."""
    ensure_available()
    if session_mode not in ("reuse", "fresh"):
        raise ValueError(
            f"session_mode must be 'reuse' or 'fresh', got {session_mode!r}")
    next_run = _compute_next_run(schedule)
    if next_run is None:
        raise ValueError(f"schedule does not produce a next fire time: {schedule}")
    # Lazy import to avoid backend.sessions ↔ backend.scheduler cycle
    from . import sessions as sess
    # Only "reuse" pre-allocates the bound session. "fresh" leaves
    # session_id empty — first run fills it with whatever it just spun
    # up, and subsequent runs overwrite (the field always points at the
    # MOST RECENT run, list_task_history gives the full historical view).
    sid = ""
    if session_mode == "reuse":
        sess_meta = sess.create_session(
            name=f"{_scheduled_label_prefix()}{name}", model=model)
        sid = sess_meta["id"]
    tid = str(uuid.uuid4())
    task = {
        "id": tid,
        "name": name,
        "prompt": prompt,
        "model": model,
        "session_id": sid,
        "session_mode": session_mode,
        "schedule": schedule,
        "enabled": True,
        "last_run": None,
        "next_run": next_run,
        "created_at": time.time(),
    }
    try:
        with _STATE_LOCK:
            snapshot = copy.deepcopy(_state)
            _state["tasks"][tid] = task
            try:
                _save_state()
            except Exception:
                _restore_state(snapshot)
                raise
    except Exception:
        # Reuse mode allocates a session before the scheduler transaction.
        # A failed scheduler write must not leave that empty session orphaned.
        if sid:
            try:
                sess.begin_session_delete(sid)
                sess.delete_session(sid)
            except Exception as cleanup_error:
                sys.stderr.write(
                    f"[scheduler] failed to roll back seed session "
                    f"({type(cleanup_error).__name__})\n"
                )
        raise
    return copy.deepcopy(task)


def update_task(tid: str, **changes: Any) -> dict | None:
    ensure_available()
    seeded_sid = ""
    rename_after_commit: tuple[str, str] | None = None
    try:
        with _STATE_LOCK:
            snapshot = copy.deepcopy(_state)
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
            if "schedule" in changes and changes["schedule"] is not None:
                next_run = _compute_next_run(changes["schedule"])
                if next_run is None:
                    # The edit form sends the whole draft. Let users rename or
                    # change the model of an already-spent disabled once task,
                    # while still rejecting any newly-selected past schedule.
                    unchanged_spent = (
                        not t.get("enabled", True)
                        and changes["schedule"] == t.get("schedule")
                    )
                    if not unchanged_spent:
                        raise ValueError(
                            "schedule does not produce a future fire time"
                        )
                t["schedule"] = changes["schedule"]
                t["next_run"] = next_run
            if "enabled" in changes and changes["enabled"] is not None:
                enabled = bool(changes["enabled"])
                if enabled and t.get("next_run") is None:
                    next_run = _compute_next_run(t.get("schedule") or {})
                    if next_run is None:
                        raise ValueError(
                            "cannot enable a task without a future fire time"
                        )
                    t["next_run"] = next_run
                t["enabled"] = enabled
            if "session_mode" in changes and changes["session_mode"] is not None:
                new_mode = changes["session_mode"]
                if new_mode not in ("reuse", "fresh"):
                    raise ValueError(
                        f"session_mode must be 'reuse' or 'fresh', got {new_mode!r}")
                old_mode = _effective_session_mode(t)
                t["session_mode"] = new_mode
            # Transitioning fresh → reuse: future runs need a bound session
            # to append to. The most-recent fresh run's session (if any) is
            # a reasonable seed — Muse already has its prior reply in there.
            # If no run yet (session_id empty), create one now so the next
            # run has somewhere to land.
                if (old_mode == "fresh" and new_mode == "reuse"
                        and not t.get("session_id")):
                    from . import sessions as sess
                    sess_meta = sess.create_session(
                        name=f"{_scheduled_label_prefix()}{t.get('name', '')}",
                        model=t.get("model", ""))
                    seeded_sid = sess_meta["id"]
                    t["session_id"] = seeded_sid
            # reuse → fresh: keep the existing session_id around (becomes the
            # "most recent run" pointer); future runs will spin up new ones.
            # The old bound session is NOT deleted — it has the user's prior
            # conversation in it and may be wanted as history.
        # Sync bound session name if the task was renamed — only meaningful
        # for reuse mode (fresh mode's session_id points at a timestamped
        # historical run, renaming it to a generic name would lose info).
            new_name = t.get("name")
            sid = t.get("session_id")
            if (sid and new_name and new_name != old_name
                    and _effective_session_mode(t) == "reuse"):
                rename_after_commit = (sid, str(new_name))
            try:
                _save_state()
            except Exception:
                _restore_state(snapshot)
                raise
            result = copy.deepcopy(t)
    except Exception:
        if seeded_sid:
            try:
                from . import sessions as sess
                sess.begin_session_delete(seeded_sid)
                sess.delete_session(seeded_sid)
            except Exception as cleanup_error:
                sys.stderr.write(
                    f"[scheduler] failed to roll back seed session "
                    f"({type(cleanup_error).__name__})\n"
                )
        raise
    if rename_after_commit:
        sid, new_name = rename_after_commit
        try:
            from . import sessions as sess
            sess.rename_session(
                sid, f"{_scheduled_label_prefix()}{new_name}")
        except Exception as e:
            sys.stderr.write(
                f"[scheduler] update_task({tid}): bound session rename failed "
                f"({type(e).__name__})\n"
            )
    return result


def _effective_session_mode(task: dict) -> str:
    """Resolve session_mode for an in-memory task dict. Old tasks created
    before 2026-05-28 don't have the field — fall back to "reuse" to
    preserve their original behavior (they have a bound session sitting
    in session_id and expect every run to append to it)."""
    return task.get("session_mode") or "reuse"


def list_task_history(tid: str, limit: int = 100) -> list[dict]:
    """All history entries belonging to `tid`, newest first. Used by the
    scheduler detail panel to render "all past runs of this task" — most
    useful in `fresh` mode where each run sits in its own session.

    No new state — filters _state["history"] in place. The history list
    is already capped at _HISTORY_CAP globally, so this is bounded too."""
    ensure_available()
    with _STATE_LOCK:
        out = copy.deepcopy([
            e for e in _state["history"] if e.get("task_id") == tid
        ])
    out.sort(key=lambda e: e.get("ts", 0), reverse=True)
    if limit > 0:
        out = out[:limit]
    return out


def _pending_cleanups_unlocked() -> dict[str, dict]:
    pending = _state.setdefault("cleanup_pending", {})
    if not isinstance(pending, dict):
        raise SchedulerPersistenceError(
            "scheduler cleanup state is invalid; writes are disabled"
        )
    return pending


def _make_task_cleanup_intent(
    tid: str,
    task: dict,
    runtime_session_ids: set[str],
) -> dict:
    intent = {
        "task_id": tid,
        "cleanup_id": str(uuid.uuid4()),
        "session_mode": _effective_session_mode(task),
        "session_id": str(task.get("session_id") or ""),
        "runtime_session_ids": sorted(runtime_session_ids),
        "created_at": time.time(),
    }
    # Reuse the load-time validator so an unsafe legacy task id/session id
    # cannot be promoted into an automatically executed deletion target.
    return _normalize_cleanup_intents({tid: intent}, {})[tid]


def get_task_cleanup(tid: str) -> dict | None:
    """Return the durable cleanup intent for an already-removed task."""
    ensure_available()
    with _STATE_LOCK:
        intent = _pending_cleanups_unlocked().get(tid)
        return copy.deepcopy(intent) if intent is not None else None


def list_pending_task_cleanups() -> list[dict]:
    """Stable snapshot used by startup recovery."""
    ensure_available()
    with _STATE_LOCK:
        return copy.deepcopy(list(_pending_cleanups_unlocked().values()))


def _record_task_cleanup_runtime_sessions(
    tid: str,
    cleanup_id: str,
    session_ids: set[str],
) -> dict | None:
    """Persist newly observed live owners before attempting disconnects."""
    invalid = [sid for sid in session_ids if not _valid_cleanup_id(sid)]
    if invalid:
        raise SchedulerPersistenceError(
            "scheduler cleanup observed an invalid runtime session id"
        )
    with _STATE_LOCK:
        pending = _pending_cleanups_unlocked()
        current = pending.get(tid)
        if current is None:
            return None
        if current.get("cleanup_id") != cleanup_id:
            raise RuntimeError("scheduler cleanup intent changed during retry")
        merged = sorted(set(current.get("runtime_session_ids", [])) | session_ids)
        if merged == current.get("runtime_session_ids", []):
            return copy.deepcopy(current)
        snapshot = copy.deepcopy(_state)
        current["runtime_session_ids"] = merged
        try:
            _save_state()
        except Exception:
            _restore_state(snapshot)
            raise
        return copy.deepcopy(current)


def _complete_task_cleanup(tid: str, cleanup_id: str) -> bool:
    """Atomically acknowledge one exact cleanup intent."""
    with _STATE_LOCK:
        pending = _pending_cleanups_unlocked()
        current = pending.get(tid)
        if current is None:
            return False
        if current.get("cleanup_id") != cleanup_id:
            raise RuntimeError("scheduler cleanup intent changed during completion")
        snapshot = copy.deepcopy(_state)
        pending.pop(tid)
        try:
            _save_state()
        except Exception:
            _restore_state(snapshot)
            raise
        return True


def delete_task(tid: str, *, purge_bound_session: bool = True) -> bool:
    """Durably remove a task, then finish or expose its cleanup transaction.

    The task row and cleanup intent are committed by one atomic scheduler.json
    replacement. Failed external cleanup therefore leaves a stable tid that
    synchronous callers and the HTTP endpoint can retry idempotently.
    """
    ensure_available()
    with _RUN_REGISTRY_LOCK:
        active_owners = [
            task
            for task, owner_tid in tuple(_RUN_TASK_IDS.items())
            if owner_tid == tid and not task.done()
        ]
        runtime_session_ids = {
            sid
            for task in active_owners
            if (sid := _RUN_SESSION_IDS.get(task))
        }
        if purge_bound_session and active_owners:
            raise RuntimeError(
                "cannot synchronously delete a running scheduler task; "
                "use the async API cleanup path"
            )
        with _STATE_LOCK:
            pending = _pending_cleanups_unlocked()
            task = _state["tasks"].get(tid)
            intent = pending.get(tid)
            if task is None and intent is None:
                return tid in _REVOKED_TASK_IDS
            if task is not None and intent is not None:
                raise SchedulerPersistenceError(
                    "scheduler task conflicts with a pending cleanup intent"
                )

            snapshot = copy.deepcopy(_state)
            was_revoked = tid in _REVOKED_TASK_IDS
            try:
                if task is not None:
                    intent = _make_task_cleanup_intent(
                        tid, task, runtime_session_ids
                    )
                    _state["tasks"].pop(tid)
                    pending[tid] = intent
                else:
                    merged = sorted(
                        set(intent.get("runtime_session_ids", []))
                        | runtime_session_ids
                    )
                    if merged != intent.get("runtime_session_ids", []):
                        intent["runtime_session_ids"] = merged
                _REVOKED_TASK_IDS.add(tid)
                _save_state()
            except Exception:
                _restore_state(snapshot)
                if not was_revoked:
                    _REVOKED_TASK_IDS.discard(tid)
                raise
            intent = copy.deepcopy(intent)

    if not purge_bound_session:
        return True
    if intent.get("runtime_session_ids"):
        raise RuntimeError(
            "cannot synchronously finish cleanup with recorded runtime owners; "
            "use the async API cleanup path"
        )
    if intent["session_mode"] == "reuse" and intent.get("session_id"):
        try:
            from .chat import purge_session_storage
            purge_session_storage(intent["session_id"])
        except Exception as e:
            sys.stderr.write(
                f"[scheduler] delete_task({tid}): bound session "
                f"{intent['session_id']} cleanup failed: {e}\n"
            )
            raise
    # A concurrent idempotent cleaner may already have acknowledged the exact
    # intent. Missing here is success; a different cleanup_id raises above.
    _complete_task_cleanup(tid, intent["cleanup_id"])
    return True


async def finish_task_cleanup(tid: str) -> bool:
    """Finish one durable deletion intent; safe to retry by task id."""
    ensure_available()
    async with _cleanup_lock(tid):
        intent = await obs.to_thread_io(
            "scheduler.cleanup_read", tid, get_task_cleanup, tid)
        if intent is None:
            return tid in _REVOKED_TASK_IDS

        runs, _, observed_session_ids = cancel_runs_for_task_now(tid)
        runtime_session_ids = (
            set(intent.get("runtime_session_ids", []))
            | observed_session_ids
        )
        record_error: Exception | None = None
        try:
            refreshed = await obs.to_thread_io(
                "scheduler.cleanup_record",
                tid,
                _record_task_cleanup_runtime_sessions,
                tid,
                intent["cleanup_id"],
                runtime_session_ids,
                owned=True,
            )
            if refreshed is not None:
                intent = refreshed
        except Exception as exc:
            # Runtime owners are already cancelled. Still disconnect and join
            # them before surfacing the persistence failure; the durable intent
            # remains available after restart.
            record_error = exc

        disconnect_errors: list[BaseException] = []
        if runtime_session_ids:
            from .chat import disconnect_client
            results = await asyncio.gather(
                *(disconnect_client(sid) for sid in sorted(runtime_session_ids)),
                return_exceptions=True,
            )
            disconnect_errors = [
                result
                for result in results
                if isinstance(result, BaseException)
            ]
        joined = await join_cancelled_runs(runs)

        if record_error is not None:
            raise record_error
        if disconnect_errors:
            raise disconnect_errors[0]
        if not joined:
            raise RuntimeError(
                "scheduler runtime owner did not stop before cleanup timeout"
            )

        if intent["session_mode"] == "reuse" and intent.get("session_id"):
            from .chat import purge_session_storage_async
            await purge_session_storage_async(intent["session_id"])

        if await obs.to_thread_io(
            "scheduler.cleanup_complete",
            tid,
            _complete_task_cleanup,
            tid,
            intent["cleanup_id"],
            owned=True,
        ):
            return True
        # Another idempotent cleaner may have acknowledged the same record.
        if await obs.to_thread_io(
            "scheduler.cleanup_read", tid, get_task_cleanup, tid) is None:
            return True
        raise RuntimeError("scheduler cleanup intent changed during completion")


async def _resume_pending_task_cleanups() -> None:
    """Best-effort startup recovery for deletion transactions."""
    for intent in list_pending_task_cleanups():
        tid = intent["task_id"]
        try:
            await finish_task_cleanup(tid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sys.stderr.write(
                f"[scheduler] pending cleanup {tid} remains retryable "
                f"after {type(exc).__name__}: {exc}\n"
            )


def list_history(limit: int = 50) -> list[dict]:
    """Most-recent first, capped at `limit`."""
    ensure_available()
    with _STATE_LOCK:
        h = _state.get("history", [])
        return copy.deepcopy(h[-limit:][::-1])


def delete_history_entry(ts: float, task_id: str = "") -> bool:
    """Delete a single history entry identified by its timestamp (the
    composite (ts, task_id) is unique within a user's records — two runs
    of the same task can't share a ts since execution is serialized per
    task; two different tasks could theoretically share a ts if they
    fire in the same second, hence the optional task_id disambiguator).

    Returns True if an entry was removed, False if no match. Safe to
    call with a `ts` that no longer exists (caller can ignore False —
    history may have been pruned by _HISTORY_CAP between display and
    click).
    """
    ensure_available()
    with _STATE_LOCK:
        snapshot = copy.deepcopy(_state)
        h = _state.get("history", [])
        for i, entry in enumerate(h):
            if entry.get("ts") == ts and (not task_id or entry.get("task_id") == task_id):
                h.pop(i)
                try:
                    _save_state()
                except Exception:
                    _restore_state(snapshot)
                    raise
                return True
    return False


def clear_history() -> int:
    """Drop ALL history entries. Returns count cleared. Does NOT touch
    `unread_count` — that's an orthogonal flag (the user might want a
    clean history list while still seeing the bell badge for unread
    runs that arrived after their last drawer-open). If you want both
    cleared, also call ack_unread() at the call site.
    """
    ensure_available()
    with _STATE_LOCK:
        snapshot = copy.deepcopy(_state)
        n = len(_state.get("history", []))
        _state["history"] = []
        try:
            _save_state()
        except Exception:
            _restore_state(snapshot)
            raise
    return n


def get_unread() -> int:
    ensure_available()
    with _STATE_LOCK:
        return _state.get("unread_count", 0)


def ack_unread() -> int:
    ensure_available()
    with _STATE_LOCK:
        snapshot = copy.deepcopy(_state)
        _state["unread_count"] = 0
        try:
            _save_state()
        except Exception:
            _restore_state(snapshot)
            raise
    return 0


# ---------- task execution ----------


@asynccontextmanager
async def _disconnect_runtime_on_cancel(session_id: str):
    """Fence a timed-out/cancelled scheduled turn before releasing its lock."""
    try:
        yield
    except asyncio.CancelledError:
        from .chat import disconnect_client
        try:
            await disconnect_client(session_id)
        except Exception as exc:
            # disconnect_client retains an unfinished SDK close in a per-session
            # fence. A later turn must join it before creating/reusing a client.
            sys.stderr.write(
                f"[scheduler] runtime cleanup pending sid={session_id[:8]} "
                f"({type(exc).__name__})\n"
            )
        raise


async def _run_sdk_task_turn(
    session_id: str,
    model: str,
    prompt: str,
    *,
    activity_owner_id: str = "",
    activity_source_id: str = "",
    activity_summary: str = "",
) -> tuple[str, str | None]:
    """Run one unattended turn under the session-wide SDK mutex."""
    from .chat import (
        _STREAM_EOF,
        _active_turns,
        _session_has_live_watcher,
        _session_runtime_lock_for,
        _sessions_with_inflight_tasks,
        _stream_for,
        _session_message_uuids,
        _TurnResponseBoundary,
        get_client,
    )
    from . import sessions as session_store

    reply_text = ""
    error: str | None = None
    saw_result = False
    # Exit order matters: the cancellation guard tears down/fences the CLI
    # while the session runtime lock is still owned. A timeout can therefore
    # never release the lock and let the next turn reuse a still-running client.
    async with (
        _session_runtime_lock_for(session_id),
        _disconnect_runtime_on_cancel(session_id),
    ):
        # Re-check after acquiring: an interactive request may have reserved
        # the session while this scheduled run was waiting for the mutex.
        active = _active_turns.get(session_id)
        if active is not None and not active.done:
            raise RuntimeError("session is busy with an interactive turn")
        if (_sessions_with_inflight_tasks.get(session_id)
                or _session_has_live_watcher(session_id)):
            raise RuntimeError(
                "session is busy with a background task watcher")
        if session_store.session_is_deleting(session_id):
            raise RuntimeError("session is being deleted")
        meta = session_store.get_session(session_id)
        if meta is None:
            raise RuntimeError("session no longer exists")

        # Start Activity only after this run owns the session runtime and has
        # rechecked the interactive reservation. The run-specific owner keeps
        # a late scheduler finish from closing a newer foreground row.
        if activity_owner_id:
            try:
                from .activity import activity as _activity
                # Mark before the write attempt. Activity.start can fail after
                # persisting the row (for example during SSE publication); in
                # that case the finalizer must still close the owner. The
                # owner-specific missing-row finish is a no-op, so a failure
                # before persistence cannot synthesize a ghost event.
                _mark_current_run_activity_started()
                await obs.to_thread_io(
                    "scheduler.activity_start",
                    session_id,
                    _activity.start,
                    session_id,
                    summary=activity_summary or prompt,
                    kind="scheduled",
                    source_id=activity_source_id,
                    owner_id=activity_owner_id,
                    owned=True,
                )
            except Exception as e:
                sys.stderr.write(
                    f"[scheduler] activity start failed: "
                    f"{type(e).__name__}: {e}\n"
                )

        # Unattended runs intentionally use bypassPermissions: no UI is
        # present to answer a prompt. This is not a sandbox; the process has
        # the service user's OS authority. Deployments that schedule prompts
        # over untrusted content must isolate the service account/container or
        # introduce an explicit non-interactive allow policy.
        client = await get_client(
            session_id=session_id,
            model=model,
            permission="bypassPermissions",
            effort=meta.get("effort") or "auto",
            service_tier=meta.get("service_tier") or "",
        )
        if session_store.session_is_deleting(session_id):
            raise RuntimeError("session is being deleted")

        # Scheduler only needs replay UUIDs, not the display-history index.
        # The latter can write a transcript-index from a worker thread that
        # outlives Task cancellation and recreates it after session deletion.
        # This cached SDK read is synchronous and bounded by the session
        # runtime mutex, so deletion joins the exact scheduler owner first.
        existing_uuids = _session_message_uuids(session_id, model)
        boundary = _TurnResponseBoundary(existing_uuids)

        def _consume(msg: Any) -> str:
            """Collect one scheduler response; True at terminal Result."""
            nonlocal reply_text, error, saw_result
            decision = boundary.classify(msg)
            if decision in {"drop", "stale_result"}:
                return decision
            if decision == "forward" and not isinstance(
                msg, (AssistantMessage, ResultMessage)
            ):
                # Lifecycle/rate-limit records are out-of-band. With no live
                # background watcher (guarded above), they are not scheduler
                # answer text and must not terminate this query.
                return decision
            if isinstance(msg, AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, TextBlock):
                        reply_text += getattr(block, "text", "") or ""
            elif isinstance(msg, ResultMessage):
                saw_result = True
                if getattr(msg, "is_error", False):
                    subtype = getattr(msg, "subtype", None) or "error"
                    errors = getattr(msg, "errors", None) or []
                    detail = "; ".join(str(e) for e in errors)
                    error = (f"SDK result error ({subtype})"
                             + (f": {detail}" if detail else ""))
                return "current_result"
            return decision

        stream = _stream_for(client)
        if stream is not None:
            # The pooled stream pump is the client's sole SDK reader. Attach
            # before query so the response cannot land in its orphan park.
            queue = stream.attach_turn()
            try:
                await client.query(prompt)
                while True:
                    msg = await queue.get()
                    if msg is _STREAM_EOF:
                        raise (
                            stream._failure
                            or RuntimeError(
                                "SDK message stream ended without "
                                "a ResultMessage"
                            )
                        )
                    if _consume(msg) == "current_result":
                        break
            finally:
                stream.detach_turn(queue)
                stream.park_unconsumed(queue)
        else:
            # Test doubles/unpooled clients have no pump, so the SDK bounded
            # reader remains the only reader and is safe here.
            await client.query(prompt)
            async for msg in client.receive_response():
                if _consume(msg) == "current_result":
                    break
            if not saw_result:
                raise RuntimeError(
                    "SDK response ended without a ResultMessage")
    return reply_text, error


async def run_task_now(tid: str) -> bool:
    """Fire-and-forget out-of-schedule run. Returns True if the task exists
    and got scheduled; False if not found. Does NOT advance next_run — this
    is a one-off, the regular schedule keeps ticking.

    Useful as a "retry" affordance after a failure, and as a smoke test
    after editing a task without having to wait for the next fire window."""
    ensure_available()

    def _read_task_for_run() -> dict | None:
        with _STATE_LOCK:
            return _state["tasks"].get(tid)

    task = await obs.to_thread_io(
        "scheduler.task_read", tid, _read_task_for_run)
    if not task:
        return False
    t = _track_task(
        asyncio.create_task(_execute_task(task)),
        task_id=tid,
        session_id=(
            str(task.get("session_id") or "")
            if _effective_session_mode(task) == "reuse"
            else ""
        ),
    )
    t.add_done_callback(_make_task_done(tid))
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
    calls would interleave / drop messages.

    Session handling per mode (see create_task docstring):
      * reuse → use task["session_id"] (always set in this mode).
      * fresh → mint a brand-new session each call. Name carries the
        task name + fire timestamp so the user can tell runs apart in
        the regular session list. task["session_id"] is overwritten to
        point at the latest run — list_task_history(tid) is the full
        view via history entries."""
    from datetime import datetime

    tid = task["id"]
    mode = _effective_session_mode(task)
    reply_text = ""
    error: str | None = None
    cancelled = False
    activity_owner_id = f"{tid}:{uuid.uuid4().hex}"
    from . import sessions as session_store

    if _task_is_revoked(tid):
        return

    async with _task_lock(tid):
        if _task_is_revoked(tid):
            return
        # Resolve `sid` INSIDE the lock so two parallel run-now clicks
        # don't both mint fresh sessions then race on session_id write.
        if mode == "fresh":
            sid = ""
            try:
                from . import sessions as sess

                def _delete_fresh_session() -> None:
                    sess.begin_session_delete(sid)
                    sess.delete_session(sid)

                ts_label = datetime.now().strftime("%m-%d %H:%M")
                sess_meta = await obs.to_thread_io(
                    "scheduler.session_create",
                    tid,
                    sess.create_session,
                    name=f"{_scheduled_label_prefix()}{task['name']} · {ts_label}",
                    model=task.get("model", ""),
                    owned=True,
                )
                sid = sess_meta["id"]

                def _commit_fresh_session() -> bool:
                    with _STATE_LOCK:
                        snapshot = copy.deepcopy(_state)
                        is_revoked = tid in _REVOKED_TASK_IDS
                        if not is_revoked:
                            # "most recent run" pointer
                            task["session_id"] = sid
                            try:
                                _save_state()
                            except Exception:
                                _restore_state(snapshot)
                                raise
                        return is_revoked

                revoked = await obs.to_thread_io(
                    "scheduler.fresh_session_commit",
                    tid,
                    _commit_fresh_session,
                    owned=True,
                )
                if revoked:
                    # A synchronous compatibility caller can revoke while
                    # create_session is doing disk I/O in another thread.
                    # This run never owned the freshly minted empty session.
                    await obs.to_thread_io(
                        "scheduler.session_delete",
                        tid,
                        _delete_fresh_session,
                        owned=True,
                    )
                    return
            except Exception as e:
                if sid:
                    try:
                        await obs.to_thread_io(
                            "scheduler.session_delete",
                            tid,
                            _delete_fresh_session,
                            owned=True,
                        )
                    except Exception as cleanup_error:
                        sys.stderr.write(
                            "[scheduler] failed to roll back fresh session "
                            f"({type(cleanup_error).__name__})\n"
                        )
                # If session minting itself fails (disk full, etc.), record
                # the failure as a history entry rather than crashing the
                # scheduler loop. Bail before touching the SDK.
                sys.stderr.write(
                    f"[scheduler] task {tid} fresh-session mint failed: "
                    f"{type(e).__name__}: {e}\n")
                # A persistence failure has already fenced the scheduler and
                # cannot safely record another mutation. Other mint failures
                # remain visible in history when that history can be saved.
                if not isinstance(e, SchedulerPersistenceError):
                    now = time.time()
                    mint_error = f"{type(e).__name__}: {e}"

                    def _persist_mint_failure() -> None:
                        with _STATE_LOCK:
                            snapshot = copy.deepcopy(_state)
                            if tid not in _REVOKED_TASK_IDS:
                                _state["history"].append({
                                    "task_id": tid,
                                    "task_name": task["name"],
                                    "session_id": "",
                                    "ts": now,
                                    "ok": False,
                                    "error": f"session mint failed: {mint_error}",
                                    "reply_preview": None,
                                })
                                _state["unread_count"] = (
                                    _state.get("unread_count", 0) + 1
                                )
                                try:
                                    _save_state()
                                except Exception:
                                    _restore_state(snapshot)
                                    raise

                    await obs.to_thread_io(
                        "scheduler.mint_failure",
                        tid,
                        _persist_mint_failure,
                        owned=True,
                    )
                return
        else:
            sid = task["session_id"]
        _bind_current_run_session(tid, sid)
        try:
            # Tasks created before the scheduler UI had a model picker stored
            # model=""; SDK then silently fell back to its built-in default
            # (which differs from whatever the user has selected in the chat
            # UI), so the bound session's reply style + capability didn't
            # match what the user expected. Fall back to muselab's MODEL
            # default when task.model is empty.
            from .settings import MODEL as _DEFAULT_MODEL
            model = task.get("model") or _DEFAULT_MODEL
            timeout_s = env_int(
                "MUSELAB_SCHEDULER_TIMEOUT_S", 1800, min_value=0)
            async with asyncio.timeout(timeout_s or None):
                reply_text, error = await _run_sdk_task_turn(
                    sid, model, task["prompt"],
                    activity_owner_id=activity_owner_id,
                    activity_source_id=tid,
                    activity_summary=task["name"],
                )
            if error:
                sys.stderr.write(
                    f"[scheduler] task {tid} ({task['name']}) "
                    f"result is_error: {error}\n")
        except asyncio.TimeoutError:
            error = (
                "scheduled task timed out after "
                f"{env_int('MUSELAB_SCHEDULER_TIMEOUT_S', 1800, min_value=0)}s"
            )
            sys.stderr.write(
                f"[scheduler] task {tid} ({task['name']}) timed out\n")
        except asyncio.CancelledError:
            # Service shutdown cancels tracked scheduler tasks. CancelledError
            # inherits BaseException, so the generic handler below does not
            # see it. Persist an explicit cancelled result before propagating;
            # otherwise `error is None` in finally records a false success.
            cancelled = True
            error = "scheduled task cancelled by service shutdown"
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[scheduler] task {tid} ({task['name']}) failed: {error}\n")
        finally:
            # Don't touch next_run here — the scheduler_loop already advanced
            # it before firing, and run_task_now() is explicitly an out-of-band
            # run that mustn't disturb the regular cadence.
            now = time.time()
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
            def _persist_run_result() -> bool:
                with _STATE_LOCK:
                    is_revoked = (
                        tid in _REVOKED_TASK_IDS
                        or session_store.session_is_deleting(sid)
                    )
                    if is_revoked:
                        return True
                    snapshot = copy.deepcopy(_state)
                    task["last_run"] = now
                    _state["history"].append(entry)
                    # Successful runs and errors both bump unread so the result
                    # remains visible in the bell drawer.
                    _state["unread_count"] = (
                        _state.get("unread_count", 0) + 1
                    )
                    if len(_state["history"]) > _HISTORY_CAP:
                        _state["history"] = _state["history"][-_HISTORY_CAP:]
                    try:
                        _save_state()
                    except SchedulerPersistenceError:
                        _restore_state(snapshot)
                        raise
                    return False

            try:
                revoked = await obs.to_thread_io(
                    "scheduler.run_result",
                    tid,
                    _persist_run_result,
                    owned=True,
                )
            except SchedulerPersistenceError as exc:
                revoked = False
                error = f"SchedulerPersistenceError: {exc}"
                sys.stderr.write(
                    f"[scheduler] task {tid} result was not persisted; "
                    "in-memory mutation rolled back\n"
                )
            try:
                from .activity import activity as _activity
                current = asyncio.current_task()
                with _RUN_REGISTRY_LOCK:
                    should_finish_activity = (
                        current not in _RUN_TASKS
                        or current in _RUN_ACTIVITY_STARTED
                    )
                if should_finish_activity:
                    await obs.to_thread_io(
                        "scheduler.activity_finish",
                        sid,
                        _activity.finish,
                        sid,
                        "cancelled" if (cancelled or revoked) else (
                            "failed" if error else "completed"),
                        owner_id=activity_owner_id,
                        owned=True,
                    )
            except Exception as e:
                sys.stderr.write(
                    f"[scheduler] activity finish failed for {tid}: "
                    f"{type(e).__name__}: {e}\n")
            # Fire Web Push to every subscribed device — but skip when the
            # user is actively at one of their devices (presence heartbeat
            # within GRACE_SECONDS). In-app the UI already flashes the bell
            # badge / fires foreground vibration on unread_count tick;
            # adding a push banner on top would be doubled noise. This
            # mirrors the gate chat.py uses for turn-done pushes, so the
            # behavior is consistent across both event classes — the
            # subscription is the only "notify on / off" switch a user has
            # to manage, not a per-class env toggle (2026-05-28: collapsed
            # 4-toggle UI down to one "notify me" switch).
            # Errors swallowed — push is best-effort, must never break the loop.
            try:
                if not cancelled and not revoked:
                    from . import presence as _presence
                    if _presence.recently_active():
                        # User is at their screen — UI badge + foreground
                        # vibrate handle the notification. Don't double-buzz.
                        # Leave a journal line so a "scheduler never pushes"
                        # report can be distinguished from delivery failure.
                        _age = _presence.last_seen_age()
                        _age_s = f"{_age:.0f}s" if _age is not None else "?"
                        sys.stderr.write(
                            f"[push] sched skipped (presence "
                            f"age={_age_s}) task={tid}\n")
                    else:
                        from . import push as _push
                        # Prefix with ⏰ so scheduler output is recognizable
                        # across locales without a server-side language
                        # preference.
                        title = f"⏰ {task['name']}"
                        if error:
                            body = f"❌ {' '.join(error.split())[:120]}"
                        else:
                            # Strip markdown so the banner shows readable
                            # prose instead of table rows, code fences, etc.
                            from .chat import _plain_preview
                            body = _plain_preview(preview or "")
                        # Offload synchronous subscription HTTPS so a slow
                        # endpoint cannot block the event loop.
                        await asyncio.to_thread(
                            _push.send_to_all, title=title, body=body or "—",
                            url="/", tag=f"task-{tid}",
                            context=f"sched {tid}")
            except Exception as e:
                sys.stderr.write(f"[scheduler] push notify failed for {tid}: {e}\n")


# ---------- daemon loop ----------

# Stagger interval for startup catch-up. After an overnight outage, N daily
# tasks can all be "missed"; firing them simultaneously spawns N CLI
# subprocesses + N model API calls in the same instant (memory + rate-limit
# spike). Spacing them out a few seconds apart keeps the catch-up gentle.
_CATCHUP_STAGGER_S = 5


def _make_task_done(tid: str):
    """Build a done-callback that surfaces an otherwise-swallowed unhandled
    exception from a fire-and-forget task. Shared by the tick loop and the
    startup catch-up path so neither runs blind."""
    def _cb(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            import traceback
            exc = t.exception()
            sys.stderr.write(
                f"[scheduler] unhandled exception in task {tid}: "
                f"{traceback.format_exception(type(exc), exc, exc.__traceback__)[-1]}\n"
            )
    return _cb


async def _delayed_execute(task: dict, delay: float) -> None:
    """Run _execute_task after `delay` seconds — used to stagger catch-up."""
    if delay > 0:
        await asyncio.sleep(delay)
    await _execute_task(task)


async def _scheduler_loop() -> None:
    """Tick every 60 seconds. Any enabled task whose next_run is in the
    past gets fired (concurrently via asyncio.create_task so a slow one
    doesn't hold up the others)."""
    sys.stderr.write("[scheduler] loop started\n")
    while True:
        try:
            now = time.time()

            committed_due: list[dict] = []

            def _advance_due_tasks() -> list[dict]:
                with _STATE_LOCK:
                    due = [
                        task for task in _state["tasks"].values()
                        if task.get("enabled", True)
                        and task.get("next_run")
                        and task["next_run"] <= now
                    ]
                    if not due:
                        return []
                    state_snapshot = copy.deepcopy(_state)
                    for task in due:
                        # Persist advancement before launch so a long-running
                        # task cannot fire twice on a later scheduler tick.
                        task["next_run"] = _compute_next_run(task["schedule"])
                        if (task.get("schedule") or {}).get("kind") == "once":
                            task["enabled"] = False
                    try:
                        _save_state()
                    except Exception:
                        _restore_state(state_snapshot)
                        raise
                    committed_due.extend(due)
                    return due

            try:
                due_tasks = await obs.to_thread_io(
                    "scheduler.advance_due", "scheduler", _advance_due_tasks,
                    owned=True,
                )
            except asyncio.CancelledError:
                # owned I/O joined the commit. Preserve launch-after-commit even
                # when shutdown arrives during fsync, then propagate cancellation.
                due_tasks = list(committed_due)
                for task in due_tasks:
                    task_obj = _track_task(
                        asyncio.create_task(_execute_task(task)),
                        task_id=str(task.get("id") or ""),
                        session_id=(
                            str(task.get("session_id") or "")
                            if _effective_session_mode(task) == "reuse"
                            else ""
                        ),
                    )
                    task_obj.add_done_callback(
                        _make_task_done(task.get("id", "?")))
                raise
            for task in due_tasks:
                task_obj = _track_task(
                    asyncio.create_task(_execute_task(task)),
                    task_id=str(task.get("id") or ""),
                    session_id=(
                        str(task.get("session_id") or "")
                        if _effective_session_mode(task) == "reuse"
                        else ""
                    ),
                )
                task_obj.add_done_callback(_make_task_done(task.get("id", "?")))
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
    now = time.time()
    missed: list[dict] = []
    # Cap catch-up window at 24 h. Without this, a task whose next_run
    # is N days stale (muselab was offline a week, or the task was
    # disabled-then-re-enabled mid-flight weeks ago and its stale
    # next_run got carried forward) fires immediately on startup with
    # a prompt that was contextually relevant a week ago. 24 h is
    # generous enough to cover overnight outages while filtering
    # actually-stale entries.
    _CATCHUP_MAX_AGE_S = 24 * 3600
    startup_snapshot: dict | None = None

    def _load_and_advance_startup() -> list[dict]:
        nonlocal startup_snapshot
        startup_missed: list[dict] = []
        with _STATE_LOCK:
            _load_state()
            state_snapshot = copy.deepcopy(_state)
            startup_snapshot = state_snapshot
            for task in _state["tasks"].values():
                sched = task.get("schedule")
                if not sched:
                    continue
                nr = task.get("next_run")
                # Enabled tasks missed within the bounded outage window catch up
                # once; stale or disabled tasks only roll their schedule forward.
                if (nr and nr <= now and task.get("enabled", True)
                        and (now - nr) < _CATCHUP_MAX_AGE_S):
                    startup_missed.append(task)
                elif nr and nr <= now and task.get("enabled", True):
                    sys.stderr.write(
                        f"[scheduler] skipping stale catch-up for task "
                        f"{task.get('id','?')} ({task.get('name','?')}): "
                        f"missed {(now - nr) / 3600:.1f}h ago, beyond 24h window\n")
                task["next_run"] = _compute_next_run(sched)
                if sched.get("kind") == "once" and task["next_run"] is None:
                    task["enabled"] = False
            try:
                _save_state()
            except Exception:
                _restore_state(state_snapshot)
                raise
        return startup_missed

    try:
        missed = await obs.to_thread_io(
            "scheduler.startup_state", "scheduler", _load_and_advance_startup,
            owned=True,
        )
    except asyncio.CancelledError:
        # Startup did not reach its catch-up launch boundary. Restore the exact
        # pre-advance schedule so the next process can recover the missed window.
        if startup_snapshot is not None:
            def _rollback_startup_advance() -> None:
                with _STATE_LOCK:
                    _restore_state(startup_snapshot)
                    _save_state()

            await obs.to_thread_io(
                "scheduler.startup_rollback",
                "scheduler",
                _rollback_startup_advance,
                owned=True,
            )
        raise
    # Resolve crash-interrupted deletions before starting new scheduled work.
    # Failures are logged and retain their exact durable intent, so scheduler
    # availability does not depend on one damaged external session tree.
    await _resume_pending_task_cleanups()

    # Kick off catch-up runs — staggered so an overnight outage with many
    # daily tasks doesn't spawn every CLI subprocess at once (thundering
    # herd). Each carries the same done-callback the tick loop uses, so a
    # catch-up that crashes isn't silently swallowed.
    for i, task in enumerate(missed):
        sys.stderr.write(
            f"[scheduler] catching up missed window for task "
            f"{task['id']} ({task.get('name','?')})\n")
        t = _track_task(
            asyncio.create_task(
                _delayed_execute(task, i * _CATCHUP_STAGGER_S)
            ),
            task_id=str(task.get("id") or ""),
            session_id=(
                str(task.get("session_id") or "")
                if _effective_session_mode(task) == "reuse"
                else ""
            ),
        )
        t.add_done_callback(_make_task_done(task.get("id", "?")))
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_scheduler() -> None:
    """Stop the tick loop and every scheduled execution owned by this process."""
    global _scheduler_task

    with _RUN_REGISTRY_LOCK:
        tracked = tuple(_RUN_TASKS)
    tasks = [task for task in (_scheduler_task, *tracked)
             if task is not None and not task.done()]
    _scheduler_task = None
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    with _RUN_REGISTRY_LOCK:
        _RUN_TASKS.clear()
        _RUN_TASK_IDS.clear()
        _RUN_SESSION_IDS.clear()
        _RUN_ACTIVITY_STARTED.clear()
