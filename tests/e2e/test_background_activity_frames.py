"""Turnless subagent/task frames update the session without making it busy."""
from __future__ import annotations

import json

from .test_chat_render_perf import _app_eval, _login

_SEED = """
const sid = app.currentId, st = app._ensureTabState(sid);
st.messages.push({role: 'tool_use', id: 'toolu-agent', name: 'Agent',
                  task_status: {task_id: 'task-1', state: 'running'}});
return sid;
"""


def _frame(sid, kind, data):
    return json.dumps({"session_id": sid, "type": kind, "data": json.dumps(data)})


def test_background_activity_updates_cards_without_streaming(
    page, backend_url, auth_token,
):
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, _SEED)
    frames = [
        _frame(sid, "task_progress", {
            "task_id": "task-1", "tool_use_id": "toolu-agent",
            "last_tool_name": "Bash"}),
        _frame(sid, "subagent_delta", {
            "session_id": sid, "parent_tool_use_id": "toolu-agent",
            "block_id": "b1", "kind": "assistant", "delta": "child ", "offset": 0}),
        _frame(sid, "subagent_delta", {
            "session_id": sid, "parent_tool_use_id": "toolu-agent",
            "block_id": "b1", "kind": "assistant", "delta": "text", "offset": 6}),
    ]
    result = _app_eval(page, """
const frames = %s;
for (const f of frames) app._handleBackgroundActivity(JSON.parse(f));
const sid = app.currentId, st = app.tabState[sid];
const card = st.messages.find(m => m.id === 'toolu-agent');
const thread = st.subagentThreads.find(t => t.parent_tool_use_id === 'toolu-agent');
return {
  tool: card.task_status.last_tool_name,
  state: card.task_status.state,
  text: thread && thread.blocks[0] && thread.blocks[0].text,
  streaming: app.isTabStreaming(sid),
};
""" % json.dumps(frames))
    assert result == {
        "tool": "Bash", "state": "running", "text": "child text", "streaming": False,
    }

    done = _app_eval(page, """
const sid = app.currentId, st = app.tabState[sid];
app._handleBackgroundActivity({session_id: sid, type: 'task_notification',
  data: JSON.stringify({task_id: 'task-1', tool_use_id: 'toolu-agent',
                        status: 'completed', summary: 'ok',
                        background_tasks_pending: 0})});
return st.messages.find(m => m.id === 'toolu-agent').task_status.state;
""")
    assert done == "completed"
