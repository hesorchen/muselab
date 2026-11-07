"""Live "Running" status has one source: the backend's main-loop flag.

It belongs to the conversation tail whichever prompt started the turn, and
survives history reloads. Every other row shows only its recorded outcome.
"""
from __future__ import annotations

import pytest

from .test_chat_render_perf import _app_eval, _login

_SETUP = """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.splice(0, st.messages.length,
  {role: 'user', text: 'origin prompt', _turnId: 'turn-user', ts: 1},
  {role: 'assistant', text: 'first answer', ts: 2, turn_status: 'completed'},
);
return sid;
"""

_STATUS = """
const st = app.tabState[app.currentId];
// User rows never host a turn footer (index.html); report reply rows only.
return st.messages.filter(m => m.role !== 'user')
  .map(m => app.turnFooterStatus(m, st));
"""


def _set_live(page, *, streaming, turn_active=False, **pane):
    _app_eval(page, """
const sid = app.currentId, st = app.tabState[sid];
Object.assign(st, arg.pane);
st.streaming = arg.streaming;
app.sessions = app.sessions.map(s => s.id === sid
  ? {...s, active: arg.turn_active || arg.streaming, turn_active: arg.turn_active}
  : s);
""", {"streaming": streaming, "turn_active": turn_active, "pane": pane})


def _append(page, row):
    _app_eval(page, "app.tabState[app.currentId].messages.push(arg);", row)


@pytest.mark.parametrize("pane", [
    {"activeTurnId": "turn-user", "parentTurnId": ""},
    {"activeTurnId": "turn-reaction", "parentTurnId": "turn-user"},
    {"activeTurnId": "turn-reaction-2", "parentTurnId": "turn-reaction-1"},
], ids=["user-turn", "cli-reaction", "chained-reaction"])
def test_tail_is_running_for_any_prompt_and_history_rows_keep_outcome(
    page, backend_url, auth_token, pane,
):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _append(page, {"role": "thinking", "text": "working"})
    _set_live(page, streaming=True, **pane)
    assert _app_eval(page, _STATUS) == ["completed", "running"]
    app_delivery = _app_eval(
        page, "app.busySendMode = 'adjust'; return app._busySendDelivery(app.currentId);")
    assert app_delivery == "adjust"


def test_reloaded_canonical_tail_with_ts_stays_running(page, backend_url, auth_token):
    """A quiet history reload replaces live rows with persisted ones (ts set)."""
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _append(page, {"role": "thinking", "text": "persisted", "ts": 3})
    _set_live(page, streaming=True, activeTurnId="turn-reaction",
              parentTurnId="turn-reaction-0")
    assert _app_eval(page, _STATUS)[-1] == "running"


def test_reaction_before_first_output_shows_running_on_previous_tail(
    page, backend_url, auth_token,
):
    """The CLI dequeued a notification; nothing new is rendered yet."""
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _set_live(page, streaming=True, activeTurnId="turn-reaction",
              parentTurnId="turn-user")
    assert _app_eval(page, _STATUS) == ["running"]


def test_turn_started_elsewhere_is_running_from_server_flag(
    page, backend_url, auth_token,
):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _set_live(page, streaming=False, turn_active=True)
    assert _app_eval(page, _STATUS) == ["running"]


def test_only_background_work_shows_recorded_outcome(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _set_live(page, streaming=False, turn_active=False, backgroundActive=True,
              backgroundTaskCount=2)
    assert _app_eval(page, _STATUS) == ["completed"]


def test_older_row_is_never_live(page, backend_url, auth_token):
    """Only the tail can be running; a stored "running" elsewhere is history."""
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _app_eval(page, """
const st = app.tabState[app.currentId];
st.messages[st.messages.length - 1].turn_status = 'running';
st.messages.push({role: 'user', text: 'next', _turnId: 'turn-next', ts: 5},
                 {role: 'assistant', text: 'done', ts: 6, turn_status: 'completed'});
""")
    _set_live(page, streaming=False)
    # Its durable ts proves the turn closed: the outcome, never "running".
    assert _app_eval(page, _STATUS) == ["completed", "completed"]


def test_compacting_suppresses_running(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP)
    _set_live(page, streaming=True, compacting=True)
    assert _app_eval(page, _STATUS)[-1] == "completed"
