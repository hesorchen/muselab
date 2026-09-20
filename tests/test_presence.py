"""Presence heartbeats recover after a browser disappears without a hidden beacon."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('gap, active', [(29.0, False), (30.0, True), (120.0, True)])
def test_expired_heartbeat_starts_new_visible_streak(monkeypatch, gap, active):
    from backend import presence

    clock = [0.0]
    monkeypatch.setattr(presence, '_devices', {})
    monkeypatch.setattr(presence, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setenv('MUSELAB_PRESENCE_MAX_VISIBLE_SEC', '60')
    for timestamp in (0.0, 15.0, 30.0, 45.0, 60.0):
        clock[0] = timestamp
        presence.mark_seen('device')
    # Uninterrupted heartbeats still trigger the parked-tab guard.
    assert not presence.recently_active()
    assert presence.last_seen_age() is None

    # A killed browser cannot send visible=False before it is reopened.
    clock[0] += gap
    presence.mark_seen('device')
    assert presence.recently_active() is active
    assert presence.last_seen_age() == (0.0 if active else None)
