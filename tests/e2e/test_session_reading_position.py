"""Switching chat sessions reopens each one where the reader left it."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page  # noqa: E402

from tests.e2e.test_chat_render_perf import (  # noqa: E402
    _app_eval,
    _assert_no_browser_errors,
    _capture_browser_errors,
    _login,
    _make_mixed_messages,
    _route_windowed_session,
)

SESSION_IDS = [f"reading-pos-{i}" for i in range(5)]

_SEED = """
app.refreshSessions = async () => {};
app._fetchTabUsage = async () => {};
app._scheduleIdlePreload = () => {};
app.availableModels = [{
  model: "e2e-model", label: "E2E model", group: "e2e", supports_thinking: true,
}];
app.model = "e2e-model";
app.sessions = arg.ids.map((id, index) => ({
  id, name: `Reading ${index}`, updated_at: Date.now() / 1000 - index,
  model: "e2e-model", permission: "bypassPermissions", thinking: true,
}));
app.openTabIds = arg.ids.slice();
app.tabState = {};
for (const id of arg.ids) {
  const st = app._blankTabState();
  st._loaded = true;
  st.messages = app._historyEnvelopes(id, arg.messages[id]);
  st.messageRange.visibleEnd = st.messages.length;
  st.messageRange.total = st.messages.length;
  st.messagesReady = true;
  st.messagesLoading = false;
  st.atBottom = true;
  // The fixture transcript is the canonical one: nothing newer to fetch.
  st._seenUpdated = app.sessions.find(row => row.id === id).updated_at;
  app.tabState[id] = st;
  app._ensureTabState(id);
}
app.currentId = arg.ids[0];
app._activateTabState(app.currentId);
return true;
"""

_SETTLED = """() => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const overlay = document.querySelector(".chat-transcript-loading-overlay");
  const st = app.tabState[app.currentId];
  return st && st.messagesReady && st.transcriptLoadPhase === "idle"
    && (!overlay || getComputedStyle(overlay).display === "none");
}"""

# First message whose bottom is below the scroller top, plus its offset.
_READING_SPOT = """() => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const body = app._chatBodyElement();
  const top = body.getBoundingClientRect().top;
  const pane = app._paneElement(app.currentId);
  for (const el of pane.querySelectorAll(".msg[data-message-key]")) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom <= top) continue;
    return {
      sid: app.currentId,
      key: el.getAttribute("data-message-key"),
      offset: rect.top - top,
      bottomDistance: body.scrollHeight - body.scrollTop - body.clientHeight,
      atBottom: app.tabState[app.currentId].atBottom,
    };
  }
  return null;
}"""


def _open(page: Page, backend_url: str, auth_token: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    payload = {
        "ids": SESSION_IDS,
        "messages": {
            sid: _make_mixed_messages(80, f"READPOS_{idx}")
            for idx, sid in enumerate(SESSION_IDS)
        },
    }
    _app_eval(page, _SEED, payload)
    page.wait_for_function(_SETTLED)


def _click_tab(page: Page, sid: str) -> None:
    page.locator(f'.chat-tab[data-tid="{sid}"]').click()
    page.wait_for_function(
        "sid => document.querySelector('#app')._x_dataStack[0].currentId === sid",
        arg=sid)
    page.wait_for_function(_SETTLED)
    # Let the anchor settle loop (a few frames) finish before measuring.
    page.wait_for_timeout(400)


def _scroll_up_with_wheel(page: Page, pixels: int) -> None:
    box = page.locator(".chat-body").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -pixels)
    page.wait_for_timeout(500)


def test_switching_sessions_restores_reading_position_warm_and_cold(
    page: Page, backend_url: str, auth_token: str,
):
    errors = _capture_browser_errors(page)
    _open(page, backend_url, auth_token)
    first, second, third, fourth, fifth = SESSION_IDS
    page.wait_for_timeout(500)
    assert page.evaluate(_READING_SPOT)["atBottom"] is True

    # The reader scrolls up into the middle of the first session's history.
    _scroll_up_with_wheel(page, 2600)
    spot = page.evaluate(_READING_SPOT)
    assert spot["atBottom"] is False and spot["bottomDistance"] > 1500, spot

    # Warm switch: the first pane stays mounted, the shared scroller moves.
    _click_tab(page, second)
    assert page.evaluate(_READING_SPOT)["bottomDistance"] < 2
    _click_tab(page, first)
    warm = page.evaluate(_READING_SPOT)
    assert warm["key"] == spot["key"], (spot, warm)
    assert abs(warm["offset"] - spot["offset"]) < 2, (spot, warm)
    assert warm["atBottom"] is False

    # Cold switch: visiting four other sessions evicts the first pane
    # (3 warm panes), so returning has to remount it.
    for sid in (second, third, fourth, fifth):
        _click_tab(page, sid)
    cold_pane_count = page.evaluate(
        "sid => document.querySelectorAll(`.msg-pane[data-tid=\"${sid}\"]`).length",
        first)
    assert cold_pane_count == 0, "first session pane should have been evicted"
    _click_tab(page, first)
    cold = page.evaluate(_READING_SPOT)
    assert cold["key"] == spot["key"], (spot, cold)
    assert abs(cold["offset"] - spot["offset"]) < 2, (spot, cold)
    assert cold["atBottom"] is False

    # A session left at the tail still reopens at the tail, following.
    _click_tab(page, second)
    tail = page.evaluate(_READING_SPOT)
    assert tail["bottomDistance"] < 2 and tail["atBottom"] is True, tail

    # Clicking the already-selected tab is still the explicit "latest" gesture.
    _click_tab(page, first)
    page.locator(f'.chat-tab[data-tid="{first}"]').click()
    page.wait_for_timeout(800)
    assert page.evaluate(_READING_SPOT)["bottomDistance"] < 2
    _assert_no_browser_errors(page, errors)


def test_reading_position_survives_a_quiet_reload_of_new_canonical_messages(
    page: Page, backend_url: str, auth_token: str,
):
    """New messages that landed while the reader was away must not yank them."""
    errors = _capture_browser_errors(page)
    _open(page, backend_url, auth_token)
    first, second = SESSION_IDS[:2]
    page.wait_for_timeout(500)
    _scroll_up_with_wheel(page, 2600)
    spot = page.evaluate(_READING_SPOT)
    assert spot["atBottom"] is False, spot
    _click_tab(page, second)

    # While away, the first session gains two canonical messages.
    base = _make_mixed_messages(80, "READPOS_0")
    extra = [
        {**base[-2], "uuid": "READPOS_0-new-1", "text": "new one", "html": "<p>new one</p>"},
        {**base[-2], "uuid": "READPOS_0-new-2", "text": "new two", "html": "<p>new two</p>"},
    ]
    newer = base[:-1] + extra + [base[-1]]
    later = time.time() + 100
    _route_windowed_session(page, first, newer, updated_at=later)
    page.evaluate(
        """([sid, count, updated]) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const row = app.sessions.find(item => item.id === sid);
          row.message_count = count;
          row.updated_at = updated;
        }""",
        [first, len(newer), later],
    )
    _click_tab(page, first)
    page.wait_for_timeout(800)
    after = page.evaluate(_READING_SPOT)
    assert after["key"] == spot["key"], (spot, after)
    assert abs(after["offset"] - spot["offset"]) < 2, (spot, after)
    assert after["atBottom"] is False, after
    # The reload did land: the transcript grew below the reader.
    assert after["bottomDistance"] > spot["bottomDistance"], (spot, after)
    _assert_no_browser_errors(page, errors)


_OPEN_FROM_TASK_CENTER = """async sid => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const now = Date.now() / 1000;
  app.activity.events = [{
    id: "evt-" + sid, session_id: sid, thread_id: sid, state: "completed",
    read: false, title: "done", created_at: now, updated_at: now, workspace: "",
  }];
  await app.openActivityCenter();
  await app.openActivityEvent(app.activity.events[0]);
  return app.currentId;
}"""


@pytest.mark.parametrize("from_other_session", [False, True])
def test_task_center_entry_keeps_reading_position(
    page: Page, backend_url: str, auth_token: str, from_other_session: bool,
):
    """Opening a session from the task center lands where the reader left it."""
    errors = _capture_browser_errors(page)
    _open(page, backend_url, auth_token)
    first, second = SESSION_IDS[:2]
    page.wait_for_timeout(500)
    _scroll_up_with_wheel(page, 2600)
    spot = page.evaluate(_READING_SPOT)
    assert spot["atBottom"] is False, spot
    if from_other_session:
        _click_tab(page, second)

    assert page.evaluate(_OPEN_FROM_TASK_CENTER, first) == first
    page.wait_for_function(_SETTLED)
    page.wait_for_timeout(600)
    after = page.evaluate(_READING_SPOT)
    assert after["key"] == spot["key"], (spot, after)
    assert abs(after["offset"] - spot["offset"]) < 2, (spot, after)
    assert after["atBottom"] is False, after
    _assert_no_browser_errors(page, errors)


_TWO_WORKSPACES = """
const [wsA, wsB] = ["/e2e/ws-a", "/e2e/ws-b"];
app.sessionWorkspaces = [
  { id: "ws-a", path: wsA, name: "ws-a", primary: true },
  { id: "ws-b", path: wsB, name: "ws-b", primary: false },
];
app.activeWorkspace = wsA;
app.sessions = app.sessions.map((row, index) => (
  { ...row, cwd: index < 2 ? wsA : wsB }));
