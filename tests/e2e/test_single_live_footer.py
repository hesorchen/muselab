"""A live turn shows exactly one Running footer.

The mounted tail row hosts its own footer whenever it is a reply row, or a user
row that carries a status flag (with steering adjustments and runtime
continuations as turn boundaries). The pane-level pending footer covers only
the gap when no mounted row shows one. Both decisions call the same rule on the
same revealed slice, so they can never render together or both disappear.
"""
from __future__ import annotations

import re

import pytest

from .test_chat_render_perf import _app_eval, _login

_SETUP = """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.splice(0, st.messages.length,
  {role: 'user', text: 'go', _turnId: 't1', _turnRoot: true, _k: 'k0'},
  {role: 'tool_use', name: 'Skill', id: 'b', input: {skill: 's'}, _k: 'k1'},
  {role: 'tool_result', id: 'b', text: 'Launching skill: s', _k: 'k2'},
  ...(arg ? [{...arg, _k: 'k3'}] : []),
);
st.streaming = true; st.activeTurnId = 't1';
st._streamStartedAt = Date.now() - 87000; st.streamElapsed = 87;
app.sessions = app.sessions.map(s => s.id === sid
  ? {...s, active: true, turn_active: true} : s);
"""

_FOOTERS = """
await new Promise(r => setTimeout(r, 300));
return [...document.querySelectorAll('.msg-pane:not([aria-hidden]) .turn-footer, .turn-pending-footer')]
  .filter(e => e.offsetParent !== null)
  .map(e => e.classList.contains('turn-pending-footer') ? 'pending' : 'row');
"""


@pytest.mark.parametrize("tail, expected", [
    (None, ["row"]),
    ({"role": "user", "text": "adjust"}, ["pending"]),
    ({"role": "user", "text": "", "turn_status": "running"}, ["row"]),
    ({"role": "user", "text": "x", "_interrupted": True}, ["row"]),
    ({"role": "user", "text": "x", "_failed": True}, ["row"]),
], ids=["reply-tail", "user-tail", "user-tail-status", "user-tail-interrupted",
        "user-tail-failed"])
