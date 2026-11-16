"""Scrolling back down a live pane after loading earlier rows returns to the
running tail.

Loading earlier rows in a live pane caps the mounted window and can cut off
the rows below the reader, including the running one. Scrolling back down
must mount them again, up to where follow was paused, so reaching the bottom
resumes the live tail and its "running" footer.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def _history(turns, live):
    rows = []
    for i in range(turns):
        rows.append({"role": "user", "text": f"Q{i}", "uuid": f"u{i}"})
        rows.append({"role": "assistant", "text": f"A{i} " + "lorem ipsum " * 60,
                     "uuid": f"a{i}", "turn_status": "completed",
                     "ts": 1_700_000_000_000 + i})
    rows.extend({"role": "assistant", "text": f"LIVE_{j} " + "dolor " * 80,
                 "uuid": f"live{j}"} for j in range(live))
    return rows


def _append_live(page, sid, j):
    _app_eval(page, """const st = app.tabState[arg[0]];
      app._appendLiveMessage(st, {role: 'assistant', uuid: 'live' + arg[1],
        text: 'LIVE_' + arg[1] + ' ' + 'dolor '.repeat(80)});""", [sid, j])


@pytest.mark.parametrize("mobile", [False, True])
def test_scrolling_back_down_returns_to_the_running_tail(
    page, backend_url, auth_token, mobile,
):
    if mobile:
        page.set_viewport_size({"width": 390, "height": 760})
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    live = {"n": 0}

    def handle(route):
        if urlparse(route.request.url).path != f"/api/chat/sessions/{sid}":
            route.continue_()
            return
        rows = _history(70, live["n"])
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": sid, "name": "live tail", "model": "e2e-model",
            "permission": "bypassPermissions", "messages": rows, "offset": 0,
            "total": len(rows), "has_more": False, "history_order": "normal",
            "history_generation": "g1",
            "completion_state": {"stable": True, "active": bool(live["n"])},
        }))

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    _app_eval(page, """const st = app.tabState[arg]; st.messages.splice(0);
      st._loaded = false; await app._ensureSessionLoaded(arg);""", sid)
    _app_eval(page, "app.scrollToBottom(true);")
    page.wait_for_timeout(300)
    _app_eval(page, "app.tabState[arg].streaming = true;", sid)
    _append_live(page, sid, 0)
    live["n"] = 1
    page.wait_for_timeout(300)
    running = page.locator(".msg-pane:visible .turn-status.running")
    expect(running).to_have_count(1)

    box = page.locator(".chat-body").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(60):
        page.mouse.wheel(0, -500)
        page.wait_for_timeout(80)
    for j in range(1, 4):
        _append_live(page, sid, j)
        live["n"] = j + 1
    page.wait_for_timeout(300)
    # Far enough up that the capped live window no longer holds the tail.
    assert _app_eval(page, "return app.hasLaterMessages(arg);", sid) is True
    expect(running).to_have_count(0)

    for _ in range(120):
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(60)
    page.wait_for_timeout(800)
    state = _app_eval(page, """const st = app.tabState[arg];
      return {later: app.hasLaterMessages(arg), atBottom: st.atBottom};""", sid)
    assert state == {"later": False, "atBottom": True}
    expect(running).to_have_count(1)
    expect(page.locator(".msg-pane:visible .msg.assistant", has_text="LIVE_3")).to_be_visible()
