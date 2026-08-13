"""CRUD + bell-chip read/ack endpoints for the scheduler.

GET    /api/scheduler/tasks         — list + current unread count
POST   /api/scheduler/tasks         — create
PATCH  /api/scheduler/tasks/{id}    — edit (rename / change time / toggle enabled)
DELETE /api/scheduler/tasks/{id}    — remove (does NOT delete the bound session)
GET    /api/scheduler/history       — most-recent-first run log
DELETE /api/scheduler/history       — clear ALL history entries
DELETE /api/scheduler/history/{ts}  — delete a single history entry (by timestamp,
                                       optional ?task_id= to disambiguate same-second runs)
POST   /api/scheduler/ack           — mark unread = 0 (called when user opens the bell drawer)
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_token
from . import scheduler as sched


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class TimeSlot(BaseModel):
    """A single hh:mm fire window. Used by `daily` schedules to support
    multiple fire times per day (e.g. 08:00 + 14:00 + 22:00)."""
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class ScheduleIn(BaseModel):
    """Polymorphic schedule. Frontend sends one shape per kind; only the
    fields relevant to that kind are required, rest ignored.

    `tz_offset_minutes` is the user's UTC offset east-positive (Beijing=+480,
    NYC=-240). Browser supplies via `-new Date().getTimezoneOffset()`. When
    absent (legacy tasks), scheduler falls back to server-local TZ — keeps
    existing schedules from drifting after the upgrade.

    `times` (opt-in, daily-only) supports multiple fire times per day. When
    present and non-empty, scheduler.py treats it as the source of truth for
    daily kind; otherwise falls back to the single (hour, minute) — keeps
    pre-upgrade tasks running unchanged. `hour`/`minute` are still always
    required so weekly/monthly/once can stay single-time and the schema
    contract is unambiguous."""
    kind: str = Field(pattern="^(daily|weekly|monthly|once)$")
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    # Cap at 24 slots — one per hour is already absurdly noisy; bounds the
    # payload size against a malicious or buggy client sending thousands.
    times: list[TimeSlot] | None = Field(default=None, max_length=24)
    weekdays: list[int] | None = None   # weekly: 0..6 (0=Mon)
    day: int | None = Field(default=None, ge=1, le=31)  # monthly
    year: int | None = Field(default=None, ge=2024, le=2100)   # once
    month: int | None = Field(default=None, ge=1, le=12)       # once
    # 'day' is reused for the day-of-month in `once` too.
    # ±24h (1440 min) covers every real-world TZ including Kiribati / Samoa.
    tz_offset_minutes: int | None = Field(default=None, ge=-1440, le=1440)
    # IANA timezone name (e.g. "America/New_York"), browser supplies via
    # Intl.DateTimeFormat().resolvedOptions().timeZone. Preferred over the
    # raw offset because it's DST-aware — the scheduler keeps firing at the
    # same wall-clock time across spring-forward / fall-back. Validated for
    # real at ZoneInfo() construction in scheduler.py; here we just bound the
    # length. tz_offset_minutes stays as the legacy fallback.
    tz: str | None = Field(default=None, max_length=64)


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1)
    schedule: ScheduleIn
    model: str = ""
    # "fresh" (default) → each run gets a brand-new session, no cross-
    # contamination. "reuse" → one session pre-allocated at task creation,
    # every run appends. See scheduler.create_task docstring.
    session_mode: str = Field(default="fresh", pattern="^(reuse|fresh)$")


class TaskPatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    schedule: ScheduleIn | None = None
    model: str | None = None
    enabled: bool | None = None
    # Pydantic re-checks the pattern when the field is set, so an invalid
    # value via PATCH gets a 422 with a clear message.
    session_mode: str | None = Field(default=None, pattern="^(reuse|fresh)$")


@router.get("/tasks", dependencies=[Depends(require_token)])
def list_tasks_endpoint() -> dict:
    return {
        "tasks": sched.list_tasks(),
        "unread_count": sched.get_unread(),
    }


@router.post("/tasks", dependencies=[Depends(require_token)])
def create_task_endpoint(req: TaskIn) -> dict:
    try:
        return sched.create_task(
            name=req.name,
            prompt=req.prompt,
            schedule=req.schedule.model_dump(exclude_none=True),
            model=req.model,
            session_mode=req.session_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.patch("/tasks/{tid}", dependencies=[Depends(require_token)])
def patch_task_endpoint(tid: str, req: TaskPatch) -> dict:
    # Pydantic v2: model_dump(exclude_unset=True) for "only sent fields"
    changes = req.model_dump(exclude_unset=True)
    # If a schedule was sent, normalize its dict shape (drop None fields)
    if "schedule" in changes and changes["schedule"]:
        changes["schedule"] = {k: v for k, v in changes["schedule"].items()
                                if v is not None}
    t = sched.update_task(tid, **changes)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@router.delete("/tasks/{tid}", dependencies=[Depends(require_token)])
async def delete_task_endpoint(tid: str) -> dict:
    task = sched.get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    # Remove scheduler ownership first, then use chat's async purge path so
    # asyncio turns/watchers are cancelled on the event-loop thread. The sync
    # scheduler helper remains available for non-server callers and tests.
    if not sched.delete_task(tid, purge_bound_session=False):
        raise HTTPException(404, "task not found")
    # delete_task() installs the durable in-process revocation fence before it
    # returns.  Now cancel and join every tracked incarnation so a run that was
    # already inside the SDK cannot append history/unread state after DELETE
    # has acknowledged success.  This is required for fresh tasks too even
    # though their completed sessions are intentionally retained.
    async def _finish_cleanup() -> None:
        runs, _, session_ids = sched.cancel_runs_for_task_now(tid)
        # A cancelled SDK receive/connect may not unwind until its CLI
        # transport is closed. Disconnect captured runtimes before waiting
        # for the owners; fresh sessions remain on disk, this only releases
        # live clients.
        if session_ids:
            from .chat import disconnect_client
            results = await asyncio.gather(
                *(disconnect_client(sid) for sid in session_ids),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        await sched.join_cancelled_runs(runs)
        if (sched._effective_session_mode(task) == "reuse"
                and task.get("session_id")):
            from .chat import purge_session_storage_async
            await purge_session_storage_async(task["session_id"])

    cleanup = asyncio.create_task(_finish_cleanup())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Once the task has been durably removed/revoked, an HTTP disconnect
        # must not leave its runtime half-cleaned. Preserve caller cancellation
        # only after the exact cleanup owner reaches a terminal result.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
    return {"deleted": tid}


@router.post("/tasks/{tid}/run", dependencies=[Depends(require_token)])
async def run_task_now_endpoint(tid: str) -> dict:
    """Trigger an out-of-schedule run of an existing task. Fire-and-forget:
    the actual LLM call happens in a background asyncio.task so the HTTP
    response returns immediately. Activity SSE carries running/terminal state;
    the UI refreshes history when that terminal event arrives.

    Used for: (a) "retry" on a failed history entry; (b) manual smoke-test
    after editing prompt / model without waiting for the next fire window."""
    task = sched.get_task(tid)
    if not task:
        raise HTTPException(404, "task not found")
    await sched.run_task_now(tid)
    return {"ok": True, "task_id": tid}


@router.get("/history", dependencies=[Depends(require_token)])
def history_endpoint(limit: int = Query(50, ge=1, le=500)) -> dict:
    # limit clamped to [1, 500]. Unbounded limit=-1 returned last 1 entry
    # (slicing semantics) and limit=99999 returned the whole history
    # blocking the frontend's render — both surfaced as confusing UI bugs.
    return {
        "history": sched.list_history(limit=limit),
        "unread_count": sched.get_unread(),
    }


@router.get("/tasks/{tid}/history", dependencies=[Depends(require_token)])
def task_history_endpoint(tid: str,
                          limit: int = Query(100, ge=1, le=500)) -> dict:
    """All history entries for ONE task, newest first. Powers the
    "this task's past runs" list in the scheduler detail view —
    especially useful for fresh-mode tasks where each run is its own
    session and the user wants to jump back to a specific snapshot."""
    if not sched.get_task(tid):
        raise HTTPException(404, "task not found")
    return {
        "history": sched.list_task_history(tid, limit=limit),
    }


@router.delete("/history", dependencies=[Depends(require_token)])
def clear_history_endpoint() -> dict:
    """Drop ALL run-history entries. Doesn't touch the unread badge —
    user can call POST /ack separately if they also want to zero that.
    Returns the count of entries removed."""
    n = sched.clear_history()
    return {"cleared": n}


@router.delete("/history/{ts}", dependencies=[Depends(require_token)])
def delete_history_entry_endpoint(
    ts: float,
    task_id: str = Query("", description="optional disambiguator when two tasks share a ts"),
) -> dict:
    """Delete a single history row identified by its timestamp.
    Returns 200 even if nothing matched — the row may already have been
    pruned by _HISTORY_CAP between the user seeing it and clicking. UI
    just unconditionally refreshes after; idempotence keeps that simple."""
    sched.delete_history_entry(ts, task_id)
    return {"deleted": True}


@router.post("/ack", dependencies=[Depends(require_token)])
def ack_endpoint() -> dict:
    return {"unread_count": sched.ack_unread()}
