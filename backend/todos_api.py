"""Authenticated to-do board API (cross-device sync)."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from .auth import require_token
from .capability_tickets import tickets
from .todos import todos

router = APIRouter(prefix="/api/todos", tags=["todos"])
_EVENT_TICKET_TTL_S = 45


class TodoItem(BaseModel):
    id: str = ""
    text: str = ""
    completed: bool = False
    priority: str = "medium"


class TodosReplaceRequest(BaseModel):
    items: list[TodoItem]
    base_revision: int | None = None


@router.get("", dependencies=[Depends(require_token)])
def list_todos():
    return todos.get()


@router.put("", dependencies=[Depends(require_token)])
def replace_todos(req: TodosReplaceRequest):
    result = todos.replace(
        [item.model_dump() for item in req.items],
        req.base_revision,
    )
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="todos changed elsewhere; re-fetch and retry",
        )
    return {"ok": True, **result}


@router.post("/events-ticket", dependencies=[Depends(require_token)])
def mint_todo_event_ticket() -> dict:
    ticket = tickets.mint(
        "todos",
        (),
        ttl=_EVENT_TICKET_TTL_S,
        single_use=True,
    )
    return {"ticket": ticket, "expires_in": _EVENT_TICKET_TTL_S}


def _require_todo_event_ticket(ticket: str = Query("")) -> None:
    if not tickets.validate(ticket, "todos", ()):
        raise HTTPException(
            status_code=401,
            detail="invalid or expired todo event ticket",
        )


@router.get("/events", dependencies=[Depends(_require_todo_event_ticket)])
async def todo_events() -> EventSourceResponse:
    """Push to-do board changes to every open device."""

    async def events() -> AsyncIterator[ServerSentEvent]:
        async with todos.subscribe() as queue:
            yield ServerSentEvent(
                event="ready",
                data=json.dumps(
                    todos.get(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            while True:
                payload = await queue.get()
                yield ServerSentEvent(
                    event="update",
                    data=json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )

    return EventSourceResponse(
        events(),
        ping=20,
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
