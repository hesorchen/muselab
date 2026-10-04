"""History status bindings reuse row metadata and keep live activity semantics."""
from __future__ import annotations

import copy
import time

from playwright.sync_api import expect

from .test_session_rename_ownership import rename_fixture as rename_fixture
from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


def test_history_search_statuses_avoid_catalog_scans_and_stay_live(page, rename_fixture):
    now = int(time.time())
    rows = copy.deepcopy(rename_fixture["sessions"])
    for index in range(998):
        rows.append({
            "id": f"synthetic-history-{index:04d}",
            "name": f"Archived target {index - 978:02d}" if index >= 978 else f"Recent session {index:04d}",
            "cwd": "/tmp/synthetic-rename-root", "model": "fixture", "permission": "default",
            "updated_at": now - index * 60, "created_at": now - index * 60,
            "active": index == 980, "turn_active": index == 980,
            "background_active": index == 979, "scheduled_active": index == 978,
        })
    rename_fixture["sessions"] = rows
    page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      for (const [index,flags] of [[981,{unread:true}],[982,{backgroundActive:true}],
          [983,{scheduledDeliveryActive:true}],[984,{streaming:true}]]) {
        Object.assign(app._ensureTabState('synthetic-history-'+String(index).padStart(4,'0')),flags);
      }
    }""")
    page.locator("#history-picker-trigger").click()
    page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0]._sessionHistoryLoaded")
    expect(page.locator(".session-picker-row")).to_have_count(20)
    page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0], array=app.sessions;
      // Delegate every call normally, counting only catalog lookups made by
      // status bindings for the synthetic search rows. No timing threshold.
      let depth=0;
      const originalFind=array.find, originals={};
      window.__historyStatusScans=0;
      array.find=function(...args) {
        if(depth) window.__historyStatusScans++;
        return originalFind.apply(this,args);
      };
      for(const method of ['isTabStreaming','isTabScheduledActive','isTabBackgroundActive','isTabRunning','isTabUnread']) {
        originals[method]=app[method];
        app[method]=function(...args) {
          const tracked=String(args[0]).startsWith('synthetic-history-');
          if(tracked) depth++;
          try { return originals[method].apply(this,args); }
          finally { if(tracked) depth--; }
        };
      }
      window.__restoreHistoryStatusObserver=()=>{
        array.find=originalFind;
        for(const [method,original] of Object.entries(originals)) app[method]=original;
      };
    }""")
    page.locator(".session-picker-search").fill("Archived target")
    expect(page.locator(".session-picker-open .ellipsis").first).to_have_text("Archived target 00")
    expect(page.locator(".session-picker-open .ellipsis").last).to_have_text("Archived target 19")
    expect(page.locator(".session-picker-row")).to_have_count(20)
    assert page.evaluate("() => window.__historyStatusScans") == 0
    page.evaluate("() => window.__restoreHistoryStatusObserver()")

    expected = [(True, False, True, False), (True, True, False, False), (True, False, False, False),
                (False, False, False, True), (True, True, False, False), (True, False, True, False),
                (True, False, False, False)] + [(False, False, False, False)] * 13
    assert page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      // Non-history callers still resolve by ID, retaining local state
      // priority and the running/unread exclusion.
      return app.filteredSessions().map(s=>[
        app.isTabRunning(s.id),app.isTabBackgroundActive(s.id),
        app.isTabScheduledActive(s.id),app.isTabUnread(s.id)]);
    }""") == [list(flags) for flags in expected]
    assert page.evaluate("""() => [...document.querySelectorAll('.session-picker-row')].map(row=>{
      const dot=row.querySelector('.chat-tab-stream-dot'),unread=row.querySelector('.chat-tab-unread-dot');
      return [getComputedStyle(dot).display!=='none',dot.classList.contains('is-background'),
        getComputedStyle(unread).display!=='none'];
    })""") == [[running, background or scheduled, unread] for running, background, scheduled, unread in expected]

    first = page.locator(".session-picker-row").first
    dot = first.locator(".chat-tab-stream-dot")
    expect(dot).to_have_attribute("title", "Scheduled task ready or running")
    page.evaluate("""() => {
      window.__firstHistoryRow=document.querySelector('.session-picker-row');
      const app=document.querySelector('#app')._x_dataStack[0];
      const meta=app.sessions.find(s=>s.id==='synthetic-history-0978');
      meta.scheduled_active=false; meta.background_active=true;
    }""")
    expect(dot).to_have_attribute("title", "Background task running")
    expect(dot).to_have_class("chat-tab-stream-dot picker-status-dot is-background")
    page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      app.sessions=app.sessions.map(s=>{
        if(s.id!=='synthetic-history-0978') return {...s};
        const replacement={...s,active:true,scheduled_active:false,background_active:false};
        delete replacement.turn_active; // Legacy server activity contract.
        return replacement;
      });
    }""")
    expect(dot).to_have_attribute("title", "Streaming…")
    expect(dot).to_have_class("chat-tab-stream-dot picker-status-dot")
    expect(dot).to_be_visible()
    assert page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      return app.isTabStreaming('synthetic-history-0978') && app.isTabRunning('synthetic-history-0978')
        && !app.isTabBackgroundActive('synthetic-history-0978')
        && !app.isTabScheduledActive('synthetic-history-0978');
    }""")
    assert page.evaluate("() => window.__firstHistoryRow===document.querySelector('.session-picker-row')")
    assert rename_fixture["errors"] == []
    assert page.evaluate("() => window.__renameUnhandled") == []
    page.keyboard.press("Escape")
    expect(page.locator("#history-picker-pop")).to_be_hidden()
