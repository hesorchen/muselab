"""Web Push HTTP surface.

  GET  /api/push/vapid-public          — base64 server pub key (frontend
                                          passes to PushManager.subscribe)
  POST /api/push/subscribe             — body = browser PushSubscription JSON
  POST /api/push/unsubscribe           — body = { endpoint: str }
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from . import push


router = APIRouter(prefix="/api/push", tags=["push"])


# Shape: PushSubscription.toJSON() always has these two top-level keys.
# Pydantic rejects extra rubbish so a buggy / malicious client can't fill
# push_subs.json with arbitrary blobs (the old `sub: dict = Body(...)` ate
# anything). MAX_SUBS_PER_HOST caps how many entries the file can grow to.
class _PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=200)


class _SubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: _PushKeys
    expirationTime: int | None = None   # PushSubscription field; we don't use it


_MAX_SUBS = 64


class _UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


@router.get("/vapid-public", dependencies=[Depends(require_token)])
def vapid_public() -> dict:
    return {"public_key": push.get_vapid_public_key()}


@router.post("/subscribe", dependencies=[Depends(require_token)])
def subscribe(sub: _SubscribeIn) -> dict:
    # Cap total subs to prevent a stale tab calling subscribe() on every
    # focus from growing push_subs.json without bound. The cap check +
    # insert happen atomically inside add_subscription_capped (under the
    # subs lock) — doing it here with two list_subscriptions() reads was
    # both racy (two concurrent subscribes could both slip past the cap)
    # and wasteful (each list_subscriptions reloads the file from disk).
    try:
        push.add_subscription_capped({
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.keys.p256dh, "auth": sub.keys.auth},
        }, _MAX_SUBS)
    except RuntimeError as e:
        raise HTTPException(429, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.post("/unsubscribe", dependencies=[Depends(require_token)])
def unsubscribe(req: _UnsubscribeIn) -> dict:
    push.remove_subscription(req.endpoint)
    return {"ok": True}
