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

    shortcut = page.locator(
        ".pane.chat > .pane-head button.icon-btn",
        has=page.locator('use[href="#i-brain"]'),
    )
    expect(shortcut).to_have_count(1)
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


def test_desktop_timeline_defaults_to_fifteen_rows_in_larger_modal(
    page: Page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopActivityEvents();
          await Promise.allSettled(Object.values(app._activityFetchPromises || {}));
          app.lang = 'zh';
          app.activity.viewLoaded = true;
          app.activity.view = 'timeline';
          app.activity.expanded = {};
          app.activity.loading = false;
          app.activity.events = Array.from({length: 20}, (_, index) => ({
            id: `timeline-cap-${index}`,
            kind: 'turn',
            session_id: `timeline-session-${index}`,
            session_name: `Timeline session ${index + 1}`,
            task_summary: `Timeline task ${index + 1}`,
            workspace: '/tmp/e2e',
            workspace_name: 'e2e',
            state: 'completed',
            read: true,
            started_at: 100 + index,
            finished_at: 200 + index,
            updated_at: 300 + index,
          }));
          app.activity.summary = {
            running: 0, unread: 0, attention: 0,
            groups: {review: 0, running: 0, failed: 0, history: 20},
            group_unread: {review: 0, running: 0, failed: 0, history: 0},
            workspaces: [],
          };
          app.activity.show = true;
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )

    modal = page.locator(".activity-modal")
    expect(modal).to_be_visible()
    expect(page.locator(".activity-row")).to_have_count(15)
    expect(page.locator(".activity-group-more")).to_have_text("还有 5 条")
    page.wait_for_timeout(250)

    geometry = page.evaluate(
        """() => {
          const modal = document.querySelector('.activity-modal');
          const body = document.querySelector('.activity-body');
          const modalRect = modal.getBoundingClientRect();
          const bodyRect = body.getBoundingClientRect();
          return {
            modalWidth: modalRect.width,
            modalHeight: modalRect.height,
            modalTop: modalRect.top,
            modalBottom: modalRect.bottom,
            bodyHeight: bodyRect.height,
            bodyScrollHeight: body.scrollHeight,
            viewportHeight: window.innerHeight,
          };
        }"""
    )
    assert geometry["modalWidth"] >= 690
    assert geometry["modalHeight"] >= 560
    assert geometry["bodyHeight"] >= 500
    assert geometry["bodyScrollHeight"] >= geometry["bodyHeight"]
    assert geometry["modalTop"] >= 0
    assert geometry["modalBottom"] <= geometry["viewportHeight"]

    page.locator(".activity-group-more").click()
    expect(page.locator(".activity-row")).to_have_count(20)
    expect(page.locator(".activity-group-more")).to_have_text("收起")

    # The larger ledger is desktop-only. The existing phone contract remains
    # a full-width modal with an internally scrolling body.
    page.set_viewport_size({"width": 390, "height": 844})
    mobile_geometry = page.evaluate(
        """() => {
          const modal = document.querySelector('.activity-modal');
          const body = document.querySelector('.activity-body');
          const rect = modal.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            width: rect.width,
            bottom: rect.bottom,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            bodyMaxHeight: getComputedStyle(body).maxHeight,
          };
        }"""
    )
    assert mobile_geometry["left"] == 0
    assert mobile_geometry["right"] <= mobile_geometry["viewportWidth"]
    assert mobile_geometry["width"] == mobile_geometry["viewportWidth"]
    assert mobile_geometry["bottom"] <= mobile_geometry["viewportHeight"]
    assert mobile_geometry["bodyMaxHeight"] == "none"


