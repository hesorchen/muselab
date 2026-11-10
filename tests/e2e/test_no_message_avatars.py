"""Transcript rows carry no avatars; both sides use the full row width."""
from __future__ import annotations

import pytest

from .test_chat_render_perf import _app_eval, _login

_SETUP = """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.splice(0, st.messages.length,
  {role: 'user', text: arg.long, _turnId: 't1', ts: 1, _k: 'k0'},
  {role: 'assistant', text: arg.long, ts: 2, turn_status: 'completed', _k: 'k1'},
);
Object.assign(st.messageRange, {visibleStart: 0, visibleEnd: 2, offset: 0, total: 2});
await new Promise(r => setTimeout(r, 400));
"""

_GEOMETRY = """
const pane = document.querySelector('.msg-pane:not([aria-hidden])');
const box = pane.getBoundingClientRect();
const user = pane.querySelector('.msg.user .bubble').getBoundingClientRect();
const muse = pane.querySelector('.msg.assistant .bubble').getBoundingClientRect();
return {
  avatars: document.querySelectorAll('.msg-avatar').length,
  pane: [box.left, box.right], user: [user.left, user.right],
  muse: [muse.left, muse.right],
};
"""


@pytest.mark.parametrize("width", [390, 1440])
def test_rows_have_no_avatars_and_fill_the_row(page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP, {"long": "长消息 " * 200})
    g = _app_eval(page, _GEOMETRY)
    assert g["avatars"] == 0
    pane_left, pane_right = g["pane"]
    pane_w = pane_right - pane_left
    # Muse starts at the row edge and spans it.
    assert g["muse"][0] - pane_left < 24
    assert g["muse"][1] - g["muse"][0] > pane_w * 0.9
    # The user bubble hugs the right edge and keeps a left gap.
    assert pane_right - g["user"][1] < 24
    assert 0.75 * pane_w < g["user"][1] - g["user"][0] <= 0.86 * pane_w