app.workspaceLastSession = {};
// Workspace switching also reloads the tree, terminals and the scoped session
// list from the server; the fixture sessions only exist in the page.
app.loadRoot = async () => true;
app.loadTrash = async () => false;
app.fetchContextInfo = async () => false;
app.fetchTerminals = async () => false;
app._startFileEvents = () => {};
app._stopFileEvents = () => {};
app._pullWorkspaceSessions = async path => ({
  ok: true, sessions: app.sessions.filter(row => row.cwd === path),
});
return [wsA, wsB];
"""


@pytest.mark.parametrize("via_task_center", [False, True])
def test_workspace_switch_keeps_reading_position(
    page: Page, backend_url: str, auth_token: str, via_task_center: bool,
):
    """Leaving a workspace mid-history and coming back lands on the same message."""
    errors = _capture_browser_errors(page)
    _open(page, backend_url, auth_token)
    ws_a, ws_b = _app_eval(page, _TWO_WORKSPACES)
    first = SESSION_IDS[0]
    page.wait_for_timeout(500)
    _scroll_up_with_wheel(page, 2600)
    spot = page.evaluate(_READING_SPOT)
    assert spot["atBottom"] is False and spot["sid"] == first, spot

    _app_eval(page, "await app.switchWorkspace(arg); return true;", ws_b)
    page.wait_for_function(
        "ws => document.querySelector('#app')._x_dataStack[0].activeWorkspace === ws",
        arg=ws_b)
    page.wait_for_function(_SETTLED)

    if via_task_center:
        # The task center names the workspace of the session it opens.
        page.evaluate(
            """async ([sid, workspace]) => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const now = Date.now() / 1000;
              await app.openActivityEvent({
                id: "evt-" + sid, session_id: sid, thread_id: sid, workspace,
                state: "completed", read: false, title: "done",
                created_at: now, updated_at: now,
              });
            }""",
            [first, ws_a],
        )
    else:
        _app_eval(page, "await app.switchWorkspace(arg); return true;", ws_a)
    page.wait_for_function(
        "ws => document.querySelector('#app')._x_dataStack[0].activeWorkspace === ws",
        arg=ws_a)
    page.wait_for_function(
        "sid => document.querySelector('#app')._x_dataStack[0].currentId === sid",
        arg=first)
    page.wait_for_function(_SETTLED)
    page.wait_for_timeout(800)
    after = page.evaluate(_READING_SPOT)
    assert after["sid"] == first
    assert after["key"] == spot["key"], (spot, after)
    assert abs(after["offset"] - spot["offset"]) < 2, (spot, after)
    assert after["atBottom"] is False, after
    _assert_no_browser_errors(page, errors)
