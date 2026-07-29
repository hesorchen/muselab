"""Browser coverage for the global activity center's live timeline."""
from __future__ import annotations

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="install with: uv add --group dev pytest-playwright",
)
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".login, .chat-tabs-list", state="visible", timeout=5000)
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized;
        }"""
    )


def test_live_updates_and_all_status_time_view(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._activityLiveSource
            && app._activityLiveSource.readyState === EventSource.OPEN;
        }""",
        timeout=5000,
    )

    page.locator(".activity-center-btn").click()
    expect(page.locator(".activity-modal")).to_be_visible()

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const base = {
            kind: 'turn',
            workspace: '/tmp/e2e',
            workspace_name: 'e2e',
            session_name: 'Activity test',
            status_detail: '',
            read: true,
          };
          app._applyActivityUpdate({
            generation: 'e2e-generation',
            revision: 1,
            item: {
              ...base,
              id: 'older',
              session_id: 'older-session',
              task_summary: 'Older completed task',
              state: 'completed',
              started_at: 10,
              finished_at: 100,
              updated_at: 100,
            },
            summary: {
              generation: 'e2e-generation',
              revision: 1,
              running: 0,
              unread: 0,
              attention: 0,
              groups: {review: 0, running: 0, failed: 0, history: 1},
              group_unread: {review: 0, running: 0, failed: 0, history: 0},
              workspaces: [],
            },
          });
          app._applyActivityUpdate({
            generation: 'e2e-generation',
            revision: 2,
            item: {
              ...base,
              id: 'newer',
              session_id: 'newer-session',
              task_summary: 'Newer failed task',
              state: 'failed',
              started_at: 20,
              finished_at: 300,
              updated_at: 300,
            },
            summary: {
              generation: 'e2e-generation',
              revision: 2,
              running: 0,
              unread: 0,
              attention: 0,
              groups: {review: 0, running: 0, failed: 1, history: 1},
              group_unread: {review: 0, running: 0, failed: 0, history: 0},
              workspaces: [],
            },
          });
          app._applyActivityUpdate({
            generation: 'e2e-generation',
            revision: 3,
            item: {
              ...base,
              id: 'newest',
              session_id: 'newest-session',
              task_summary: 'Newest running task',
              state: 'running',
              started_at: 400,
              finished_at: null,
              updated_at: 400,
            },
            summary: {
              generation: 'e2e-generation',
              revision: 3,
              running: 1,
              unread: 0,
              attention: 0,
              groups: {review: 0, running: 1, failed: 1, history: 1},
              group_unread: {review: 0, running: 0, failed: 0, history: 0},
              workspaces: [],
            },
          });
          app.setActivityView('timeline');
        }"""
    )

    expect(page.locator(".activity-view-switch button").nth(1)).to_have_class(
        "active"
    )
    labels = page.locator(".activity-group .activity-row strong")
    expect(labels).to_have_count(3)
    assert labels.all_text_contents() == [
        "Newest running task",
        "Newer failed task",
        "Older completed task",
    ]


def test_terminal_event_wins_over_stale_tab_activity_snapshot(
    page: Page, backend_url, auth_token
):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = 'tab-terminal-e2e';
          const st = app._ensureTabState(sid);
          st.streaming = false;
          st.backgroundActive = false;
          app.sessions = [
            ...app.sessions.filter(session => session.id !== sid),
            {
              id: sid,
              name: 'Terminal state test',
              active: true,
              turn_active: true,
              background_active: false,
            },
          ];
          const before = app.isTabRunning(sid);

          app._setSessionActivityExpectation(sid, false);
          const immediate = app.isTabRunning(sid);

          // A list request that started before `done` may still return the old
          // running flags. It must not relight the dot.
          const stale = app._retainExpectedSessionActivity({
            id: sid,
            active: true,
            turn_active: true,
            background_active: false,
          });
          app.sessions = app.sessions.map(
            session => session.id === sid ? {...session, ...stale} : session,
          );
          const afterStale = app.isTabRunning(sid);
          const expectationHeld = !!st._sessionActivityExpected;

          // Once the backend echoes idle, release the guard. A later genuine
          // cross-device running transition must become visible normally.
          app._retainExpectedSessionActivity({
            id: sid,
            active: false,
            turn_active: false,
            background_active: false,
          });
          const expectationCleared = !st._sessionActivityExpected;
          app.sessions = app.sessions.map(session =>
            session.id === sid
              ? {
                  ...session,
                  active: true,
                  turn_active: true,
                  background_active: false,
                }
              : session);
          const laterRemoteTurn = app.isTabRunning(sid);
          return {
            before,
            immediate,
            afterStale,
            expectationHeld,
            expectationCleared,
            laterRemoteTurn,
          };
        }"""
    )
    assert result == {
        "before": True,
        "immediate": False,
        "afterStale": False,
        "expectationHeld": True,
        "expectationCleared": True,
        "laterRemoteTurn": True,
    }
