"""Authenticated activity-center API."""

import hashlib
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from .activity import activity
from .auth import require_token
from .capability_tickets import tickets

router = APIRouter(prefix="/api/activity", tags=["activity"])
_EVENT_TICKET_TTL_S = 45


class ActivityPatchRequest(BaseModel):
    pinned: bool


def _json(request: Request, response: Response, payload: dict):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    etag = f'W/"{hashlib.blake2b(raw, digest_size=12).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return payload


@router.get("", dependencies=[Depends(require_token)])
def list_activity(request: Request, response: Response,
                  limit: int = Query(100, ge=1, le=500)):
    return _json(request, response, activity.snapshot(limit))


@router.get("/summary", dependencies=[Depends(require_token)])
def activity_summary(request: Request, response: Response):
    return _json(request, response, activity.summary())


@router.post("/events-ticket", dependencies=[Depends(require_token)])
def mint_activity_event_ticket() -> dict:
    ticket = tickets.mint(
        "activity",
        (),
        ttl=_EVENT_TICKET_TTL_S,
        single_use=True,
    )
    return {"ticket": ticket, "expires_in": _EVENT_TICKET_TTL_S}


def _require_activity_event_ticket(ticket: str = Query("")) -> None:
    if not tickets.validate(ticket, "activity", ()):
        raise HTTPException(
            status_code=401,
            detail="invalid or expired activity event ticket",
        )


@router.get("/events", dependencies=[Depends(_require_activity_event_ticket)])
async def activity_events() -> EventSourceResponse:
    """Push task state transitions instead of waiting for the 10 s fallback poll."""

    async def events() -> AsyncIterator[ServerSentEvent]:
        async with activity.subscribe() as queue:
            yield ServerSentEvent(
                event="ready",
                data=json.dumps(
                    {
                        "generation": activity.generation,
                        "revision": activity.revision,
                    },
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


@router.post("/ack-all", dependencies=[Depends(require_token)])
def ack_all():
    return {"ok": True, "changed": activity.ack(), "summary": activity.summary()}


@router.patch("/{event_id}", dependencies=[Depends(require_token)])
def patch_activity(event_id: str, req: ActivityPatchRequest):
    update = activity.set_pin(event_id, req.pinned)
    if update is None:
        raise HTTPException(status_code=404, detail="activity not found")
    return {"ok": True, **update}


@router.post("/{event_id}/ack", dependencies=[Depends(require_token)])
def ack_event(event_id: str):
    return {"ok": True, "changed": activity.ack(event_id), "summary": activity.summary()}


@router.post("/session/{sid}/ack", dependencies=[Depends(require_token)])
def ack_session(sid: str):
    return {"ok": True, "changed": activity.ack(sid=sid), "summary": activity.summary()}
