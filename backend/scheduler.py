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
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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
}

_scheduler_task: asyncio.Task | None = None
# Strong references to fire-and-forget execution tasks (tick-loop fires,
# run-now clicks, startup catch-up). asyncio holds only a weak reference to
# a task, so a bare `create_task(...)` whose handle goes out of scope can be
# garbage-collected mid-run, silently cancelling a scheduled run. Each task
# is added here and removed by its done-callback so the set stays bounded.
_RUN_TASKS: set[asyncio.Task] = set()


def _track_task(t: asyncio.Task) -> asyncio.Task:
    """Hold a strong ref to a fire-and-forget task until it completes."""
    _RUN_TASKS.add(t)
    t.add_done_callback(_RUN_TASKS.discard)
    return t
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
    with _STATE_LOCK:
        return sorted(
            _state["tasks"].values(),
            key=lambda t: (not t.get("enabled", True), t.get("created_at", 0)),
        )


def get_task(tid: str) -> dict | None:
    with _STATE_LOCK:
        return _state["tasks"].get(tid)


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
    if session_mode not in ("reuse", "fresh"):
        raise ValueError(
            f"session_mode must be 'reuse' or 'fresh', got {session_mode!r}")
    next_run = _compute_next_run(schedule)
    if next_run is None and schedule.get("kind") != "once":
        # Allow `once` with no next_run only when explicitly so (past
        # date) — but the API layer should reject those upfront.
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
    with _STATE_LOCK:
        _state["tasks"][tid] = task
        _save_state()
    return task


def update_task(tid: str, **changes: Any) -> dict | None:
    with _STATE_LOCK:
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
            if old_mode == "fresh" and new_mode == "reuse" and not t.get("session_id"):
                try:
                    from . import sessions as sess
                    sess_meta = sess.create_session(
                        name=f"{_scheduled_label_prefix()}{t.get('name', '')}",
                        model=t.get("model", ""))
                    t["session_id"] = sess_meta["id"]
                except Exception as e:
                    sys.stderr.write(
                        f"[scheduler] update_task({tid}): seed session for "
                        f"reuse mode failed: {e}\n")
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
    with _STATE_LOCK:
        out = [e for e in _state["history"] if e.get("task_id") == tid]
    out.sort(key=lambda e: e.get("ts", 0), reverse=True)
    if limit > 0:
        out = out[:limit]
    return out


def delete_task(tid: str) -> bool:
    """Delete a task and (only for reuse-mode) its bound session.

    Behavior by mode (chosen 2026-05-28 per user spec):
      * reuse — delete the bound session too. There's exactly one; no
        per-run history apart from what's inside that JSONL; orphaning
        it would litter the history picker with un-attributable
        `[定时] xxx` rows.
      * fresh — DON'T touch any sessions. Each prior run is its own
        independent session with potentially valuable history snapshots;
        cascading delete could nuke dozens at once. The user can multi-
        select and delete in the regular sessions list if they want.

    Returns True if the task existed and got removed."""
    with _STATE_LOCK:
        t = _state["tasks"].pop(tid, None)
        if not t:
            return False
        mode = _effective_session_mode(t)
        sid = t.get("session_id")
        _save_state()
    # Cascade OUTSIDE the lock — the purge touches disk (SDK JSONL, sidecar,
    # attachments) and must not stall other scheduler state operations.
    # purge_session_storage is the same full-cleanup path the HTTP session
    # delete uses; the old sess.delete_session-only call left the SDK JSONL
    # behind, so the "deleted" session could re-appear in the session list.
    if mode == "reuse" and sid:
        try:
            from .chat import purge_session_storage
            purge_session_storage(sid)
        except Exception as e:
            sys.stderr.write(
                f"[scheduler] delete_task({tid}): bound session {sid} "
                f"cleanup failed: {e}\n")
    return True


def list_history(limit: int = 50) -> list[dict]:
    """Most-recent first, capped at `limit`."""
    with _STATE_LOCK:
        h = _state.get("history", [])
        return h[-limit:][::-1]


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
    with _STATE_LOCK:
        h = _state.get("history", [])
        for i, entry in enumerate(h):
            if entry.get("ts") == ts and (not task_id or entry.get("task_id") == task_id):
                h.pop(i)
                _save_state()
                return True
    return False


def clear_history() -> int:
    """Drop ALL history entries. Returns count cleared. Does NOT touch
    `unread_count` — that's an orthogonal flag (the user might want a
    clean history list while still seeing the bell badge for unread
    runs that arrived after their last drawer-open). If you want both
    cleared, also call ack_unread() at the call site.
    """
    with _STATE_LOCK:
        n = len(_state.get("history", []))
        _state["history"] = []
        _save_state()
    return n


