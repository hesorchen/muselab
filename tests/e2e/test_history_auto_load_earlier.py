"""Scrolling to the top of a long session loads the preceding page by itself."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page  # noqa: E402

from tests.e2e.test_chat_render_perf import (  # noqa: E402
    _app_eval,
    _assert_no_browser_errors,
    _bootstrap_session_for_real_load,
    _capture_browser_errors,
    _login,
    _make_mixed_messages,
    _route_windowed_session,
)

_STATE = """() => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const st = app.tabState[app.currentId];
  const body = app._chatBodyElement();
  const top = body.getBoundingClientRect().top;
  let key = "", offset = 0;
  const pane = app._paneElement(app.currentId);
  for (const el of (pane ? pane.querySelectorAll(".msg[data-message-key]") : [])) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom <= top) continue;
    key = el.getAttribute("data-message-key"); offset = rect.top - top; break;
  }
  return {
    messages: st.messages.length, loading: !!st._loadingEarlier,
    earlier: app.earlierMessageCount(), hasMore: app.hasMoreHistory(), scrollTop: body.scrollTop, key, offset,
    first: (st.messages[0] && st.messages[0].text || "").slice(0, 24),
  };
}"""


_OFFSET_OF = """key => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const el = app._paneElement(app.currentId)
    .querySelector(`.msg[data-message-key="${CSS.escape(key)}"]`);
  return el ? el.getBoundingClientRect().top
    - app._chatBodyElement().getBoundingClientRect().top : null;
}"""


def test_reaching_the_top_loads_earlier_history_without_a_click(
    page: Page, backend_url: str, auth_token: str,
):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1280, "height": 800})
    sid = "auto-earlier-history"
    messages = _make_mixed_messages(380, "AUTO_EARLIER")
    requests = _route_windowed_session(page, sid, messages)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Auto earlier history")
    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[app.currentId];
          return st.messagesReady && !st.messagesLoading
            && st.messages.some(m => (m.text || "").includes("AUTO_EARLIER_379"));
        }""",
        timeout=15000,
    )
    page.wait_for_timeout(600)
    loaded = len(requests)
    start = page.evaluate(_STATE)
    assert start["hasMore"] is True and start["messages"] <= 120, start

    box = page.locator(".chat-body").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    # Reading a little way up, well below the top edge, loads nothing.
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(700)
    mid = page.evaluate(_STATE)
    assert mid["scrollTop"] > 800 and mid["earlier"] == start["earlier"], (start, mid)

    # A layout/programmatic jump to the top is not a reader gesture.
    page.wait_for_timeout(3000)
    page.evaluate("() => document.querySelector('.chat-body').scrollTop = 0")
    page.wait_for_timeout(700)
    assert page.evaluate(_STATE)["earlier"] == start["earlier"]
    assert len(requests) == loaded, requests

    # A wheel at the physical top pages without any click, and the message the
    # reader is looking at stays where it was.
    before = page.evaluate(_STATE)
    page.mouse.wheel(0, -300)
    page.wait_for_function(
        """n => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.earlierMessageCount() < n && !app.tabState[app.currentId]._loadingEarlier;
        }""",
        arg=before["earlier"], timeout=10000)
    page.wait_for_timeout(600)
    # The message the reader was looking at has not moved on screen, even
    # though older blocks now sit above it.
    moved = page.evaluate(_OFFSET_OF, before["key"])
    assert moved is not None and abs(moved - before["offset"]) < 3, (before, moved)

    # Keep wheeling up: the whole history arrives page by page.
    for _ in range(60):
        state = page.evaluate(_STATE)
        if not state["hasMore"]:
            break
        page.mouse.wheel(0, -2500)
        page.wait_for_timeout(250)
    final = page.evaluate(_STATE)
    assert final["hasMore"] is False, final
    assert final["messages"] == 380, final
    assert "AUTO_EARLIER_000" in _app_eval(
        page,
        "return app.tabState[app.currentId].messages.map(m => m.text || '').join('\\n');")
    _assert_no_browser_errors(page, errors)
