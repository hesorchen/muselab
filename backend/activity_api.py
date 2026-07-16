"""Authenticated activity-center API."""

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request, Response

from .activity import activity
from .auth import require_token

router = APIRouter(prefix="/api/activity", tags=["activity"])


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
    return _json(request, response,
                 {"events": activity.list(limit), "summary": activity.summary()})


@router.get("/summary", dependencies=[Depends(require_token)])
def activity_summary(request: Request, response: Response):
    return _json(request, response, activity.summary())


@router.post("/ack-all", dependencies=[Depends(require_token)])
def ack_all():
    return {"ok": True, "changed": activity.ack(), "summary": activity.summary()}


@router.post("/{event_id}/ack", dependencies=[Depends(require_token)])
def ack_event(event_id: str):
    return {"ok": True, "changed": activity.ack(event_id), "summary": activity.summary()}


@router.post("/session/{sid}/ack", dependencies=[Depends(require_token)])
def ack_session(sid: str):
    return {"ok": True, "changed": activity.ack(sid=sid), "summary": activity.summary()}
