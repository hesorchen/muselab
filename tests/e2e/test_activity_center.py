"""Browser coverage for the global activity center's live timeline."""
from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="install with: uv add --group dev pytest-playwright",
)
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(
        ".login, .chat-tabs-list", state="visible", timeout=15000
    )
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=15000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized;
        }"""
    )


def test_memory_shortcut_opens_memory_settings_page(
    page: Page, backend_url, auth_token
):
    _login(page, backend_url, auth_token)

    shortcut = page.locator(".activity-center-btn + .icon-btn")
    expect(shortcut).to_have_count(1)
    expect(shortcut.locator('use[href="#i-brain"]')).to_have_count(1)
    shortcut.click()

    expect(page.locator(".settings-modal")).to_be_visible()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.settings.show && app.settings.activePage === 'memory';
        }"""
    )
    expect(page.locator(".memory-settings-section")).to_be_visible()


def test_cached_activity_refresh_does_not_shift_rows_or_modal(
    page: Page, backend_url, auth_token,
):
    """The loading indicator must be out of flow when cached rows exist."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopActivityEvents();
          await Promise.allSettled(Object.values(app._activityFetchPromises || {}));
          app.activity.viewLoaded = true;
          app.activity.view = 'timeline';
          app.activity.events = [{
            id: 'stable-refresh-row', kind: 'turn',
            session_id: 'stable-refresh-session',
            session_name: 'Stable activity row',
            task_summary: 'Must not jump when loading disappears',
            workspace: '/tmp/e2e', workspace_name: 'e2e',
            state: 'running', read: true,
            started_at: 100, finished_at: 0, updated_at: 100,
          }];
          app.activity.summary = {
            running: 1, unread: 0, attention: 0,
            groups: {review: 0, running: 1, failed: 0, history: 0},
            group_unread: {review: 0, running: 0, failed: 0, history: 0},
            workspaces: [],
          };
          app.fetchActivity = () => new Promise(resolve => {
            window.__finishStableActivityRefresh = resolve;
          });
          void app.openActivityCenter();
        }"""
    )
    expect(page.locator(".activity-modal")).to_be_visible()
    expect(page.locator(".activity-refreshing")).to_be_visible()
    row = page.locator(".activity-row").filter(has_text="Stable activity row")
    expect(row).to_be_visible()
    # Measure only the loading/refresh transition.  The modal has its own
    # short open scale transition; CI can reach this assertion while that
    # unrelated animation is still changing geometry by ~1-2px.
    page.wait_for_timeout(250)

    before = page.evaluate(
        """async () => {
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const modal = document.querySelector('.activity-modal');
          const body = document.querySelector('.activity-body');
          const row = Array.from(document.querySelectorAll('.activity-row'))
            .find(node => node.textContent.includes('Stable activity row'));
          return {
            modalHeight: modal.getBoundingClientRect().height,
            bodyHeight: body.getBoundingClientRect().height,
            rowTop: row.getBoundingClientRect().top,
          };
        }"""
    )
    page.evaluate("() => window.__finishStableActivityRefresh(true)")
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0].activity.loading"
    )
    after = page.evaluate(
        """async () => {
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const modal = document.querySelector('.activity-modal');
          const body = document.querySelector('.activity-body');
          const row = Array.from(document.querySelectorAll('.activity-row'))
            .find(node => node.textContent.includes('Stable activity row'));
          return {
            modalHeight: modal.getBoundingClientRect().height,
            bodyHeight: body.getBoundingClientRect().height,
            rowTop: row.getBoundingClientRect().top,
          };
        }"""
    )

    for key in ("modalHeight", "bodyHeight", "rowTop"):
        assert abs(after[key] - before[key]) < 1, (before, after)


