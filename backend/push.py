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
import re
import sys

from .settings import ROOT, atomic_write_text


_DIR = (ROOT / ".muselab") if ROOT else None
_VAPID_FILE = (_DIR / "vapid.json") if _DIR else None
_SUBS_FILE = (_DIR / "push_subs.json") if _DIR else None

_vapid: dict[str, str] | None = None
_subs: dict[str, dict] = {}  # endpoint -> subscription dict


def _gen_vapid_keypair() -> dict[str, str]:
    """Generate a fresh P-256 ECDSA keypair encoded as urlsafe-base64,
    matching the format pywebpush + browsers expect.

    Use TraditionalOpenSSL (SEC1 `-----BEGIN EC PRIVATE KEY-----`), NOT
    PKCS8. py_vapid.Vapid.from_string passes the PEM to
    `serialization.load_pem_private_key`, which (in current versions)
    chokes on PKCS8 EC keys with an opaque "ASN.1 parsing error:
    invalid length" — confirmed reproducible against pywebpush in the
    pinned deps. SEC1 is the older EC-only format every VAPID library
    supports. Public key bytes are identical either way."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_numbers = private_key.public_key().public_numbers()
    # Uncompressed point: 0x04 || X (32 bytes) || Y (32 bytes)
    raw_pub = b"\x04" + public_numbers.x.to_bytes(32, "big") + \
                       public_numbers.y.to_bytes(32, "big")
    public_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")
    return {"private_pem": private_pem, "public_b64": public_b64}


def _migrate_pkcs8_to_sec1(pem: str) -> str | None:
    """One-shot migration for vapid.json written before the SEC1 fix.
    Same key material, different ASN.1 wrapping, so derived public key
    + existing browser subscriptions stay valid — no re-subscribe
    needed. Returns the new PEM, or None if input is already SEC1."""
    if "BEGIN EC PRIVATE KEY" in pem:
        return None
    if "BEGIN PRIVATE KEY" not in pem:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        key = serialization.load_pem_private_key(
            pem.encode("ascii"), password=None, backend=default_backend())
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
    except Exception as e:
        sys.stderr.write(f"[push] PKCS8→SEC1 migration failed: {e}\n")
        return None


def _ensure_vapid() -> dict[str, str]:
    global _vapid
    if _vapid:
        return _vapid
    if _VAPID_FILE and _VAPID_FILE.exists():
        try:
            _vapid = json.loads(_VAPID_FILE.read_text(encoding="utf-8"))
            # Auto-migrate old PKCS8 vapid.json to SEC1 in place. Public
            # key (and therefore subscriptions) are unchanged.
            old_pem = _vapid.get("private_pem", "")
            new_pem = _migrate_pkcs8_to_sec1(old_pem)
            if new_pem and new_pem != old_pem:
                _vapid["private_pem"] = new_pem
                atomic_write_text(_VAPID_FILE,
                                   json.dumps(_vapid, indent=2))
                try:
                    os.chmod(_VAPID_FILE, 0o600)
                except Exception:
                    pass
                sys.stderr.write(
                    "[push] migrated vapid.json from PKCS8 to SEC1; "
                    "existing subscriptions still valid\n")
            return _vapid
        except Exception as e:
            sys.stderr.write(f"[push] vapid.json unreadable, regenerating: {e}\n")
    _vapid = _gen_vapid_keypair()
    if _VAPID_FILE:
        atomic_write_text(_VAPID_FILE, json.dumps(_vapid, indent=2))
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
    atomic_write_text(_SUBS_FILE, json.dumps(_subs, indent=2))


def add_subscription(sub: dict) -> None:
    """`sub` is the JSON shape from PushManager.subscription.toJSON():
       {endpoint: str, keys: {p256dh: str, auth: str}}

    Reload from disk before mutating so we don't overwrite changes a
    parallel worker / out-of-band edit made between our last load and
    this save. _subs is purely a cache of the on-disk file — disk wins."""
    _load_subs()
    endpoint = sub.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    _subs[endpoint] = sub
    _save_subs()


def remove_subscription(endpoint: str) -> bool:
    _load_subs()
    if endpoint in _subs:
        del _subs[endpoint]
        _save_subs()
        return True
    return False


def list_subscriptions() -> list[dict]:
    _load_subs()
    return list(_subs.values())


def send_to_all(title: str, body: str, *, url: str = "/",
                 tag: str = "muselab-task") -> dict:
    """Fire a push payload at every subscription. Dead subs (410/404
    from the push service) are dropped from the store. Returns
    {sent, dropped, errors}."""
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid

    vapid = _ensure_vapid()
    # pywebpush.webpush passes the vapid_private_key string through
    # Vapid.from_string, which fails on standard EC PEMs with an opaque
    # "ASN.1 parsing error". from_pem works fine on the same input. So
    # we instantiate Vapid ourselves and pass the object instead of a
    # string — pywebpush detects the object via `hasattr(_, "sign")`
    # and skips from_string entirely.
    vapid_obj = Vapid.from_pem(vapid["private_pem"].encode("ascii"))
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    # Reload from disk before iterating — another worker may have added /
    # removed subscriptions since our last touch. _subs is just a cache.
    _load_subs()
    sent = 0
    dropped: list[str] = []
    errors: list[str] = []
    for endpoint, sub in list(_subs.items()):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_obj,
                # subject: py_vapid enforces this MUST be `mailto:<email>`
                # (no https URL accepted, even though VAPID spec allows it).
                # Apple's push.apple.com further rejects mailtos whose TLD
                # is non-routable (`.local`, `.test`) with 403 BadJwtToken.
                # `.dev` is a real ICANN TLD, accepted by both Apple + FCM.
                # Self-hosters can override via MUSELAB_VAPID_SUBJECT in .env.
                vapid_claims={"sub": os.environ.get(
                    "MUSELAB_VAPID_SUBJECT", "mailto:noreply@muselab.dev")},
                ttl=24 * 3600,
            )
            sent += 1
        except WebPushException as e:
            # CAREFUL: `if e.response` is a trap — requests.Response.__bool__
            # returns self.ok, which is False for ANY 4xx/5xx. That's
            # precisely the 404/410 "dead subscription" responses we need to
            # detect, so the old truthiness gate short-circuited to None and
            # dead subs were never dropped (they re-failed on every send).
            # Use an explicit `is not None`, with a regex fallback off the
            # message string for the rare case .response is genuinely absent.
            resp = e.response
            code = getattr(resp, "status_code", None) if resp is not None else None
            if code is None:
                m = re.search(r"\b(404|410)\b", str(e))
                if m:
                    code = int(m.group(1))
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
