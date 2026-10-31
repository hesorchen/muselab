"""Keep concurrent live-turn footer work independent of mounted message rows."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright is required")
from playwright.sync_api import expect  # noqa: E402

from .test_chat_render_perf import (  # noqa: E402
    _app_eval,
    _assert_no_browser_errors,
    _capture_browser_errors,
    _install_fake_event_source,
    _login,
)


def test_concurrent_streams_bound_footer_work_and_keep_live_status(
    page, backend_url, auth_token,
):
    """Real Alpine bindings must not scan each live turn for every hidden footer."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"e2e-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    ids = [f"concurrent-footer-{i}" for i in range(3)]
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.availableModels = [{
          model: 'e2e-model', label: 'E2E model', group: 'e2e',
        }];
        app.model = app.defaultModel = 'e2e-model';
        app.sessions = arg.map(id => ({
          id, name: id, updated_at: Date.now() / 1000,
          model: 'e2e-model', permission: 'bypassPermissions',
          cwd: app.currentWorkspacePath(),
        }));
        app.openTabIds = arg.slice();
        app.tabState = {};
        for (const id of arg) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messagesReady = true;
          st.atBottom = true;
          app.tabState[id] = st;
        }
        app.currentId = arg[0];
        app._activateTabState(arg[0]);
        """,
        ids,
    )
    for index, sid in enumerate(ids):
        _app_eval(
            page,
            """
            app.currentId = arg;
            await app.switchSession();
            app.input = 'Inspect the fixture';
            app.send();
            """,
            sid,
        )
        page.wait_for_function(
            "n => window.__fakeChatStreams().length === n", arg=index + 1,
        )

    # Exercise actual SSE handlers, including updates in the two warm panes.
    page.evaluate(
        """async () => {
          window.__emitFooterRound = i => {
            for (let s = 0; s < 3; s++) {
              const toolId = `footer-tool-${s}-${i}`;
              const taskId = `footer-task-${s}-${i}`;
              window.__emitSseAt(s, 'text', {text: `Progress ${s}/${i} `});
              window.__emitSseAt(s, 'tool_use', {
                id: toolId, name: 'Agent', summary: `Inspect ${i}`,
                input: {description: `Inspect ${i}`, run_in_background: true},
              });
              window.__emitSseAt(s, 'task_started', {
                task_id: taskId, tool_use_id: toolId, description: `Inspect ${i}`,
              });
              window.__emitSseAt(s, 'subagent_delta', {
                parent_tool_use_id: toolId, block_id: `footer-child-${s}-${i}`,
                kind: 'assistant', offset: 0, delta: `Result ${s}/${i}`,
              });
              window.__emitSseAt(s, 'task_notification', {
                task_id: taskId, tool_use_id: toolId,
                status: 'completed', summary: 'Done', background_tasks_pending: 0,
              });
            }
          };
          for (let i = 0; i < 60; i++) {
            window.__emitFooterRound(i);
          }
          await new Promise(resolve => setTimeout(resolve, 250));
        }"""
    )
    snapshot = _app_eval(page, """
      return {currentId: app.currentId, open: app.openTabIds,
        workspace: app.currentWorkspacePath(), warm: app.warmTranscriptTabIds(),
        sessions: app.sessions.map(s => ({id: s.id, cwd: s.cwd})),
        mounted: [...document.querySelectorAll('.msg-pane')].map(p => p.dataset.tid)};
    """)
    assert len(snapshot["mounted"]) == 3, snapshot
    assert page.locator('.msg-pane[data-tid^="concurrent-footer-"] .msg').count() >= 270

    # Count the expensive ownership operation instead of setting a timing
    # threshold sensitive to CI CPU load. Every sample appends in all panes.
    _app_eval(
        page,
        """
        const original = app._turnMessageBelongsToActiveTurn;
        window.__footerOwnershipCalls = 0;
        app._turnMessageBelongsToActiveTurn = function(...args) {
          window.__footerOwnershipCalls += 1;
          return original.apply(this, args);
        };
        await app.$nextTick();
        """,
    )
    calls = page.evaluate(
        """async () => {
          const samples = [];
          for (let i = 60; i < 64; i++) {
            window.__footerOwnershipCalls = 0;
            window.__emitFooterRound(i);
            await new Promise(resolve => requestAnimationFrame(() =>
              requestAnimationFrame(resolve)));
            samples.push(window.__footerOwnershipCalls);
          }
          return samples;
        }"""
    )
    # A few bindings per actual turn tail, with room for scheduling variation;
    # hidden per-message footers used to cause thousands of scans per update.
    assert 0 < max(calls) < 500, calls

    for sid in ids:
        _app_eval(
            page,
            "app.currentId = arg; await app.switchSession();",
            sid,
        )
        pane = page.locator(f'.msg-pane[data-tid="{sid}"]')
        expect(pane).to_be_visible()
        expect(pane.locator('.turn-footer:visible')).to_have_count(1)
        expect(pane.locator('.turn-footer .turn-status.running')).to_be_visible()
        expect(pane.locator('.turn-footer .turn-model')).to_contain_text('E2E model')
        state = _app_eval(
            page,
            """
            const st = app._ensureTabState(arg);
            return {streaming: st.streaming, messages: st.messages.length,
              tasks: st.subagentThreads.length,
              tailSummary: st.messages[st.messages.length - 1].summary};
            """,
            sid,
        )
        assert state == {
            "streaming": True, "messages": 129,
            "tasks": 64, "tailSummary": "Inspect 63",
        }
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("offset,running", [(300, True), (0, False)])
def test_live_footer_survives_a_tail_window_without_the_turn_user(
    page, backend_url, auth_token, offset, running,
):
    """A refresh mid-way through a long turn loads only the history tail.

    2026-10-31: the turn had over 400 rows, so the reloaded window had no user
    boundary and every live row lost its Running footer.  Only an unloaded
    earlier history may stand in for that boundary.
    """
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        app.conciseChat = false;
        const sid = app.currentId, st = app._ensureTabState(sid);
        st._loaded = true;
        st.messages = [
          {role: 'tool_use', name: 'Bash', summary: 'ls', input: {command: 'ls'},
           id: 'tail-use', uuid: 'tail-use', _k: 'tail-use'},
          {role: 'tool_result', id: 'tail-use', tool_name: 'Bash',
           preview: 'README.md', uuid: 'tail-result', _k: 'tail-result'},
        ];
        Object.assign(st.messageRange, {
          offset: arg, total: arg + 2, visibleStart: 0, visibleEnd: 2,
        });
        st.streaming = true;
        st.activeTurnId = 'turn-live';
        app._activateTabState(sid);
        st.messagesReady = true;
        """,
        offset,
    )
    sid = _app_eval(page, "return app.currentId;")
    pane = page.locator(f'.msg-pane[data-tid="{sid}"]')
    expect(pane.locator(".tool")).to_be_visible()
    status = pane.locator(".turn-footer:visible .turn-status.running")
    if running:
        expect(status).to_be_visible()
    else:
        expect(status).to_have_count(0)
    _assert_no_browser_errors(page, errors)