def test_session_rename_updates_loaded_activity_row_immediately(
    page: Page, backend_url, auth_token
):
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const before = app.sessions.find(row => row.id === sid)?.name || '';
          app.activity.events = [{
            id: 'rename-target',
            session_id: sid,
            session_name: before,
            task_summary: 'Keep task summary',
            workspace: app.currentWorkspacePath(),
            workspace_name: 'e2e',
            state: 'completed',
            read: true,
            started_at: 1,
            finished_at: 2,
            updated_at: 2,
          }];
          app.renamingPickerSid = sid;
          app.pickerRenameDraft = 'Renamed activity row';
          await app.pickerCommitInlineRename();
          return {
            sessionName: app.sessions.find(row => row.id === sid)?.name,
            activityName: app.activity.events[0]?.session_name,
            taskSummary: app.activity.events[0]?.task_summary,
            updatedAt: app.activity.events[0]?.updated_at,
          };
        }"""
    )

    assert result == {
        "sessionName": "Renamed activity row",
        "activityName": "Renamed activity row",
        "taskSummary": "Keep task summary",
        "updatedAt": 2,
    }


def test_live_updates_and_all_status_time_view(page: Page, backend_url, auth_token):
    page.add_init_script("localStorage.removeItem('muselab_activity_view')")
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
    expect(page.locator(".activity-view-switch button").nth(1)).to_have_class(
        "active"
    )

    pin_requests: list[dict] = []

    def patch_pin(route):
        request = route.request.post_data_json
        pin_requests.append(request)
        pinned = bool(request["pinned"])
        revision = 3 + len(pin_requests)
        summary = {
            "generation": "e2e-generation",
            "revision": revision,
            "running": 1,
            "unread": 0,
            "attention": 0,
            "groups": {"review": 0, "running": 1, "failed": 1, "history": 1},
            "group_unread": {"review": 0, "running": 0, "failed": 0, "history": 0},
            "workspaces": [],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "generation": "e2e-generation",
                "revision": revision,
                "item": {
                    "kind": "turn",
                    "workspace": "/tmp/e2e",
                    "workspace_name": "e2e",
                    "session_name": "Activity test",
                    "status_detail": "",
                    "read": True,
                    "id": "older",
                    "session_id": "older-session",
                    "task_summary": "Older completed task",
                    "state": "completed",
                    "started_at": 10,
                    "finished_at": 100,
                    "updated_at": 100,
                    "pinned": pinned,
                },
                "summary": summary,
            }),
        )

    page.route("**/api/activity/older", patch_pin)

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
          app.activity.events = [];
          app.activity.expanded = {};
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
          for (let index = 0; index < 9; index += 1) {
            app.activity.events.push({
              ...base,
              id: `extra-${index}`,
              session_id: `extra-session-${index}`,
              task_summary: `Extra task ${index}`,
              state: 'completed',
              started_at: 90 - index,
              finished_at: 90 - index,
              updated_at: 90 - index,
            });
          }
        }"""
    )

    session_labels = page.locator(".activity-group .activity-session-name")
    task_labels = page.locator(".activity-group .activity-task-summary")
    expect(session_labels).to_have_count(10)
    expect(task_labels).to_have_count(10)
    assert session_labels.all_text_contents()[:3] == [
        "Activity test", "Activity test", "Activity test",
    ]
    assert task_labels.all_text_contents()[:3] == [
        "Newest running task",
        "Newer failed task",
        "Older completed task",
    ]

    older = page.locator(".activity-row-wrap").filter(
        has_text="Older completed task"
    )
    pin = older.locator(".activity-pin")
    expect(pin).to_have_attribute("aria-pressed", "false")
    pin.click()
    expect(pin).to_be_enabled()
    expect(pin).to_have_attribute("aria-pressed", "true")
    assert task_labels.all_text_contents()[:3] == [
        "Older completed task",
        "Newest running task",
        "Newer failed task",
    ]
    expect(page.locator(".activity-modal")).to_be_visible()

    pin.click()
    expect(pin).to_be_enabled()
    expect(pin).to_have_attribute("aria-pressed", "false")
    assert task_labels.all_text_contents()[:3] == [
        "Newest running task",
        "Newer failed task",
        "Older completed task",
    ]
    assert pin_requests == [{"pinned": True}, {"pinned": False}]

    more = page.locator(".activity-group-more")
    expect(more).to_have_text("2 more")
    more.click()
    expect(session_labels).to_have_count(12)
    expect(task_labels).to_have_count(12)


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


