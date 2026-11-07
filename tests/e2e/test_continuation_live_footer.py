"""The CLI's own reaction keeps a live "Running" footer and accepts adjust."""
from __future__ import annotations

import pytest

from .test_chat_render_perf import _app_eval, _login


@pytest.mark.parametrize("parent", ["turn-user", "turn-earlier-continuation"])
def test_continuation_segment_shows_running_and_allows_adjust(
    page, backend_url, auth_token, parent,
):
    """A chained reaction is parented on the previous reaction (2026-11-07)."""
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.push(
  {role: 'user', text: 'origin prompt', _turnId: 'turn-user', ts: 1},
  {role: 'assistant', text: 'first answer', ts: 2, turn_status: 'completed'},
  {role: 'assistant', text: 'reacting to the task'},
);
// A live continuation: its own turn id, parented on the user turn or on
// an earlier continuation when reactions chain.
st.streaming = true;
st.activeTurnId = 'turn-continuation';
st.parentTurnId = arg;
st.backgroundActive = true;
const live = st.messages[st.messages.length - 1];
app.busySendMode = 'adjust';
return {
  footer: app.turnFooterStatus(live, st),
  delivery: app._busySendDelivery(sid),
};
""", parent)
    assert result == {"footer": "running", "delivery": "adjust"}