def get_unread() -> int:
    with _STATE_LOCK:
        return _state.get("unread_count", 0)


def ack_unread() -> int:
    with _STATE_LOCK:
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
    with _STATE_LOCK:
        task = _state["tasks"].get(tid)
    if not task:
        return False
    t = _track_task(asyncio.create_task(_execute_task(task)))
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
    from .chat import get_client  # local import — avoids startup cycle
    from datetime import datetime

    tid = task["id"]
    mode = _effective_session_mode(task)
    reply_text = ""
    error: str | None = None

    async with _task_lock(tid):
        # Resolve `sid` INSIDE the lock so two parallel run-now clicks
        # don't both mint fresh sessions then race on session_id write.
        if mode == "fresh":
            try:
                from . import sessions as sess
                ts_label = datetime.now().strftime("%m-%d %H:%M")
                sess_meta = sess.create_session(
                    name=f"{_scheduled_label_prefix()}{task['name']} · {ts_label}",
                    model=task.get("model", ""))
                sid = sess_meta["id"]
                with _STATE_LOCK:
                    task["session_id"] = sid   # "most recent run" pointer
                    _save_state()
            except Exception as e:
                # If session minting itself fails (disk full, etc.), record
                # the failure as a history entry rather than crashing the
                # scheduler loop. Bail before touching the SDK.
                sys.stderr.write(
                    f"[scheduler] task {tid} fresh-session mint failed: "
                    f"{type(e).__name__}: {e}\n")
                now = time.time()
                with _STATE_LOCK:
                    _state["history"].append({
                        "task_id": tid,
                        "task_name": task["name"],
                        "session_id": "",
                        "ts": now,
                        "ok": False,
                        "error": f"session mint failed: {type(e).__name__}: {e}",
                        "reply_preview": None,
                    })
                    _state["unread_count"] = _state.get("unread_count", 0) + 1
                    _save_state()
                return
        else:
            sid = task["session_id"]
        try:
            # Tasks created before the scheduler UI had a model picker stored
            # model=""; SDK then silently fell back to its built-in default
            # (which differs from whatever the user has selected in the chat
            # UI), so the bound session's reply style + capability didn't
            # match what the user expected. Fall back to muselab's MODEL
            # default when task.model is empty.
            from .settings import MODEL as _DEFAULT_MODEL
            model = task.get("model") or _DEFAULT_MODEL
            # SECURITY — unattended runs use bypassPermissions BY DESIGN.
            # A scheduled task fires with no human present, so there is no
            # one to answer a permission prompt; any mode that can block on
            # can_use_tool would hang the run forever. The cost is that the
            # agent can run arbitrary Bash (mv / rm / write files) with no
            # confirmation. The real escalation path is PROMPT INJECTION:
            # if a scheduled prompt pulls in external content (e.g.
            # "summarize my latest email / fetch this page"), injected
            # instructions in that content execute unchecked. Mitigations
            # already in place: the SDK disallowed_tools blocklist (chat.py)
            # and the archive-root sandbox. Self-hosters who schedule tasks
            # that read untrusted external content should keep those prompts
            # narrow. Do NOT "fix" this by switching to default/acceptEdits
            # without also giving unattended runs a non-interactive
            # permission resolver — otherwise scheduled tasks will silently
            # hang. (See audit E/247.)
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
            await client.query(task["prompt"])
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, TextBlock):
                            reply_text += getattr(block, "text", "") or ""
                elif isinstance(msg, ResultMessage):
                    # The SDK reports turn-level failures (max turns, budget
                    # exceeded, permission denied, API errors) THROUGH a
                    # normal ResultMessage with is_error=True — not as an
                    # exception. Without this check the run was recorded as
                    # ok and the user saw a "success" entry with whatever
                    # partial text had streamed.
                    if getattr(msg, "is_error", False):
                        _subtype = getattr(msg, "subtype", None) or "error"
                        _errs = getattr(msg, "errors", None) or []
                        _detail = "; ".join(str(e) for e in _errs)
                        error = (f"SDK result error ({_subtype})"
                                 + (f": {_detail}" if _detail else ""))
                        sys.stderr.write(
                            f"[scheduler] task {tid} ({task['name']}) "
                            f"result is_error: {error}\n")
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
            with _STATE_LOCK:
                _state["history"].append(entry)
                # Successful runs bump unread; errors also bump so the user
                # notices them — but they show as red in the UI.
                _state["unread_count"] = _state.get("unread_count", 0) + 1
                if len(_state["history"]) > _HISTORY_CAP:
                    _state["history"] = _state["history"][-_HISTORY_CAP:]
                _save_state()
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
                from . import presence as _presence
                if _presence.recently_active():
                    # User is at their screen — UI badge + foreground vibrate
                    # handle the notification. Don't double-buzz. Leave a
                    # journal line so a "scheduler never pushes" report can
                    # be told apart from an actual delivery failure.
                    # None-safe: a device can flip to hidden between
                    # recently_active() and this call (see chat.py sites).
                    _age = _presence.last_seen_age()
                    _age_s = f"{_age:.0f}s" if _age is not None else "?"
                    sys.stderr.write(
                        f"[push] sched skipped (presence "
                        f"age={_age_s}) task={tid}\n")
                else:
                    from . import push as _push
                    # Prefix with ⏰ so the notification banner is universally
                    # recognizable as muselab scheduler output across both zh
                    # and en users. (Pushes are server-side rendered; we don't
                    # know the user's lang preference, so language-neutral
                    # icon beats either zh or en prose.)
                    title = f"⏰ {task['name']}"
                    if error:
                        body = f"❌ {' '.join(error.split())[:120]}"
                    else:
                        # Strip markdown so the banner shows readable prose
                        # instead of table rows, code fences, etc.
                        from .chat import _plain_preview
                        body = _plain_preview(preview or "")
                    # Offload pywebpush's synchronous per-subscription HTTPS
                    # to a thread so a slow/dead push endpoint can't block the
                    # event loop (and every concurrent SSE/HTTP request) while
                    # the scheduler fans out task notifications.
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
            with _STATE_LOCK:
                snapshot = list(_state["tasks"].values())
            for task in snapshot:
                if not task.get("enabled", True):
                    continue
                nr = task.get("next_run")
                if nr and nr <= now:
                    # Advance next_run optimistically so a long-running
                    # task doesn't fire twice if we tick again before it
                    # finishes.
                    with _STATE_LOCK:
                        task["next_run"] = _compute_next_run(task["schedule"])
                        # A `once` task fires exactly once — after firing it
                        # has no future next_run, so disable it too. This
                        # flips the UI toggle off so the user sees it's spent,
                        # instead of a dead-enabled task that can never fire
                        # again.
                        if (task.get("schedule") or {}).get("kind") == "once":
                            task["enabled"] = False
                        _save_state()
                    task_obj = _track_task(asyncio.create_task(_execute_task(task)))
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
    with _STATE_LOCK:
        _load_state()
        for task in _state["tasks"].values():
            sched = task.get("schedule")
            if not sched:
                continue
            nr = task.get("next_run")
            # If next_run is in the past AND the task is enabled, the window
            # was missed while we were down. Disabled tasks just get next_run
            # rolled forward (no catch-up), matching their "don't fire" intent.
            if (nr and nr <= now and task.get("enabled", True)
                    and (now - nr) < _CATCHUP_MAX_AGE_S):
                missed.append(task)
            elif nr and nr <= now and task.get("enabled", True):
                sys.stderr.write(
                    f"[scheduler] skipping stale catch-up for task "
                    f"{task.get('id','?')} ({task.get('name','?')}): "
                    f"missed {(now - nr) / 3600:.1f}h ago, beyond 24h window\n")
            task["next_run"] = _compute_next_run(sched)
            # A spent `once` task (date in the past → no future next_run)
            # should be disabled, not left dead-enabled. Covers both tasks
            # that just missed their window (already queued for catch-up
            # above, so they still fire one final time) and ones that fired
            # in a previous run but stayed enabled. Future-dated `once` tasks
            # keep next_run set, so they stay enabled until they fire.
            if sched.get("kind") == "once" and task["next_run"] is None:
                task["enabled"] = False
        _save_state()
    # Kick off catch-up runs — staggered so an overnight outage with many
    # daily tasks doesn't spawn every CLI subprocess at once (thundering
    # herd). Each carries the same done-callback the tick loop uses, so a
    # catch-up that crashes isn't silently swallowed.
    for i, task in enumerate(missed):
        sys.stderr.write(
            f"[scheduler] catching up missed window for task "
            f"{task['id']} ({task.get('name','?')})\n")
        t = _track_task(asyncio.create_task(
            _delayed_execute(task, i * _CATCHUP_STAGGER_S)))
        t.add_done_callback(_make_task_done(task.get("id", "?")))
    _scheduler_task = asyncio.create_task(_scheduler_loop())
