"""CRUD + bell-chip read/ack endpoints for the scheduler.

GET    /api/scheduler/tasks       — list + current unread count
POST   /api/scheduler/tasks       — create
PATCH  /api/scheduler/tasks/{id}  — edit (rename / change time / toggle enabled)
DELETE /api/scheduler/tasks/{id}  — remove (does NOT delete the bound session)
GET    /api/scheduler/history     — most-recent-first run log
POST   /api/scheduler/ack         — mark unread = 0 (called when user opens the bell drawer)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from . import scheduler as sched


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    model: str = ""


class TaskPatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
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
    return sched.create_task(
        name=req.name,
        prompt=req.prompt,
        hour=req.hour,
        minute=req.minute,
        model=req.model,
    )


@router.patch("/tasks/{tid}", dependencies=[Depends(require_token)])
def patch_task_endpoint(tid: str, req: TaskPatch) -> dict:
    # Pydantic v2: model_dump(exclude_unset=True) for "only sent fields"
    changes = req.model_dump(exclude_unset=True)
    t = sched.update_task(tid, **changes)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@router.delete("/tasks/{tid}", dependencies=[Depends(require_token)])
def delete_task_endpoint(tid: str) -> dict:
    if not sched.delete_task(tid):
        raise HTTPException(404, "task not found")
    return {"deleted": tid}


@router.get("/history", dependencies=[Depends(require_token)])
def history_endpoint(limit: int = 50) -> dict:
    return {
        "history": sched.list_history(limit=limit),
        "unread_count": sched.get_unread(),
    }


@router.post("/ack", dependencies=[Depends(require_token)])
def ack_endpoint() -> dict:
    return {"unread_count": sched.ack_unread()}
