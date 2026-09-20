"""Keep concurrent live-turn footer work independent of mounted message rows."""
from __future__ import annotations

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
