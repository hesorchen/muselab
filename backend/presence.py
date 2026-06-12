"""User-presence tracking — gates Web Push so a notification doesn't
fan out to the phone while the user is actively at the desktop.

Why this exists:
  Each browser/PWA's service worker can only see ITS OWN device's window
  visibility. Service worker on the phone has no way to know the desktop
  tab is in foreground — so even with the per-device SW visibility check
  in sw.js, the user would still hear their phone buzz while typing on
  the laptop. We fix that by having the frontend POST /api/presence
  whenever visibility changes (plus a 15s keep-alive while visible); the
  chat-done push step then asks "is any device foregrounded right now?"
  and silently skips the push if so.

v2 (2026-06-12): per-device records with an explicit hidden signal.
  v1 kept a single shared timestamp and could only let it EXPIRE — after
  backgrounding, the server had to wait out the full GRACE_SECONDS before
  daring to push, so any turn that finished within ~30s of backgrounding
  was silently swallowed (user report: "切后台了还是没推送"). Now the
  frontend reports visible:false the moment the page hides, which
  immediately disqualifies that device from suppressing pushes. The
  grace window REMAINS, but demoted to a safety net for the one case the
  hidden signal can't cover: browser process killed / network dropped
  before the beacon got out — a stale visible record then expires after
  GRACE_SECONDS instead of suppressing pushes forever.

This is a single-user app (one MUSELAB_TOKEN, one archive), so a small
device_id-keyed dict is enough — device_id is a random UUID the frontend
mints once into localStorage, purely to tell records apart; it carries
no auth meaning. Old clients that POST without a body land on the
"default" id with visible=True — exactly the v1 behavior.

Thread-safety note: callers run on the event loop (chat.py) and in
FastAPI's threadpool (the sync /api/presence handler). All mutations are
single dict-item assignments, atomic under the GIL; worst case a race
reads a one-heartbeat-stale value, which the grace window absorbs. No
lock needed.
"""

from __future__ import annotations

import time

# Grace window: how long a `visible` report stays trusted without a
# refresh. Frontend keep-alives fire every 15s while visible, so 30s
# tolerates one dropped heartbeat. This is NOT the push delay after
# backgrounding anymore (the explicit hidden signal handles that
# instantly) — it only bounds how long a killed-browser's stale
# "visible" record can keep suppressing pushes.
GRACE_SECONDS: float = 30.0

# device_id -> (last_report_ts, visible). Bounded by the user's device
# count in practice; pruned opportunistically in mark_seen.
_devices: dict[str, tuple[float, bool]] = {}

# Entries untouched for this long are dropped — dead device ids (cleared
# localStorage, retired phone) shouldn't accumulate forever.
_PRUNE_SECONDS: float = 24 * 3600.0


def mark_seen(device_id: str = "default", visible: bool = True) -> None:
    """Called by /api/presence on every frontend report.

    visible=True  — page is foregrounded (init / 15s keep-alive / refocus)
    visible=False — page just hid (visibilitychange→hidden / pagehide);
                    immediately releases this device's push suppression.
    """
    now = time.time()
    _devices[device_id] = (now, visible)
    if len(_devices) > 8:  # prune only when the dict has visibly grown
        for k, (ts, _vis) in list(_devices.items()):
            if now - ts > _PRUNE_SECONDS:
                _devices.pop(k, None)


def recently_active(grace: float = GRACE_SECONDS) -> bool:
    """True if any device is believed to be foregrounded right now:
    its last report said visible AND arrived within `grace` seconds.
    Push-gate uses this to skip fan-out when the user is at a device."""
    now = time.time()
    return any(vis and (now - ts) < grace for ts, vis in _devices.values())


def last_seen_age() -> float | None:
    """Seconds since the freshest still-`visible` report. None when no
    device currently claims visibility (all hidden, or never reported).
    Used by the push-skip log line and the /api/presence response so
    the frontend can self-diagnose."""
    now = time.time()
    ages = [now - ts for ts, vis in _devices.values() if vis]
    return min(ages) if ages else None
