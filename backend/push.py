"""Web Push — VAPID-signed notifications to subscribed browsers / PWAs.

Two persistent bits live on disk:

  <archive>/.muselab/vapid.json       generated once at startup; the
                                       keypair muselab uses to sign every
                                       push so push services accept it
  <archive>/.muselab/push_subs.json   active subscriptions (one per
                                       device, identified by endpoint)

Both are JSON, neither has to migrate between muselab versions: regenerate
vapid.json => everyone re-subscribes. Drop push_subs.json => everyone
loses their subscription but the keypair is intact.

Public API:
  get_vapid_public_key()       — base64 url-safe, ship to the frontend
  add_subscription(sub)        — persist after pushManager.subscribe()
  remove_subscription(endp)    — called on user opt-out
  send_to_all(title, body, …)  — fire-and-forget; iterates all subs,
                                 drops dead ones (410 / 404 from the
                                 push service) automatically
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from .settings import ROOT


_DIR = (ROOT / ".muselab") if ROOT else None
_VAPID_FILE = (_DIR / "vapid.json") if _DIR else None
_SUBS_FILE = (_DIR / "push_subs.json") if _DIR else None

_vapid: dict[str, str] | None = None
_subs: dict[str, dict] = {}  # endpoint -> subscription dict


def _gen_vapid_keypair() -> dict[str, str]:
    """Generate a fresh P-256 ECDSA keypair encoded as urlsafe-base64,
    matching the format pywebpush + browsers expect."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_numbers = private_key.public_key().public_numbers()
    # Uncompressed point: 0x04 || X (32 bytes) || Y (32 bytes)
    raw_pub = b"\x04" + public_numbers.x.to_bytes(32, "big") + \
                       public_numbers.y.to_bytes(32, "big")
    public_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")
    return {"private_pem": private_pem, "public_b64": public_b64}


def _ensure_vapid() -> dict[str, str]:
    global _vapid
    if _vapid:
        return _vapid
    if _VAPID_FILE and _VAPID_FILE.exists():
        try:
            _vapid = json.loads(_VAPID_FILE.read_text(encoding="utf-8"))
            return _vapid
        except Exception as e:
            sys.stderr.write(f"[push] vapid.json unreadable, regenerating: {e}\n")
    _vapid = _gen_vapid_keypair()
    if _VAPID_FILE:
        _VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VAPID_FILE.write_text(
            json.dumps(_vapid, indent=2), encoding="utf-8",
        )
        try:
            os.chmod(_VAPID_FILE, 0o600)
        except Exception:
            pass
    return _vapid


def get_vapid_public_key() -> str:
    return _ensure_vapid()["public_b64"]


def _load_subs() -> None:
    global _subs
    if not _SUBS_FILE or not _SUBS_FILE.exists():
        return
    try:
        d = json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _subs = d
    except Exception:
        pass


def _save_subs() -> None:
    if not _SUBS_FILE:
        return
    _SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SUBS_FILE.write_text(json.dumps(_subs, indent=2), encoding="utf-8")


def add_subscription(sub: dict) -> None:
    """`sub` is the JSON shape from PushManager.subscription.toJSON():
       {endpoint: str, keys: {p256dh: str, auth: str}}"""
    endpoint = sub.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    _subs[endpoint] = sub
    _save_subs()


def remove_subscription(endpoint: str) -> bool:
    if endpoint in _subs:
        del _subs[endpoint]
        _save_subs()
        return True
    return False


def list_subscriptions() -> list[dict]:
    return list(_subs.values())


def send_to_all(title: str, body: str, *, url: str = "/",
                 tag: str = "muselab-task") -> dict:
    """Fire a push payload at every subscription. Dead subs (410/404
    from the push service) are dropped from the store. Returns
    {sent, dropped, errors}."""
    from pywebpush import webpush, WebPushException

    vapid = _ensure_vapid()
    private_pem = vapid["private_pem"]
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    dropped: list[str] = []
    errors: list[str] = []
    for endpoint, sub in list(_subs.items()):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_pem,
                # subject: an email or https URL — push services need it
                # to know who's sending. The literal value isn't validated.
                vapid_claims={"sub": "mailto:noreply@muselab.local"},
                ttl=24 * 3600,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None) if e.response else None
            if code in (404, 410):
                # Subscription is dead (user uninstalled / cleared) — drop it.
                del _subs[endpoint]
                dropped.append(endpoint)
            else:
                errors.append(f"{code}: {e}")
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    if dropped:
        _save_subs()
    return {"sent": sent, "dropped": len(dropped), "errors": errors}


def init() -> None:
    """Idempotent — main.py startup hook calls this; we load subs and
    eagerly generate VAPID so the frontend's first /api/push/vapid-public
    call doesn't have to wait on key generation."""
    _ensure_vapid()
    _load_subs()
