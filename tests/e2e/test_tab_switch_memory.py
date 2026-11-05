"""Switching between long sessions must not pile up detached DOM.

Alpine 3.14.1 kept every unmounted message pane alive through its reactive
dependency graph, so a page that stayed open for days grew by hundreds of MB
and felt slow until it was reloaded. 3.14.9 releases them; this test fails if a
vendored upgrade or an app change brings the accumulation back.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page  # noqa: E402

from .test_chat_render_perf import (  # noqa: E402
    _app_eval,
    _login,
    _make_mixed_messages,
)

SESSIONS = 6
ROUNDS = 4

_SEED = """
app.refreshSessions = async () => {};
app._fetchTabUsage = async () => {};
app._scheduleIdlePreload = () => {};
app.availableModels = [{
  model: "e2e-model", label: "E2E model", group: "e2e", supports_thinking: true,
}];
app.model = "e2e-model";
app.sessions = arg.ids.map((id, index) => ({
  id, name: `Memory ${index}`, updated_at: Date.now() / 1000 - index,
  model: "e2e-model", permission: "bypassPermissions", thinking: true,
}));
app.openTabIds = arg.ids.slice();
app.tabState = {};
for (const id of arg.ids) {
  const st = app._blankTabState();
  st._loaded = true;
  // Like a real load: the newest 100 blocks are resident, 40 are mounted.
  const all = arg.messages[id], tail = all.slice(Math.max(0, all.length - 100));
  st.messages = app._historyEnvelopes(id, tail);
  st.messageRange.offset = all.length - tail.length;
  st.messageRange.total = all.length;
  st.messageRange.visibleEnd = st.messages.length;
  st.messageRange.visibleStart = Math.max(0, st.messages.length - 40);
  st.messagesReady = true; st.messagesLoading = false; st.atBottom = true;
  st._seenUpdated = app.sessions.find(row => row.id === id).updated_at;
  app.tabState[id] = st;
  app._ensureTabState(id);
}
app.currentId = arg.ids[0];
app._activateTabState(app.currentId);
return true;
"""


def _dom_nodes(cdp) -> int:
    cdp.send("HeapProfiler.collectGarbage")
    cdp.send("HeapProfiler.collectGarbage")
    return int(cdp.send("Memory.getDOMCounters")["nodes"])


def test_repeated_tab_switching_does_not_accumulate_dom(
    page: Page, backend_url: str, auth_token: str,
):
    if page.context.browser.browser_type.name != "chromium":
        pytest.skip("DOM counters need the Chromium DevTools protocol")
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    ids = [f"memory-{i}" for i in range(SESSIONS)]
    messages = {sid: _make_mixed_messages(900, f"MEM{idx}") for idx, sid in enumerate(ids)}
    _app_eval(page, _SEED, {"ids": ids, "messages": messages})
    page.wait_for_function(
        "() => { const a = document.querySelector('#app')._x_dataStack[0];"
        " return a.tabState[a.currentId].messagesReady; }")
    cdp = page.context.new_cdp_session(page)

    counts = []
    for _ in range(ROUNDS):
        for sid in ids:
            _app_eval(page, "await app.activateTab(arg); return true;", sid)
            page.wait_for_timeout(200)
            box = page.locator(".chat-body").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, -1500)
            page.wait_for_timeout(100)
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(100)
        counts.append(_dom_nodes(cdp))

    # Round 1 builds every pane once; after that the same sessions revisited
    # must reuse or release their DOM instead of leaving it behind (the leaking
    # version roughly doubled the count every round).
    assert counts[-1] <= counts[1] * 1.3, counts