def test_activity_search_finds_capped_sessions_and_combines_with_status_filter(
    page: Page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1200, "height": 820})
    _login(page, backend_url, auth_token)

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopActivityEvents();
          await Promise.allSettled(Object.values(app._activityFetchPromises || {}));
          app.lang = 'zh';
          app.activity.viewLoaded = true;
          app.activity.view = 'timeline';
          app.activity.filter = [];
          app.activity.query = '';
          app.activity.expanded = {};
          app.activity.loading = false;
          app.activity.customGroups = [];
          app.activity.events = Array.from({length: 20}, (_, index) => ({
            id: `search-row-${index}`,
            kind: 'turn',
            session_id: index === 0 ? 'needle-session-uuid' : `session-${index}`,
            session_name: index === 0 ? 'Needle Alpha' : `Generic session ${index}`,
            task_summary: index === 0 ? 'Quant platform audit' : `Generic task ${index}`,
            workspace: index === 0 ? '/srv/quant-research' : '/tmp/e2e',
            workspace_name: index === 0 ? 'quant-research' : 'e2e',
            state: 'completed', read: true,
            started_at: 100 + index,
            finished_at: 200 + index,
            updated_at: 300 + index,
          }));
          app.activity.events.push({
            id: 'failed-search-row', kind: 'scheduled',
            session_id: 'failed-session', session_name: 'Broken delivery',
            task_summary: 'Publish report', status_detail: 'Renderer crashed',
            workspace: '/srv/delivery', workspace_name: 'delivery',
            state: 'failed', read: false,
            started_at: 500, finished_at: 510, updated_at: 510,
          });
          app.activity.summary = {
            running: 0, unread: 1, attention: 1,
            groups: {review: 0, running: 0, failed: 1, history: 20},
            group_unread: {review: 0, running: 0, failed: 1, history: 0},
            workspaces: ['/tmp/e2e', '/srv/quant-research', '/srv/delivery'],
          };
          app.activity.show = true;
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )

    search = page.locator(".activity-searchbar input")
    expect(search).to_be_visible()
    expect(page.locator(".activity-row")).to_have_count(15)

    # The target is older than the timeline cap, so filtering must happen before
    # the 15-row slice rather than against the already-visible rows.
    search.fill("Needle")
    expect(page.locator(".activity-row")).to_have_count(1)
    expect(page.locator(".activity-row")).to_contain_text("Needle Alpha")
    expect(page.locator(".activity-search-count")).to_have_text("1 条匹配")

    search.fill("quant-research")
    expect(page.locator(".activity-row")).to_have_count(1)
    search.fill("needle-session-uuid")
    expect(page.locator(".activity-row")).to_have_count(1)

    page.get_by_role("button", name="按状态").click()
    page.locator(".activity-chip.failed").click()
    expect(page.locator(".activity-search-empty")).to_be_visible()

    # Localized state labels and status details are searchable, and the text
    # query intersects with the selected failed-status group.
    search.fill("失败")
    expect(page.locator(".activity-row")).to_have_count(1)
    expect(page.locator(".activity-row")).to_contain_text("Broken delivery")
    search.fill("Renderer crashed")
    expect(page.locator(".activity-row")).to_have_count(1)

    page.locator(".activity-search-clear").click()
    expect(search).to_have_value("")
    expect(page.locator(".activity-row")).to_have_count(1)


