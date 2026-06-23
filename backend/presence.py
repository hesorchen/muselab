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

import os
import time

# Grace window: how long a `visible` report stays trusted without a
# refresh. Frontend keep-alives fire every 15s while visible, so 30s
# tolerates one dropped heartbeat. This is NOT the push delay after
# backgrounding anymore (the explicit hidden signal handles that
# instantly) — it only bounds how long a killed-browser's stale
# "visible" record can keep suppressing pushes.
GRACE_SECONDS: float = 30.0


def _max_visible_streak() -> float:
    """Phantom-tab guard (2026-06-23). A device that reports `visible`
    every 15s but NEVER sends a `hidden` signal is almost certainly a
    desktop tab left foregrounded on an always-on machine — not someone
    actually watching. Because recently_active() is GLOBAL (any visible
    device suppresses pushes to ALL devices, including the phone), one
    such parked tab silently silences phone notifications forever.

    So we stop trusting a continuous `visible` streak once it exceeds
    this many seconds: past that, the device no longer suppresses
    pushes. A real desktop session almost always breaks visibility
    within the window — switching tabs, locking the screen, or
    minimizing all fire visibilitychange→hidden, which resets the
    streak. Only a genuinely parked tab runs past it.

    Default 30 min; override with MUSELAB_PRESENCE_MAX_VISIBLE_SEC
    (<=0 disables the guard, restoring the old always-trust behavior)."""
    try:
        v = float(os.environ.get("MUSELAB_PRESENCE_MAX_VISIBLE_SEC", "1800"))
    except (TypeError, ValueError):
        return 1800.0
    return v


# device_id -> (last_report_ts, visible, visible_since). visible_since is
# the start of the CURRENT uninterrupted visible streak (None when the
# device's last report was hidden), used by the phantom-tab guard above.
# Bounded by the user's device count in practice; pruned opportunistically
# in mark_seen.
_devices: dict[str, tuple[float, bool, float | None]] = {}

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
    prev = _devices.get(device_id)
    if visible:
        # Continue the streak if the device was already visible; otherwise
        # (new device, or coming back from hidden) start a fresh streak now.
        if prev is not None and prev[1] and prev[2] is not None:
            visible_since = prev[2]
        else:
            visible_since = now
    else:
        visible_since = None
    _devices[device_id] = (now, visible, visible_since)
    if len(_devices) > 8:  # prune only when the dict has visibly grown
        for k, (ts, _vis, _vs) in list(_devices.items()):
            if now - ts > _PRUNE_SECONDS:
                _devices.pop(k, None)


def recently_active(grace: float = GRACE_SECONDS) -> bool:
    """True if any device is believed to be foregrounded right now:
    its last report said visible AND arrived within `grace` seconds.
    Push-gate uses this to skip fan-out when the user is at a device.

    Phantom-tab guard: a device whose continuous `visible` streak has run
    past _max_visible_streak() is treated as a parked tab and no longer
    suppresses pushes — see _max_visible_streak() for the rationale."""
    now = time.time()
    max_streak = _max_visible_streak()
    for ts, vis, vsince in _devices.values():
        if not vis or (now - ts) >= grace:
            continue
        if max_streak > 0 and vsince is not None \
                and (now - vsince) >= max_streak:
            continue  # parked tab — don't let it gate pushes
        return True
    return False


def last_seen_age() -> float | None:
    """Seconds since the freshest report from a device that currently
    suppresses pushes (visible, within grace, and not a parked tab). None
    when no such device exists. Mirrors recently_active()'s gate so the
    push-skip log line and /api/presence response reflect the device that
    actually caused (or would cause) suppression."""
    now = time.time()
    max_streak = _max_visible_streak()
    ages = []
    for ts, vis, vsince in _devices.values():
        if not vis or (now - ts) >= GRACE_SECONDS:
            continue
        if max_streak > 0 and vsince is not None \
                and (now - vsince) >= max_streak:
            continue
        ages.append(now - ts)
    return min(ages) if ages else None
