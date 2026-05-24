"""User-presence heartbeat tracking — gates Web Push so a notification
doesn't fan out to the phone while the user is actively at the desktop.

Why this exists:
  Each browser/PWA's service worker can only see ITS OWN device's window
  visibility. Service worker on the phone has no way to know the desktop
  tab is in foreground — so even with the per-device SW visibility check
  in sw.js, the user would still hear their phone buzz while typing on
  the laptop. We fix that by having the frontend POST /api/presence every
  ~15s whenever the page is visible; the chat-done push step then asks
  "did any device check in within the last N seconds?" and silently skips
  the push if so.

This is a single-user app (one MUSELAB_TOKEN, one archive), so a single
shared timestamp is enough. No per-device-id bookkeeping needed.

Trade-off: if you're idle but the muselab tab is in foreground (visible
but not interacting), the heartbeat keeps firing and the phone stays
quiet. We deem this acceptable — the reply is one glance away on the
desktop anyway. Once you minimize / switch away, the heartbeat stops and
within the grace window the phone starts ringing again.
"""

from __future__ import annotations

import time

# Grace window: how long after the most recent "visible" heartbeat we
# still consider the user "actively at a device". 30s gives a couple of
# missed heartbeats of slack (frontend sends every 15s) without leaving
# the user without their phone too long after they walk away.
GRACE_SECONDS: float = 30.0

# Module-global timestamp — fine for the single-user model. If muselab
# ever goes multi-user, this becomes {user_id: ts} but the call sites
# below should largely keep working with the same shape.
_last_seen_ts: float = 0.0


def mark_seen() -> None:
    """Called by the /api/presence endpoint each time the frontend
    reports the page is visible."""
    global _last_seen_ts
    _last_seen_ts = time.time()


def recently_active(grace: float = GRACE_SECONDS) -> bool:
    """True if any device has reported visibility within `grace` seconds.
    Push-gate uses this to skip fan-out when the user is at a device."""
    if _last_seen_ts <= 0:
        return False  # never reported — treat as not active
    return (time.time() - _last_seen_ts) < grace


def last_seen_age() -> float | None:
    """How many seconds since the last heartbeat. None if never reported.
    Used by /api/presence response so the frontend can self-diagnose."""
    if _last_seen_ts <= 0:
        return None
    return time.time() - _last_seen_ts
