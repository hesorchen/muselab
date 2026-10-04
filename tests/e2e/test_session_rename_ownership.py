"""Session rename responses own their intent, not matching label text."""
from __future__ import annotations

import copy
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


@pytest.fixture
def rename_fixture(page, settings_frontend_url):
    state = {"sessions": [{"id": sid, "name": name, "cwd": "/tmp/synthetic-rename-root",
                           "updated_at": 200, "model": "fixture", "permission": "default"}
                          for sid, name in [("rename-a", "Session A"), ("rename-b", "Session B")]],
             "pending": [], "history_reads": 0, "errors": []}
    page.on("pageerror", lambda error: state["errors"].append(str(error)))

    def handle(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.path.startswith("/api/chat/sessions/") and request.method == "PATCH":
            state["pending"].append(route)
        elif parsed.path == "/api/chat/sessions":
            state["history_reads"] += 1
            route.fulfill(json={"sessions": copy.deepcopy(state["sessions"]), "session_redirects": {}})
        elif parsed.path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            # No MuseLab backend/config/SDK is served. Every optional API is
            # synthetic as well; no real conversation or model request exists.
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""sessions => {
      const app=document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.token='rename-synthetic-token-at-least-32chars';
      app.authed=true; app.appReady=true; app._sessionsInitialized=true; app._modelsLoaded=true;
      app.availableModels=[{model:'fixture',label:'Fixture',group:'Fixture'}];
      app.activeWorkspace='/tmp/synthetic-rename-root';
      app.sessions=sessions; app.openTabIds=sessions.map(s=>s.id); app.tabState={};
      app.refreshSessions=async()=>{}; app._syncSessionListQuiet=async()=>{};
      app._fetchTabUsage=async()=>{}; app._checkActiveTurn=()=>{}; app._scheduleIdlePreload=()=>{};
      for (const meta of sessions) {
        const st=app._ensureTabState(meta.id);
        st._loaded=true; st.messagesReady=true; st.messagesLoading=false;
        st.draft.input=meta.id==='rename-b' ? 'Retained B chat draft' : '';
      }
      app._activateTabState('rename-a');
      app.activity.events=sessions.map(s=>({id:'event-'+s.id,session_id:s.id,session_name:s.name}));
      window.__renameSettled=0; window.__renameUnhandled=[];
      const rename=app._renameSessionOptimistically;
      app._renameSessionOptimistically=function(...args) {
        const promise=rename.apply(this,args);
        promise.then(()=>window.__renameSettled++,()=>window.__renameSettled++);
        return promise;
      };
      window.addEventListener('unhandledrejection',event=>{
        window.__renameUnhandled.push(String(event.reason?.message||event.reason));
      });
    }""", copy.deepcopy(state["sessions"]))
    expect(_tab(page, "rename-a")).to_be_visible()
    expect(_tab(page, "rename-b")).to_be_visible()
    yield state
    for route in state["pending"]:
        try:
            route.abort()
        except Exception:
            pass


def _tab(page, sid):
    return page.locator(f'.chat-tab[data-tid="{sid}"]')


def _rename(page, state, sid, name):
    count = len(state["pending"])
    _tab(page, sid).locator(".chat-tab-name").dblclick()
    field = _tab(page, sid).locator(".chat-tab-rename-input")
    expect(field).to_be_visible()
    field.fill(name)
    field.press("Enter")
    deadline = time.monotonic() + 5
    while len(state["pending"]) == count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(state["pending"]) == count + 1
    return state["pending"][-1]


def _finish(page, state, route, *, status=200):
    settled = page.evaluate("() => window.__renameSettled")
    sid = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
    if status == 200:
        name = route.request.post_data_json["name"]
        next(row for row in state["sessions"] if row["id"] == sid)["name"] = name
        route.fulfill(json={"ok": True, "session_id": sid})
    else:
        route.fulfill(status=status, json={"detail": "Synthetic rename rejection"})
    state["pending"].remove(route)
    page.wait_for_function("count=>window.__renameSettled>count", arg=settled)


def _history(page, state):
    count = state["history_reads"]
    page.locator(".chat-tab-history > button").click()
    page.wait_for_function("() => { const app=document.querySelector('#app')._x_dataStack[0]; return app.sessionPickerOpen && !app.sessionHistoryLoading; }")
    assert state["history_reads"] == count + 1


def _assert_clean(page, state):
    assert state["errors"] == []
    assert page.evaluate("() => window.__renameUnhandled") == []


def test_failed_tab_rename_stays_rolled_back_after_history_refresh(page, rename_fixture):
    route = _rename(page, rename_fixture, "rename-a", "Rejected name")
    _finish(page, rename_fixture, route, status=503)
    expect(_tab(page, "rename-a").locator(".chat-tab-name")).to_have_text("Session A")
    expect(page.locator(".toast").filter(has_text="Rename failed")).to_be_visible()
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0]._sessionNameExpected['rename-a'] === undefined")
    _history(page, rename_fixture)
    expect(_tab(page, "rename-a").locator(".chat-tab-name")).to_have_text("Session A")
    expect(page.get_by_role("button", name="Open session: Session A", exact=True)).to_be_visible()
    _assert_clean(page, rename_fixture)


def test_old_failed_same_label_cannot_undo_newer_confirmed_rename(page, rename_fixture):
    first = _rename(page, rename_fixture, "rename-a", "Repeated name")
    second = _rename(page, rename_fixture, "rename-a", "Middle name")
    _finish(page, rename_fixture, second)
    third = _rename(page, rename_fixture, "rename-a", "Repeated name")
    _finish(page, rename_fixture, third)
    _finish(page, rename_fixture, first, status=503)
    expect(_tab(page, "rename-a").locator(".chat-tab-name")).to_have_text("Repeated name")
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0].activity.events.find(e=>e.session_id==='rename-a').session_name") == "Repeated name"
    assert page.locator(".toast").filter(has_text="Rename failed").count() == 0
    _assert_clean(page, rename_fixture)


def test_pending_rename_failure_keeps_switched_session_and_new_editor(page, rename_fixture):
    route = _rename(page, rename_fixture, "rename-a", "Rejected A name")
    _tab(page, "rename-b").click()
    expect(_tab(page, "rename-b")).to_have_class("chat-tab active")
    expect(page.locator(".chat-input-textarea")).to_have_value("Retained B chat draft")
    _tab(page, "rename-b").locator(".chat-tab-name").dblclick()
    field = _tab(page, "rename-b").locator(".chat-tab-rename-input")
    field.fill("Unsubmitted B rename")
    _finish(page, rename_fixture, route, status=503)
    expect(_tab(page, "rename-b")).to_have_class("chat-tab active")
    expect(field).to_be_visible()
    expect(field).to_have_value("Unsubmitted B rename")
    expect(page.locator(".chat-input-textarea")).to_have_value("Retained B chat draft")
    expect(_tab(page, "rename-a").locator(".chat-tab-name")).to_have_text("Session A")
    _assert_clean(page, rename_fixture)


def test_confirmed_rename_releases_expected_name_after_server_echo(page, rename_fixture):
    route = _rename(page, rename_fixture, "rename-a", "Confirmed name")
    _finish(page, rename_fixture, route)
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0]._sessionNameExpected['rename-a'].settled")
    _history(page, rename_fixture)
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0]._sessionNameExpected['rename-a'] === undefined")
    page.locator(".chat-tab-history > button").click()
    # A later canonical list can contain a valid new name (e.g. another
    # device). Once the confirmed name was echoed, the local intent is done.
    rename_fixture["sessions"][0]["name"] = "Later canonical name"
    _history(page, rename_fixture)
    expect(_tab(page, "rename-a").locator(".chat-tab-name")).to_have_text("Later canonical name")
    _assert_clean(page, rename_fixture)
