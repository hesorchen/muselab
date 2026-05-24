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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_token
from . import scheduler as sched


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class ScheduleIn(BaseModel):
    """Polymorphic schedule. Frontend sends one shape per kind; only the
    fields relevant to that kind are required, rest ignored.

    `tz_offset_minutes` is the user's UTC offset east-positive (Beijing=+480,
    NYC=-240). Browser supplies via `-new Date().getTimezoneOffset()`. When
    absent (legacy tasks), scheduler falls back to server-local TZ — keeps
    existing schedules from drifting after the upgrade."""
    kind: str = Field(pattern="^(daily|weekly|monthly|once)$")
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    weekdays: list[int] | None = None   # weekly: 0..6 (0=Mon)
    day: int | None = Field(default=None, ge=1, le=31)  # monthly
    year: int | None = Field(default=None, ge=2024, le=2100)   # once
    month: int | None = Field(default=None, ge=1, le=12)       # once
    # 'day' is reused for the day-of-month in `once` too.
    # ±24h (1440 min) covers every real-world TZ including Kiribati / Samoa.
    tz_offset_minutes: int | None = Field(default=None, ge=-1440, le=1440)


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1)
    schedule: ScheduleIn
    model: str = ""


class TaskPatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    schedule: ScheduleIn | None = None
    model: str | None = None
    enabled: bool | None = None


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
def delete_task_endpoint(tid: str) -> dict:
    if not sched.delete_task(tid):
        raise HTTPException(404, "task not found")
    return {"deleted": tid}


@router.post("/tasks/{tid}/run", dependencies=[Depends(require_token)])
async def run_task_now_endpoint(tid: str) -> dict:
    """Trigger an out-of-schedule run of an existing task. Fire-and-forget:
    the actual LLM call happens in a background asyncio.task so the HTTP
    response returns immediately. UI polls /history to see the result.

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