def test_live_turn_renders_one_running_footer(
    page, backend_url, auth_token, tail, expected,
):
    page.set_viewport_size({"width": 390, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP, tail)
    assert _app_eval(page, _FOOTERS) == expected


# ---------------------------------------------------------------------------
# The pending footer must decide on the SAME revealed slice as the row x-if.
# The row footer is mounted from paneMsgs (the mounted window); the repository
# tail alone is not enough: a steering row, an optimistic prompt, or a frozen
# reading window can sit past the last mounted row.
# ---------------------------------------------------------------------------

_STATE = """
app._pullSessionList = async () => false;
app._requestSessionSync = () => false;
app.loadSession = async () => false;
app._scheduleIdlePreload = () => {};
app._fetchTabUsage = async () => {};
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.splice(0, st.messages.length,
  ...arg.rows.map((row, n) => ({...row, _k: 'rf' + n})));
st.messageRange.visibleStart = arg.start || 0;
st.messageRange.visibleEnd = arg.end === undefined
  ? st.messages.length : arg.end;
st._loaded = true; st.messagesReady = true; st.atBottom = true;
st.compacting = false;
st.streaming = !!arg.streaming;
st.streamElapsed = arg.elapsed || 0;
st._streamStartedAt = arg.startedAt ? Date.now() - arg.startedAt : 0;
st.streamingModel = arg.streamModel || '';
st._runningTurnStartHint = arg.startHint ? Date.now() - arg.startHint : 0;
app.sessions = app.sessions.map(s => s.id === sid
  ? {...s, active: !!arg.live, turn_active: !!arg.live,
     model: arg.sessionModel || s.model}
  : s);
"""

_VISIBLE_FOOTERS = r"""
await new Promise(r => app.$nextTick(r));
await new Promise(r => setTimeout(r, 300));
await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
const meters = (root, sel) => !!root.querySelector(sel)
  && root.querySelector(sel).offsetParent !== null;
return [...document.querySelectorAll(
    '.msg-pane:not([aria-hidden]) .turn-footer, .turn-pending-footer')]
  .filter(el => el.offsetParent !== null)
  .map(el => ({
    kind: el.classList.contains('turn-pending-footer') ? 'pending' : 'row',
    text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
    hasTime: meters(el, '.msg-ts'),
    hasElapsed: meters(el, '.msg-elapsed'),
    hasModel: meters(el, '.turn-model'),
  }));
"""

_COMPLETED_TURN = {
    "role": "assistant", "text": "finished output",
    "ts": 1700000000000, "elapsed": 287, "model": "e2e-finished-model",
    "turn_status": "completed",
}


@pytest.mark.parametrize("viewport", [
    {"width": 390, "height": 844}, {"width": 1440, "height": 900},
], ids=["phone", "desktop"])
def test_steering_row_past_the_mounted_window_keeps_one_footer(
    page, backend_url, auth_token, viewport,
):
    """Case 1 (2026-11-22): a mid-turn steering row lands at the repository
    tail while the mounted window still ends at the streaming row. The row
    footer stays; the pending copy beside it must not."""
    page.set_viewport_size(viewport)
    _login(page, backend_url, auth_token)
    _app_eval(page, "await app._ensureSessionLoaded(app.currentId);")
    _app_eval(page, _STATE, {
        "rows": [
            {"role": "user", "text": "go", "_turnId": "t1", "_turnRoot": True},
            _COMPLETED_TURN,
            {"role": "thinking", "text": "still streaming",
             "ts": 1700000005000, "elapsed": 12, "model": "e2e-finished-model"},
            {"role": "user", "text": "adjust this", "_steeringAdjustment": True,
             "_turnRoot": False},
        ],
        "end": 3,
        "streaming": True, "live": True,
        "startedAt": 457000, "elapsed": 457,
        "streamModel": "e2e-live-model",
    })
    footers = _app_eval(page, _VISIBLE_FOOTERS)
    assert [f["kind"] for f in footers] == ["row"], footers


def test_finished_tail_with_unmounted_prompt_keeps_one_footer(
    page, backend_url, auth_token,
):
    """Case 2 (2026-11-22): a new optimistic prompt sits in the repository but
    the mounted window still ends at the finished turn, whose footer is on
    screen. The stale pending copy must yield to it."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "await app._ensureSessionLoaded(app.currentId);")
    _app_eval(page, _STATE, {
        "rows": [
            {"role": "user", "text": "previous", "_turnRoot": True},
            _COMPLETED_TURN,
            {"role": "user", "text": "next prompt", "_turnRoot": True},
        ],
        "end": 2,
        "streaming": False, "live": True,
        "sessionModel": "e2e-finished-model",
    })
    footers = _app_eval(page, _VISIBLE_FOOTERS)
    assert [f["kind"] for f in footers] == ["row"], footers


def test_server_owned_running_footer_shows_time_elapsed_and_model(
    page, backend_url, auth_token,
):
    """A turn the server owns (started elsewhere / resumed after a restart)
    has no local stream timer. The pending footer must fall back to the same
    clock and session model as the row footer instead of a bare "Running"."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "await app._ensureSessionLoaded(app.currentId);")
    _app_eval(page, _STATE, {
        "rows": [
            {"role": "user", "text": "previous", "_turnRoot": True},
            _COMPLETED_TURN,
            {"role": "user", "text": "next prompt", "_turnRoot": True},
        ],
        "end": 3,
        "streaming": False, "live": True,
        "startHint": 63000,
        "sessionModel": "e2e-footer-model",
    })
    footers = _app_eval(page, _VISIBLE_FOOTERS)
    pending = [f for f in footers if f["kind"] == "pending"]
    assert len(pending) == 1, footers
    entry = pending[0]
    assert entry["hasTime"] and entry["hasElapsed"] and entry["hasModel"], entry
    assert "e2e-footer-model" in entry["text"], entry
    assert re.search(r"·\s*\d+m\d+s", entry["text"]), entry


def test_frozen_window_without_a_row_footer_still_shows_pending(
    page, backend_url, auth_token,
):
    """When the revealed slice has not reached the live tail, no visible row
    footer exists; the pending footer must remain the one live indicator
    instead of both sides suppressing themselves."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "await app._ensureSessionLoaded(app.currentId);")
    _app_eval(page, _STATE, {
        "rows": [
            {"role": "user", "text": "go", "_turnId": "t1", "_turnRoot": True},
            {"role": "assistant", "text": "working"},
            {"role": "thinking", "text": "tail output past the window"},
        ],
        "end": 2,
        "streaming": True, "live": True,
        "startedAt": 5000, "elapsed": 5,
        "streamModel": "e2e-live-model",
    })
    footers = _app_eval(page, _VISIBLE_FOOTERS)
    assert [f["kind"] for f in footers] == ["pending"], footers
