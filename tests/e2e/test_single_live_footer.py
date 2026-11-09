"""A live turn shows exactly one Running footer.

The tail row hosts its own footer whenever it is a reply row, or a user row
that carries a status flag. The pane-level pending footer covers only the gap
before any such row exists, so the two can never render together.
"""
from __future__ import annotations

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