def test_custom_groups_show_empty_sections_and_move_sessions(
    page: Page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopActivityEvents();
          await Promise.allSettled(Object.values(app._activityFetchPromises || {}));
          app.lang = 'zh';
          app.activity.viewLoaded = true;
          app.activity.view = 'groups';
          app.activity.loading = false;
          app.activity.expanded = {};
          app.activity.customGroups = [
            {id: 'research', name: 'Research', color: 'violet'},
            {id: 'delivery', name: 'Delivery', color: 'green'},
          ];
          app.activity.groupOrder = ['research', 'delivery', '__ungrouped__'];
          app.activity.events = [{
            id: 'grouped-row', session_id: 'grouped-session',
            session_name: 'Research session', task_summary: 'Grouped task',
            workspace: '/tmp/e2e', workspace_name: 'e2e',
            state: 'completed', read: false, group_id: 'research',
            started_at: 10, finished_at: 20, updated_at: 20,
          }, {
            id: 'ungrouped-row', session_id: 'ungrouped-session',
            session_name: 'Ungrouped session', task_summary: 'Move this task',
            workspace: '/tmp/e2e', workspace_name: 'e2e',
            state: 'running', read: true,
            started_at: 30, finished_at: 0, updated_at: 30,
          }];
          app.activity.summary = {
            running: 1, unread: 1, attention: 0,
            groups: {review: 1, running: 1, failed: 0, history: 0},
            group_unread: {review: 1, running: 0, failed: 0, history: 0},
            workspaces: [],
          };
          window.__activityGroupCalls = [];
          app.api = async (path, options = {}) => {
            window.__activityGroupCalls.push({path, options});
            if (path === '/api/activity/groups' && options.method === 'POST') {
              const group = {
                id: 'writing', name: options.json.name,
                color: options.json.color,
              };
              return {ok: true, data: {
                revision: app._activityRevision + 1,
                custom_groups: [...app.activity.customGroups, group],
                group_order: [...app.activity.groupOrder.filter(id => id !== '__ungrouped__'), 'writing', '__ungrouped__'],
              }};
            }
            if (path === '/api/activity/groups/order' && options.method === 'PUT') {
              const lookup = new Map(app.activity.customGroups.map(group => [group.id, group]));
              return {ok: true, data: {
                revision: app._activityRevision + 1,
                custom_groups: options.json.ids.map(id => lookup.get(id)).filter(Boolean),
                group_order: [...options.json.ids],
              }};
            }
            if (path.endsWith('/group') && options.method === 'PUT') {
              const id = decodeURIComponent(path.split('/').at(-2));
              const item = app.activity.events.find(row => row.id === id);
              const updated = {...item};
              if (options.json.group_id) updated.group_id = options.json.group_id;
              else delete updated.group_id;
              return {ok: true, data: {
                revision: app._activityRevision + 1,
                item: updated,
                items: [{...updated}],
                custom_groups: [...app.activity.customGroups],
                group_order: [...app.activity.groupOrder],
              }};
            }
            return {ok: false, data: null, error: 'unexpected test request'};
          };
          app.activity.show = true;
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )

    groups = page.locator(".activity-group.is-custom")
    expect(groups).to_have_count(3)
    assert page.locator(
        ".activity-custom-group-head > strong"
    ).all_text_contents() == ["Research", "Delivery", "未分组"]
    board_geometry = page.evaluate(
        """() => {
          const modal = document.querySelector('.activity-modal');
          const body = document.querySelector('.activity-body');
          const lanes = Array.from(body.querySelectorAll('.activity-group.is-custom'));
          const rects = lanes.map(node => node.getBoundingClientRect());
          return {
            modalWidth: modal.getBoundingClientRect().width,
            modalHeight: modal.getBoundingClientRect().height,
            bodyDisplay: getComputedStyle(body).display,
            columns: getComputedStyle(body).gridTemplateColumns.split(' ').length,
            laneWidths: rects.map(rect => rect.width),
            laneTops: rects.map(rect => Math.round(rect.top)),
          };
        }"""
    )
    assert board_geometry["modalWidth"] >= 1050
    # One board row should size the modal to its content instead of stretching
    # it to most of the viewport and leaving a large empty area underneath.
    assert 450 <= board_geometry["modalHeight"] <= 650, board_geometry
    assert board_geometry["bodyDisplay"] == "grid"
    assert board_geometry["columns"] == 3
    assert min(board_geometry["laneWidths"]) >= 300
    assert len(set(board_geometry["laneTops"])) == 1
    delivery = groups.filter(has_text="Delivery")
    expect(delivery.locator(".activity-custom-group-empty")).to_be_visible()

    ungrouped_row = page.locator(".activity-row-wrap").filter(
        has_text="Ungrouped session"
    )
    ungrouped_row.hover()
    ungrouped_row.locator(".activity-row-group").click()
    menu = page.locator(".activity-move-menu")
    expect(menu).to_be_visible()
    expect(menu.locator("button")).to_have_count(3)
    menu.locator("button").filter(has_text="Research").click()
    page.wait_for_function(
        "() => window.__activityGroupCalls.some(call => call.path.endsWith('/ungrouped-row/group'))"
    )
    moved_state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.activity.events.map(row => ({
            id: row.id, group: row.group_id || '', order: row.group_order,
          }));
        }"""
    )
    assert next(row for row in moved_state if row["id"] == "ungrouped-row")["group"] == "research", moved_state

    research = groups.filter(has_text="Research")
    expect(research.locator(".activity-row-wrap")).to_have_count(2)
    expect(groups.filter(has_text="未分组").locator(
        ".activity-custom-group-empty"
    )).to_be_visible()

    page.locator(".activity-groups-toolbar .btn-ghost").click()
    editor = page.locator(".activity-group-editor")
    expect(editor).to_be_visible()
    editor.locator("input").fill("Writing")
    editor.locator(".activity-group-swatch.is-rose").click()
    editor.locator('button[type="submit"]').click()
    expect(page.locator(".activity-custom-group-head > strong")).to_contain_text(
        ["Research", "Delivery", "Writing", "未分组"]
    )

    # Rows can be inserted at an exact position, and every visible lane —
    # including the built-in Ungrouped lane — participates in board ordering.
    drag_state = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const research = app.activityCustomGroupSections()
            .find(group => group.groupId === 'research');
          const source = app.activity.events.find(row => row.id === 'ungrouped-row');
          const target = app.activity.events.find(row => row.id === 'grouped-row');
          app.activity.dragEventId = source.id;
          app.activity.dragInsertAfter = false;
          await app.onActivityRowDrop(research, target);

          const writing = app.activityCustomGroupSections()
            .find(group => group.orderId === 'writing');
          app.activity.dragGroupId = writing.orderId;
          const fakeRect = {left: 0, top: 0, width: 300, height: 300};
          app.onActivityRowDragOver({
            clientX: 10, clientY: 10,
            currentTarget: {getBoundingClientRect: () => fakeRect},
          }, research, target);
          const forwardedTarget = app.activity.dragOverGroupOrderId;
          await app.onActivityRowDrop(research, target);
          await new Promise(resolve => app.$nextTick(resolve));
          return {
            forwardedTarget,
            groupOrder: [...app.activity.groupOrder],
            researchOrder: app.activityAllEvents(research).map(row => row.id),
          };
        }"""
    )
    assert drag_state["forwardedTarget"] == "research"
    assert drag_state["researchOrder"] == ["ungrouped-row", "grouped-row"]
    assert drag_state["groupOrder"] == [
        "writing", "research", "delivery", "__ungrouped__",
    ]
    assert page.locator(
        ".activity-custom-group-head > strong"
    ).all_text_contents() == ["Writing", "Research", "Delivery", "未分组"]
    expect(page.locator(".activity-group.is-custom").last.locator(
        ".activity-group-drag-handle"
    )).to_be_hidden()

    calls = page.evaluate("() => window.__activityGroupCalls")
    placement = next(
        call for call in calls
        if call["path"].endswith("/ungrouped-row/group")
        and call["options"]["json"].get("before_event_id") == "grouped-row"
    )
    assert placement["options"]["json"]["group_id"] == "research"
    reorder = next(
        call for call in reversed(calls)
        if call["path"] == "/api/activity/groups/order"
    )
    assert reorder["options"]["json"]["ids"] == [
        "writing", "research", "delivery", "__ungrouped__",
    ]

    # A busy group is an independently scrollable lane. The old five-row cap
    # plus a constrained grid lane clipped everything below roughly four rows,
    # including the "more" control, so there was no way to reach older sessions.
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.activity.events = Array.from({length: 12}, (_, index) => ({
            id: `busy-group-${index}`,
            session_id: `busy-session-${index}`,
            session_name: `Busy session ${index + 1}`,
            task_summary: `Busy task ${index + 1}`,
            workspace: '/tmp/e2e', workspace_name: 'e2e',
            state: 'completed', read: true, group_id: 'research',
            started_at: index, finished_at: index + 10, updated_at: index + 20,
          }));
          await new Promise(resolve => app.$nextTick(resolve));
        }"""
    )
    research = page.locator(".activity-group.is-custom").filter(
        has=page.locator(".activity-custom-group-head > strong", has_text="Research")
    )
    expect(research.locator(".activity-row-wrap")).to_have_count(12)
    lane_scroll = research.evaluate(
        """lane => {
          const before = {clientHeight: lane.clientHeight, scrollHeight: lane.scrollHeight};
          lane.scrollTop = lane.scrollHeight;
          return {...before, scrollTop: lane.scrollTop};
        }"""
    )
    assert 280 <= lane_scroll["clientHeight"] <= 320
    assert lane_scroll["scrollHeight"] > lane_scroll["clientHeight"]
    assert lane_scroll["scrollTop"] > 0
    expect(research.locator(".activity-session-name").last).to_have_text(
        "Busy session 1"
    )

    calls = page.evaluate("() => window.__activityGroupCalls")
    assert any(call["path"].endswith("/ungrouped-row/group") for call in calls)
    assert any(call["path"] == "/api/activity/groups" for call in calls)


def test_session_rename_updates_loaded_activity_row_immediately(
    page: Page, backend_url, auth_token
):
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopActivityEvents();
          await Promise.allSettled(Object.values(app._activityFetchPromises || {}));
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
    expect(page.locator(".activity-view-switch button").nth(0)).to_have_class(
        "active"
    )
    page.locator(".activity-view-switch button").nth(2).click()
    expect(page.locator(".activity-view-switch button").nth(2)).to_have_class(
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
          for (let index = 0; index < 14; index += 1) {
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
    expect(session_labels).to_have_count(15)
    expect(task_labels).to_have_count(15)
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
    expect(session_labels).to_have_count(17)
    expect(task_labels).to_have_count(17)


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
