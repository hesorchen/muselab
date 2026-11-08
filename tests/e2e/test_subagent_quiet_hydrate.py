"""Quiet history reloads refetch Subagent transcripts only when needed.

Background task progress bumps the session revision every few seconds, and
each bump triggers a quiet history reload. Refetching every Subagent
transcript on each reload moved tens of MB per call, so the reload refetches
at once only when the Agent cards changed, and otherwise on a bounded cadence.
"""
from __future__ import annotations

from .test_chat_render_perf import _app_eval, _login

_SETUP = """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.splice(0, st.messages.length,
  {role: 'user', text: 'go', ts: 1},
  {role: 'tool_use', name: 'Agent', id: 'toolu_a', input: {}, ts: 2,
   task_status: arg.state ? {task_id: 't1', state: arg.state} : null},
);
if (arg.result) st.messages.push({role: 'tool_result', id: 'toolu_a', text: 'ok', ts: 3});
st.subagentsLoaded = false;
st.subagentsHydratedAt = 0;
st._subagentCardsKey = undefined;
window.__subagentFetches = 0;
app._fetchWithDeadline = async (url) => {
  if (String(url).includes('/subagents')) window.__subagentFetches += 1;
  return new Response(JSON.stringify({session_id: sid, threads: []}),
                      {status: 200, headers: {'Content-Type': 'application/json'}});
};
"""

_QUIET_LOAD = """
const sid = app.currentId, st = app.tabState[sid];
app._hydrateSubagentsAfterQuietLoad(sid, st);
await new Promise(r => setTimeout(r, 50));
return window.__subagentFetches;
"""


def _age(page, ms):
    _app_eval(page, "app.tabState[app.currentId].subagentsHydratedAt -= arg;", ms)


def test_settled_cards_skip_refetch_until_cards_change(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP, {"state": "", "result": True})
    assert _app_eval(page, _QUIET_LOAD) == 1  # first load always fetches
    assert _app_eval(page, _QUIET_LOAD) == 1  # unchanged and settled: skip
    _age(page, 60_000)
    assert _app_eval(page, _QUIET_LOAD) == 1  # still inside the 5 min cadence
    _app_eval(page, """
app.tabState[app.currentId].messages.push(
  {role: 'tool_use', name: 'Agent', id: 'toolu_b', input: {}, ts: 4});
""")
    assert _app_eval(page, _QUIET_LOAD) == 2  # a new card fetches at once
    _app_eval(page, """
app.tabState[app.currentId].messages.push({role: 'tool_result', id: 'toolu_b', ts: 5});
""")
    assert _app_eval(page, _QUIET_LOAD) == 3  # its result fetches at once
    _age(page, 301_000)
    assert _app_eval(page, _QUIET_LOAD) == 4  # bounded convergence cadence


def test_running_background_subagent_refetches_every_30s(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SETUP, {"state": "running", "result": True})
    assert _app_eval(page, _QUIET_LOAD) == 1
    assert _app_eval(page, _QUIET_LOAD) == 1
    _age(page, 31_000)
    assert _app_eval(page, _QUIET_LOAD) == 2
    _app_eval(page, """
app.tabState[app.currentId].messages[1].task_status =
  {task_id: 't1', state: 'completed'};
""")
    assert _app_eval(page, _QUIET_LOAD) == 3  # settlement fetches at once