def test_activity_row_targeted_lookup_opens_mobile_session_and_workspace(
    page: Page, backend_url, auth_token
):
    """An in-flight windowed poll must not swallow an activity deep-link id."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    current = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            sid: app.currentId,
            workspace: app.currentWorkspacePath(),
          };
        }"""
    )
    target_sid = "activity-windowed-target-e2e"
    target_workspace = current["workspace"].rstrip("/") + "/activity-target"
    target_history_requests: list[str] = []

    def target_history(route):
        target_history_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": target_sid,
                "name": "Activity target session",
                "cwd": target_workspace,
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": [{
                    "role": "assistant",
                    "text": "ACTIVITY_TARGET_VISIBLE",
                    "uuid": "activity-target-assistant",
                    "ts": 1_700_100_000_000,
                }],
                "offset": 0,
                "total": 1,
                "has_more": False,
                "history_generation": "activity-target-e2e",
                "updated_at": 2,
            }),
        )

    page.route(f"**/api/chat/sessions/{target_sid}?*", target_history)
    page.route(
        "**/api/activity/activity-target-row/ack",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "summary": {
                    "running": 0,
                    "unread": 0,
                    "attention": 0,
                    "groups": {
                        "review": 0, "running": 0, "failed": 0, "history": 1,
                    },
                    "group_unread": {
                        "review": 0, "running": 0, "failed": 0, "history": 0,
                    },
                    "workspaces": [],
                },
            }),
        ),
    )

    initial = page.evaluate(
        """arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const nativeFetch = window.fetch.bind(window);
          const windowed = app.sessions
            .filter(session => session.id !== arg.targetSid)
            .map(session => ({...session}));
          const target = {
            id: arg.targetSid,
            name: 'Activity target session',
            cwd: arg.targetWorkspace,
            updated_at: 2,
            active: false,
            turn_active: false,
            background_active: false,
            model: 'e2e-model',
            permission: 'bypassPermissions',
            thinking: true,
          };
          const response = sessions => new Response(
            JSON.stringify({sessions}),
            {status: 200, headers: {'Content-Type': 'application/json'}},
          );

          window.__activityNativeFetch = nativeFetch;
          window.__activityListRequests = [];
          window.__activityOrdinaryPending = false;
          window.__activityOrdinaryReleased = false;
          window.fetch = (input, init) => {
            const raw = typeof input === 'string' ? input : input?.url || '';
            const url = new URL(raw, location.origin);
            if (url.pathname === '/api/chat/sessions' && url.searchParams.has('limit')) {
              const ids = (url.searchParams.get('ids') || '')
                .split(',').filter(Boolean);
              window.__activityListRequests.push(ids);
              if (ids.includes(arg.targetSid)) {
                return Promise.resolve(response([...windowed, target]));
              }
              window.__activityOrdinaryPending = true;
              return new Promise(resolve => {
                window.__releaseActivityOrdinary = () => {
                  if (window.__activityOrdinaryReleased) return;
                  window.__activityOrdinaryReleased = true;
                  resolve(response(windowed));
                };
              });
            }
            return nativeFetch(input, init);
          };

          const primary = {
            path: arg.currentWorkspace,
            name: 'Primary',
            primary: true,
          };
          app.sessionWorkspaces = [
            primary,
            {path: arg.targetWorkspace, name: 'Activity target', primary: false},
          ];
          app.activeWorkspace = arg.currentWorkspace;
          app.workspaceSurfaces[arg.targetWorkspace] = {
            previewSurface: 'file',
            previewTabs: [],
          };
          app._workspaceRuntimeCaches.set(arg.targetWorkspace, {
            visible: [],
            childCache: {},
            previewCache: [],
            previewCacheBytes: 0,
            trash: {items: [], count: 0},
          });
          // Keep this navigation test focused on the real workspace/session
          // state transition rather than unrelated file/terminal probes.
          app.loadRoot = async () => true;
          app.loadTrash = async () => true;
          app.fetchContextInfo = async () => true;
          app.fetchTerminals = async () => true;
          app._checkActiveTurn = () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.fetchActivity = async () => true;
          app.setMobileTab('files');
          app._sessionListPullPromise = null;
          void app._pullSessionList(false);
          return {
            targetInitiallyMissing: !app.sessions.some(
              session => session.id === arg.targetSid),
            initialTabIds: [...app.openTabIds],
            mobileTab: app.mobileTab,
          };
        }""",
        {
            "targetSid": target_sid,
            "targetWorkspace": target_workspace,
            "currentWorkspace": current["workspace"],
        },
    )
    assert initial["targetInitiallyMissing"] is True
    assert initial["mobileTab"] == "files"
    page.wait_for_function(
        "() => window.__activityOrdinaryPending === true"
    )

    page.evaluate(
        """arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.activity.viewLoaded = true;
          app.activity.view = 'timeline';
          app.activity.filter = [];
          app.activity.events = [{
            id: 'activity-target-row',
            kind: 'turn',
            session_id: arg.targetSid,
            session_name: 'Activity target session',
            workspace: arg.targetWorkspace,
            workspace_name: 'Activity target',
            task_summary: 'Open the out-of-window activity target',
            status_detail: '',
            state: 'completed',
            read: false,
            started_at: 100,
            finished_at: 200,
            updated_at: 200,
          }];
          app.activity.summary = {
            running: 0,
            unread: 1,
            attention: 0,
            groups: {review: 1, running: 0, failed: 0, history: 0},
            group_unread: {review: 1, running: 0, failed: 0, history: 0},
            workspaces: [],
          };
        }""",
        {"targetSid": target_sid, "targetWorkspace": target_workspace},
    )
    # The chat-header trigger is intentionally hidden while the mobile Files
    # surface is active. Open the global overlay through the same app method,
    # then exercise the real row click while Files remains selected.
    page.evaluate(
        """() => document.querySelector('#app')._x_dataStack[0]
          .openActivityCenter()"""
    )
    expect(page.locator(".activity-modal")).to_be_visible()
    row = page.locator(".activity-row").filter(
        has_text="Open the out-of-window activity target"
    )
    expect(row).to_be_visible()
    row.click()

    # The click switches the workspace before it reaches the coalesced list
    # pull. Keep the ordinary response pending until that exact state is seen.
    page.wait_for_function(
        """path => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return !app.activity.show && app.activeWorkspace === path
            && !!app._sessionListPullPromise;
        }""",
        arg=target_workspace,
    )
    page.evaluate("() => window.__releaseActivityOrdinary()")
    page.wait_for_function(
        """arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[arg.sid];
          return app.currentId === arg.sid
            && app.currentWorkspacePath() === arg.workspace
            && app.mobileTab === 'chat'
            && !!st && st._loaded
            && st.messages.some(m => m.text === 'ACTIVITY_TARGET_VISIBLE');
        }""",
        arg={"sid": target_sid, "workspace": target_workspace},
        timeout=10000,
    )

    result = page.evaluate(
        """sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sessionIds = new Set(app.sessions.map(session => session.id));
          const result = {
            currentId: app.currentId,
            workspace: app.currentWorkspacePath(),
            mobileTab: app.mobileTab,
            targetInSessions: sessionIds.has(sid),
            targetTabCount: app.openTabIds.filter(id => id === sid).length,
            ghostIds: app.openTabIds.filter(id => !sessionIds.has(id)),
            listRequests: window.__activityListRequests.map(ids => [...ids]),
          };
          window.fetch = window.__activityNativeFetch;
          return result;
        }""",
        target_sid,
    )
    assert result["currentId"] == target_sid
    assert result["workspace"] == target_workspace
    assert result["mobileTab"] == "chat"
    assert result["targetInSessions"] is True
    assert result["targetTabCount"] == 1
    assert result["ghostIds"] == []
    assert len(result["listRequests"]) >= 2
    assert target_sid not in result["listRequests"][0]
    assert target_sid in result["listRequests"][1]
    assert target_history_requests, "target session history was never loaded"
