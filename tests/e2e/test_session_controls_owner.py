"""Late per-session setting writes retain their original session owner."""
from __future__ import annotations

import copy
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


@pytest.fixture
def control_fixture(page, settings_frontend_url):
    state = {"sessions": [
        {"id": "control-a", "name": "Session A", "model": "model-alpha",
         "permission": "default", "plan_return_permission": ""},
        {"id": "control-b", "name": "Session B", "model": "model-beta",
         "permission": "acceptEdits", "plan_return_permission": ""},
    ], "pending": [], "errors": []}
    for row in state["sessions"]:
        row.update(cwd="/tmp/synthetic-controls-root", message_count=0,
                   updated_at=200, effort="auto", service_tier="")
    page.on("pageerror", lambda error: state["errors"].append(str(error)))

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path.startswith("/api/chat/sessions/") and request.method == "PATCH":
            state["pending"].append(route)
        elif path == "/api/chat/sessions":
            route.fulfill(json={"sessions": copy.deepcopy(state["sessions"]),
                                "session_redirects": {}})
        elif path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            # The server only serves static frontend files. All API routes,
            # including any optional probe, use synthetic data without a SDK.
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""sessions => {
      const app=document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.token='controls-synthetic-token-at-least-32chars';
      app.authed=true; app.appReady=true; app._sessionsInitialized=true;
      app._modelsLoaded=true; app.activeWorkspace='/tmp/synthetic-controls-root';
      app.availableModels=['alpha','beta','gamma','delta'].map(name=>({
        model:'model-'+name, label:name, group:'Synthetic',
      }));
      app.sessions=sessions; app.openTabIds=sessions.map(s=>s.id); app.tabState={};
      app._syncSessionListQuiet=async()=>{}; app._fetchTabUsage=async()=>{};
      app._checkActiveTurn=()=>{}; app._scheduleIdlePreload=()=>{};
      for (const meta of sessions) {
        const st=app._ensureTabState(meta.id);
        st._loaded=true; st.messagesReady=true; st.messagesLoading=false;
        st.draft.input=meta.id==='control-a' ? 'Retained A chat draft' : 'Retained B chat draft';
      }
      app.currentId='control-a';
      app._activateTabState('control-a');
      window.__settingSettled={model:0,permission:0}; window.__settingUnhandled=[];
      for (const [field,method] of [
        ['model','onModelChange'], ['permission','onPermissionChange'],
      ]) {
        const change=app[method];
        app[method]=function(...args) {
          const promise=change.apply(this,args);
          promise.then(()=>window.__settingSettled[field]++,()=>window.__settingSettled[field]++);
          return promise;
        };
      }
      window.addEventListener('unhandledrejection',event=>{
        window.__settingUnhandled.push(String(event.reason?.message||event.reason));
      });
    }""", copy.deepcopy(state["sessions"]))
    expect(_tab(page, "control-a")).to_be_visible()
    expect(_tab(page, "control-b")).to_be_visible()
    yield state
    for route in state["pending"]:
        try:
            route.abort()
        except Exception:
            pass


def _tab(page, sid):
    return page.locator(f'.chat-tab[data-tid="{sid}"]')


def _select(page, field):
    return page.locator(f'.chat-toolbar select[x-model="{field}"]')


def _choose(page, state, field, value):
    count = len(state["pending"])
    select = _select(page, field)
    expect(select).to_be_enabled()
    select.select_option(value)
    deadline = time.monotonic() + 5
    while len(state["pending"]) == count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(state["pending"]) == count + 1
    route = state["pending"][-1]
    assert route.request.post_data_json[field] == value
    return route


def _finish(page, state, field, route, status):
    count = page.evaluate("field=>window.__settingSettled[field]", field)
    if status == 200:
        sid = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
        current = next(row for row in state["sessions"] if row["id"] == sid)
        payload = route.request.post_data_json
        if field == "permission":
            current["plan_return_permission"] = current["permission"] if payload[field] == "plan" else ""
        current.update(payload)
        route.fulfill(json={"ok": True, "session_id": sid})
    else:
        route.fulfill(status=status, json={"detail": "Synthetic setting rejection"})
    state["pending"].remove(route)
    page.wait_for_function("args=>window.__settingSettled[args.field]>args.count",
                           arg={"field": field, "count": count})


@pytest.mark.parametrize("field", ["model", "permission"])
@pytest.mark.parametrize("status", [503, 200], ids=["rejected", "confirmed"])
def test_late_setting_result_preserves_other_tab_and_original_outcome(page, control_fixture, field, status):
    original = "model-alpha" if field == "model" else "default"
    chosen_a = "model-gamma" if field == "model" else "plan"
    chosen_b = "model-delta" if field == "model" else "dontAsk"
    first = _choose(page, control_fixture, field, chosen_a)
    _tab(page, "control-b").click()
    expect(_tab(page, "control-b")).to_have_class("chat-tab active")
    field_b = _choose(page, control_fixture, field, chosen_b)
    page.locator(".chat-input-textarea").fill("B typed during setting save")

    _finish(page, control_fixture, field, first, status)
    expect(_tab(page, "control-b")).to_have_class("chat-tab active")
    expect(_select(page, field)).to_have_value(chosen_b)
    expect(page.locator(".chat-input-textarea")).to_have_value("B typed during setting save")
    if field == "permission":
        # A's completion must leave B's actual in-flight control disabled.
        expect(_select(page, field)).to_be_disabled()
    _finish(page, control_fixture, field, field_b, 200)
    expect(_select(page, field)).to_be_enabled()
    expect(_select(page, field)).to_have_value(chosen_b)

    _tab(page, "control-a").click()
    expected_a = chosen_a if status == 200 else original
    expect(_select(page, field)).to_have_value(expected_a)
    expect(page.locator(".chat-input-textarea")).to_have_value("Retained A chat draft")
    if status != 200:
        marker = "_modelExpected" if field == "model" else "_permissionExpected"
        assert page.evaluate("key=>document.querySelector('#app')._x_dataStack[0].tabState['control-a'][key]===null", marker)
    page.locator(".chat-tab-history > button").click()
    page.wait_for_function("() => { const app=document.querySelector('#app')._x_dataStack[0]; return app.sessionPickerOpen && !app.sessionHistoryLoading; }")
    expect(_select(page, field)).to_have_value(expected_a)
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0].sessions.find(s=>s.id==='control-a').plan_return_permission") == ("default" if field == "permission" and status == 200 else "")
    page.locator(".chat-tab-history > button").click()
    _tab(page, "control-b").click()
    expect(_select(page, field)).to_have_value(chosen_b)
    expect(page.locator(".chat-input-textarea")).to_have_value("B typed during setting save")
    assert control_fixture["errors"] == []
    assert page.evaluate("() => window.__settingUnhandled") == []
