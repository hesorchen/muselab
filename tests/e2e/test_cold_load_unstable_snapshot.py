"""A cold load that races a runtime change still installs the history.

The backend marks a snapshot unstable when the runtime changed while it was
read. The client rejects it, but an empty pane has no other owner that would
reload it, so the cold load retries instead of leaving the transcript blank.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_unstable_cold_snapshot_is_retried_until_history_installs(
    page, backend_url, auth_token,
):
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    messages = [
        {"role": "user", "text": "EARLIER_PROMPT", "uuid": "u1"},
        {"role": "assistant", "text": "EARLIER_ANSWER", "uuid": "a1",
         "turn_status": "completed", "ts": 1_700_000_000_000},
    ]
    calls = []

    def handle(route):
        calls.append(route.request.url)
        stable = len(calls) > 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": sid, "name": "unstable cold load", "model": "e2e-model",
            "permission": "bypassPermissions", "messages": messages,
            "offset": 0, "total": len(messages), "has_more": False,
            "history_order": "normal", "history_generation": "gen-unstable",
            "completion_state": {"stable": stable, "active": False},
        }))

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    _app_eval(page, """
      const st = app.tabState[arg];
      st.messages.splice(0, st.messages.length);
      st._loaded = false;
      await app._ensureSessionLoaded(arg);
    """, sid)
    assert len(calls) >= 2
    expect(page.locator(".msg-pane:visible .msg.assistant",
                        has_text="EARLIER_ANSWER")).to_be_visible()
    assert _app_eval(page, "return app.tabState[arg]._loaded;", sid) is True


def test_failed_cold_read_is_retried_while_the_pane_is_empty(
    page, backend_url, auth_token,
):
    """A cold read that fails on the network (a timeout under a busy server)
    is retried with backoff instead of leaving the pane on its spinner."""
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    messages = [
        {"role": "user", "text": "RETRY_PROMPT", "uuid": "u1"},
        {"role": "assistant", "text": "RETRY_ANSWER", "uuid": "a1",
         "turn_status": "completed", "ts": 1_700_000_000_000},
    ]
    calls = []

    def handle(route):
        # The glob's "?" also matches the "/" of sub-routes (/queue, /active).
        if urlparse(route.request.url).path != f"/api/chat/sessions/{sid}":
            route.continue_()
            return
        calls.append(route.request.url)
        if len(calls) <= 2:
            route.abort("timedout")
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": sid, "name": "failed cold load", "model": "e2e-model",
            "permission": "bypassPermissions", "messages": messages,
            "offset": 0, "total": len(messages), "has_more": False,
            "history_order": "normal", "history_generation": "gen-retry",
            "completion_state": {"stable": True, "active": False},
        }))

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    loaded = _app_eval(page, """
      const st = app.tabState[arg];
      st.messages.splice(0, st.messages.length);
      st._loaded = false;
      return await app._ensureSessionLoaded(arg);
    """, sid)
    assert len(calls) == 3
    assert loaded is True
    expect(page.locator(".msg-pane:visible .msg.assistant",
                        has_text="RETRY_ANSWER")).to_be_visible()
