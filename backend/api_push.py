"""Web Push HTTP surface.

  GET  /api/push/vapid-public          — base64 server pub key (frontend
                                          passes to PushManager.subscribe)
  POST /api/push/subscribe             — body = browser PushSubscription JSON
  POST /api/push/unsubscribe           — body = { endpoint: str }
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from .auth import require_token
from . import push


router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-public", dependencies=[Depends(require_token)])
def vapid_public() -> dict:
    return {"public_key": push.get_vapid_public_key()}


@router.post("/subscribe", dependencies=[Depends(require_token)])
def subscribe(sub: dict = Body(...)) -> dict:
    try:
        push.add_subscription(sub)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.post("/unsubscribe", dependencies=[Depends(require_token)])
def unsubscribe(req: dict = Body(...)) -> dict:
    endpoint = (req or {}).get("endpoint", "")
    if not endpoint:
        raise HTTPException(400, "endpoint required")
    push.remove_subscription(endpoint)
    return {"ok": True}
