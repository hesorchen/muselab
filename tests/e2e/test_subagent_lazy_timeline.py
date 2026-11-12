"""Agent timelines refresh as a summary and load bodies when opened.

A long session's full Subagent timelines reached tens of MB per refresh.
The background refresh now fetches every row without bodies; opening a card
fetches that card's subtree, and an open card keeps its bodies through later
summary refreshes, loading any rows those refreshes add.
"""
import json

from playwright.sync_api import expect

from .test_chat_render_perf import (
    _app_eval, _assert_no_browser_errors, _capture_browser_errors,
    _login, _route_windowed_session,
)


def _settled_login(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    # The boot hydrate must finish before the routes below take over.
    page.wait_for_function("""() => {
      const a = document.querySelector('#app')._x_dataStack[0];
      return !a.tabState[a.currentId].subagentsLoading;
    }""")


def _block(block_id, role, **extra):
    return {"session_id": "s", "parent_tool_use_id": "toolu_card", "role": role,
            "block_id": block_id, "message_uuid": block_id, **extra}


def test_opened_timeline_loads_bodies_and_keeps_them(page, backend_url, auth_token):
    errors = _capture_browser_errors(page)
    _settled_login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    blocks = [_block("b1", "assistant", text="FIRST_BODY"),
              _block("b2", "tool_use", name="Bash", id="t1", summary="ls -la")]
    calls = []

    def timeline(route):
        url = route.request.url
        calls.append("parent" if "parent=toolu_card" in url
                     else "summary" if "view=summary" in url else "full")
        rows = [dict(b) for b in blocks]
        if "view=summary" in url:
            rows = [{**{k: v for k, v in b.items() if k != "text"}, "stub": True}
                    for b in rows]
        route.fulfill(content_type="application/json", body=json.dumps({
            "session_id": sid, "threads": [{
                "session_id": sid, "agent_id": "agent-1",
                "parent_tool_use_id": "toolu_card", "orphaned": False,
                "message_count": len(rows), "blocks": rows,
            }],
        }))

    _route_windowed_session(page, sid, [{
        "role": "tool_use", "name": "Agent", "id": "toolu_card",
        "uuid": "card", "task": {"description": "Synthetic task"},
    }])
    # Registered last so it wins over the windowed-session glob.
    page.route(f"**/api/chat/sessions/{sid}/subagents*", timeline)
    _app_eval(page, "await app.loadSession(arg);", sid)
    details = page.locator(".msg-pane:visible .subagent-timeline:visible")
    expect(details).to_be_visible()
    expect(details.locator(".subagent-timeline-count")).to_have_text("2")
    assert calls == ["summary"]
    # Closed: only the summary was fetched and no body is in the page.
    assert "FIRST_BODY" not in details.inner_html()

    details.locator("summary").click()
    expect(details).to_contain_text("FIRST_BODY")
    expect(details).to_contain_text("ls -la")
    assert calls == ["summary", "parent"]

    # A later summary adds a row: the loaded body stays, the new row loads.
    blocks.append(_block("b3", "tool_result", text="SECOND_BODY", id="t1"))
    _app_eval(page, "await app.hydrateSubagents(arg, app.tabState[arg], {quiet: true});", sid)
    expect(details).to_contain_text("SECOND_BODY")
    expect(details).to_contain_text("FIRST_BODY")
    assert calls == ["summary", "parent", "summary", "parent"]
    expect(details.locator(".subagent-timeline-count")).to_have_text("3")
    expect(details.locator(".subagent-timeline-text.is-pending:visible")).to_have_count(0)
    _assert_no_browser_errors(page, errors)


def test_failed_body_load_says_so_and_reopening_retries(page, backend_url, auth_token):
    _settled_login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    state = {"fail": True}

    def timeline(route):
        url = route.request.url
        if "parent=" in url and state["fail"]:
            route.fulfill(status=500, body="no")
            return
        stub = "view=summary" in url
        route.fulfill(content_type="application/json", body=json.dumps({
            "session_id": sid, "threads": [{
                "session_id": sid, "agent_id": "agent-1",
                "parent_tool_use_id": "toolu_card", "orphaned": False,
                "message_count": 1,
                "blocks": [_block("b1", "assistant", **({"stub": True} if stub
                                                       else {"text": "LATE_BODY"}))],
            }],
        }))

    _route_windowed_session(page, sid, [{
        "role": "tool_use", "name": "Agent", "id": "toolu_card",
        "uuid": "card", "task": {"description": "Synthetic task"},
    }])
    # Registered last so it wins over the windowed-session glob.
    page.route(f"**/api/chat/sessions/{sid}/subagents*", timeline)
    _app_eval(page, "await app.loadSession(arg);", sid)
    details = page.locator(".msg-pane:visible .subagent-timeline:visible")
    expect(details).to_be_visible()
    details.locator("summary").click()
    expect(details).to_contain_text("加载失败" if _app_eval(page, "return app.lang;") == "zh"
                                    else "Failed to load")
    state["fail"] = False
    details.locator("summary").click()
    details.locator("summary").click()
    expect(details).to_contain_text("LATE_BODY")