def test_running_footer_keeps_the_server_start_after_history_and_new_rows(
    page, backend_url, auth_token,
):
    """2026-10-31: a refresh mid-continuation restarted the footer at 1s.

    The history tail says the turn has run 600s. The pane timer started just
    now, as a continuation reconnect does. The footer must show ~10m, keep
    ticking, and keep that start on a live row appended afterwards.
    """
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        app.conciseChat = false;
        const sid = app.currentId, st = app._ensureTabState(sid);
        st._loaded = true;
        st.streaming = true;
        st._streamStartedAt = Date.now();
        st.streamElapsed = 0;
        st._streamTimer = setInterval(() => {
          st.streamElapsed = (Date.now() - st._streamStartedAt) / 1000;
        }, 1000);
        st.messages = app._historyEnvelopes(sid, [
          {role: 'tool_use', name: 'Bash', summary: 'sleep 90',
           input: {command: 'sleep 90'}, id: 'snap-use', uuid: 'snap-use',
           turn_status: 'running', elapsed: 600},
        ]);
        Object.assign(st.messageRange, {
          offset: 300, total: 301, visibleStart: 0, visibleEnd: 1,
        });
        app._activateTabState(sid);
        st.messagesReady = true;
        """,
    )
    sid = _app_eval(page, "return app.currentId;")
    elapsed = page.locator(
        f'.msg-pane[data-tid="{sid}"] .turn-footer:visible .msg-elapsed')
    expect(elapsed).to_have_text(re.compile(r"· 10m0[0-2]s"))
    expect(elapsed).to_have_text(re.compile(r"· 10m0[4-9]s"), timeout=8000)
    # A live row without its own elapsed must not fall back to a fresh start.
    _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        st.messages.push({role: 'tool_result', id: 'snap-use', tool_name: 'Bash',
          preview: 'done', uuid: 'live-result', _k: 'live-result'});
        st.messageRange.total += 1;
        st.messageRange.visibleEnd = st.messages.length;
        """,
    )
    expect(page.locator(f'.msg-pane[data-tid="{sid}"] .msg')).to_have_count(2)
    expect(elapsed).to_have_text(re.compile(r"· 10m(0[4-9]|1\d)s"))
    # Once history reports the turn ended, the hint is forgotten.
    hint = _app_eval(
        page,
        """
        const sid = app.currentId, st = app._ensureTabState(sid);
        app._adoptRunningTurnStart(sid, [{turn_status: 'completed', elapsed: 700}]);
        return st._runningTurnStartHint;
        """,
    )
    assert hint == 0
    _assert_no_browser_errors(page, errors)
