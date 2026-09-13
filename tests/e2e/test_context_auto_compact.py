"""Automatic compaction obeys the backend budget through the real SSE path."""
import pytest

from .test_live_subagent_updates import live_mux_server  # noqa: F401
from .test_chat_render_perf import (
    _app_eval, _capture_browser_errors, _assert_no_browser_errors, _login,
)


@pytest.mark.parametrize("threshold,terminal,expected", [
    (0, {}, 0),
    (None, {}, 0),
    (90000, {}, 1),
    (90000, {"is_error": True, "error": "fixture failure"}, 0),
    (90000, {"cancelled": True}, 0),
])
def test_completed_turn_only_auto_compacts_with_authoritative_budget(
    page, request, auth_token, threshold, terminal, expected,
):
    errors = _capture_browser_errors(page)
    base = request.getfixturevalue("live_mux_server")
    _login(page, base, auth_token)
    page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0]._chatMuxConnected")
    sid = _app_eval(page, """
        await app._ensureSessionRegistered(app.currentId);
        window.__compactRequests = [];
        app.runCompact = async (sid, options) => window.__compactRequests.push({sid, options});
        return app.currentId;
    """)
    headers = {"X-Auth-Token": auth_token}
    response = page.request.post(base + "/fixture/begin", headers=headers, data={"sid": sid})
    assert response.ok
    page.wait_for_function(
        "sid => document.querySelector('#app')._x_dataStack[0].tabState[sid]?.streaming",
        arg=sid,
    )
    usage = {"context_used": 174000, "context_limit": 100000, "context_used_pct": 174}
    if threshold is not None:
        usage["auto_compact_threshold"] = threshold
    response = page.request.post(
        base + "/fixture/finish", headers=headers,
        data={"sid": sid, "done": {"session_usage": usage, **terminal}},
    )
    assert response.ok
    page.wait_for_function(
        "sid => !document.querySelector('#app')._x_dataStack[0].tabState[sid]?.streaming",
        arg=sid,
    )
    # Let Alpine's scheduled done callback run; it must not launch a command
    # just because an estimated denominator makes the meter exceed 100%.
    page.wait_for_timeout(150)
    requests = page.evaluate("window.__compactRequests")
    assert len(requests) == expected
    if expected:
        assert requests == [{"sid": sid, "options": {"skipConfirm": True}}]
    _assert_no_browser_errors(page, errors)
