"""Inherited Agent details keep updating while the successor is in use."""
import json

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import (
    _app_eval, _assert_no_browser_errors, _capture_browser_errors,
    _login, _route_windowed_session,
)


@pytest.mark.parametrize("width", [1440, 390])
def test_inherited_timeline_updates_without_replacing_active_child(
    page, backend_url, auth_token, width,
):
    page.set_viewport_size({"width": width, "height": 900})
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    page.wait_for_function("""() => {
      const a = document.querySelector('#app')._x_dataStack[0];
      return !a.tabState[a.currentId].subagentsLoading;
    }""")
    sid = _app_eval(page, "return app.currentId;")
    source = "00000000-0000-4000-8000-000000000123"
    blocks = [{
        "session_id": source, "parent_tool_use_id": "inherited-agent-call",
        "role": "assistant", "text": "INHERITED_STARTED", "block_id": "started",
    }]
    requests = []

    def timeline(route):
        requests.append(route.request.url)
        route.fulfill(content_type="application/json", body=json.dumps({
            "session_id": sid, "threads": [{
                "session_id": source, "agent_id": "ancestor-agent",
                "parent_tool_use_id": "inherited-agent-call", "orphaned": False,
                "blocks": blocks,
            }],
        }))

    page.route(f"**/api/chat/sessions/{sid}/subagents", timeline)
    page.route(f"**/api/chat/sessions/{source}/active", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({
            "active": True, "runtime_background_tasks_pending": 1,
            "runtime_continuation_pending": False, "runtime_ui_revision": "",
        }),
    ))
    _route_windowed_session(page, sid, [{
        "role": "tool_use", "name": "Agent", "id": "inherited-agent-call",
        "uuid": "parent-card", "task": {"description": "Synthetic inherited task"},
    }])
    _app_eval(page, "await app.loadSession(arg, {probeActive: false});", sid)
    details = page.locator(".msg-pane:visible .subagent-timeline:visible")
    expect(details).to_be_visible()
    details.locator("summary").click()
    expect(details).to_contain_text("INHERITED_STARTED")
    before = len(requests)

    _app_eval(page, """
      const st = app.tabState[arg.sid];
      st.streaming = true;
      st.sessionSync.inheritedSourceSid = arg.source;
      st.sessionSync.inheritedTicksLeft = 10;
      await app._pollInheritedTasks(arg.sid, st, {sourceSid: arg.source});
    """, {"sid": sid, "source": source})
    assert len(requests) == before, "ordinary task polls must not reread all timelines"

    blocks.append({
        "session_id": source, "parent_tool_use_id": "inherited-agent-call",
        "role": "tool_result", "text": "INHERITED_READ_FINISHED", "block_id": "read-result",
    })
    _app_eval(page, """
      const st = app.tabState[arg.sid];
      st.subagentsHydratedAt = Date.now() - 5001;
      await app._pollInheritedTasks(arg.sid, st, {sourceSid: arg.source});
    """, {"sid": sid, "source": source})
    expect(details).to_contain_text("INHERITED_READ_FINISHED")
    assert _app_eval(page, "return app.tabState[arg].streaming;", sid) is True
    assert _app_eval(page, "return app.tabState[arg].messages.length;", sid) == 1
    assert len(requests) == before + 1
    _assert_no_browser_errors(page, errors)
