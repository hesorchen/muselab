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


@pytest.mark.parametrize("width,ending", [(1440, "end"), (390, "boundary"), (1440, "terminal")])
def test_native_compaction_animates_and_recovers_after_reload(
    page, request, auth_token, width, ending,
):
    """Real mux replay must restore maintenance UI on the correct session."""
    from playwright.sync_api import expect

    page.set_viewport_size({"width": width, "height": 900})
    errors = _capture_browser_errors(page)
    base = request.getfixturevalue("live_mux_server")
    _login(page, base, auth_token)
    page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0]._chatMuxConnected")
    sid = _app_eval(page, """
        await app._ensureSessionRegistered(app.currentId);
        return app.currentId;
    """)
    headers = {"X-Auth-Token": auth_token}
    assert page.request.post(base + "/fixture/begin", headers=headers, data={"sid": sid}).ok
    page.wait_for_function(
        "sid => document.querySelector('#app')._x_dataStack[0].tabState[sid]?.es", arg=sid,
    )
    response = page.request.post(
        base + "/fixture/native-compact", headers=headers, data={"sid": sid, "phase": "start"},
    )
    assert response.ok
    started_at = response.json()["started_at_ms"]
    animation = page.locator(".compact-pending:visible")
    expect(animation).to_have_count(1)
    expect(animation).to_be_in_viewport()
    assert animation.locator(".thinking-dots span").first.evaluate(
        "el => getComputedStyle(el).animationName") != "none"
    assert _app_eval(page, "return app.tabState[arg]._compactStartedAt;", sid) == started_at

    # Another tab must not inherit this session's maintenance state.
    page.locator(".chat-tab-new").click()
    expect(page.locator(".compact-pending:visible")).to_have_count(0)
    _app_eval(page, "await app.activateTab(arg);", sid)
    expect(page.locator(".compact-pending:visible")).to_have_count(1)

    # A new page must reconstruct start state from server replay, not a local flag.
    page.reload()
    expect(page.locator(".compact-pending:visible")).to_have_count(1)
    assert _app_eval(page, "return app.tabState[arg]._compactStartedAt;", sid) == started_at
    if ending == "terminal":
        assert page.request.post(base + "/fixture/finish", headers=headers, data={
            "sid": sid, "done": {"is_error": True, "error": "fixture interrupted compact"},
        }).ok
    else:
        assert page.request.post(base + "/fixture/native-compact", headers=headers, data={
            "sid": sid, "phase": ending,
        }).ok
    expect(page.locator(".compact-pending:visible")).to_have_count(0)
    assert _app_eval(page, "return app.tabState[arg]._compactStartedAt;", sid) == 0
    if ending != "terminal":
        assert page.request.post(base + "/fixture/burst", headers=headers,
                                 data={"sid": sid, "count": 1}).ok
        expect(page.locator(".msg-pane:visible")).to_contain_text("LIVE_PARENT_AFTER_AGENTS")
        assert page.request.post(base + "/fixture/finish", headers=headers,
                                 data={"sid": sid, "done": {}}).ok
    _assert_no_browser_errors(page, errors)
