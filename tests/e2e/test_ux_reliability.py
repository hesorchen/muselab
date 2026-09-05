"""Targeted browser contracts for settings and conversation reliability."""
from __future__ import annotations

import json
import pytest

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_memory_status_failure_does_not_block_content(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/api/memory/status", lambda route: route.fulfill(
        status=503, content_type="application/json", body="{}"))
    page.route("**/api/memory/items?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "items": [{"id": "memory-fixture", "content": "Visible despite status failure",
                       "kind": "fact", "status": "active"}], "total": 1,
        })))
    _app_eval(page, 'await app.openMemoryCenter();')
    expect(page.locator(".memory-center-section .memory-card-content").filter(
        has_text="Visible despite status failure")).to_be_visible()
    assert _app_eval(page, "return app.settings.memory.listLoaded;")
    assert _app_eval(page, "return app.settings.memory.statusError;")
    assert not page.locator(".settings-menu").is_visible()


def test_memory_old_response_cannot_replace_new_tab(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      await app.openMemoryCenter();
      while (app.settings.memory.loading) await new Promise(r => setTimeout(r, 20));
      const original = window.fetch, replies = {};
      window.fetch = (url, options) => {
        const path = String(url).split('?')[0];
        if (['/api/memory/items','/api/memory/jobs'].includes(path)) {
          return new Promise(resolve => replies[path] = resolve);
        }
        return original(url, options);
      };
      try {
        app.settings.memory.tab='items'; const older=app.refreshMemoryCenter();
        app.settings.memory.tab='jobs'; const newer=app.refreshMemoryCenter();
        replies['/api/memory/jobs'](new Response(JSON.stringify({items:[{id:'new-job'}]})));
        await newer;
        replies['/api/memory/items'](new Response(JSON.stringify({items:[{id:'old-memory'}]})));
        await older;
        return {tab:app.settings.memory.tab, ids:app.settings.memory.items.map(i=>i.id)};
      } finally { window.fetch=original; }
    """)
    assert result == {"tab": "jobs", "ids": ["new-job"]}


def test_absolute_reference_captures_origin_workspace(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const original=app.fileWorkspacePath;
      try {
        app.fileWorkspacePath=()=>'/workspace/first';
        const quote=app._selectionQuoteSnapshot({source:'preview',path:'notes/test.md',text:'fixture'});
        const mention=app._mentionPath('notes/test.md');
        app.fileWorkspacePath=()=>'/workspace/second';
        return {path:quote.path, mention, prompt:app._composerPromptText('',[quote]),
          absolute:app.absoluteFilePath('/workspace/first/notes/test.md')};
      } finally {app.fileWorkspacePath=original;}
    """)
    assert result["path"] == result["mention"] == result["absolute"] == "/workspace/first/notes/test.md"
    assert "/workspace/first/notes/test.md" in result["prompt"]
    assert "/workspace/second" not in result["prompt"]


def test_settings_service_and_general_layout(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setLang('zh'); await app.openSettings('service');")
    service = page.locator('[data-page="service"]')
    expect(service).to_be_visible()
    expect(service.locator('button', has_text="重启服务")).to_be_visible()
    assert page.locator('[data-page="versions"] button').evaluate_all(
        "buttons => buttons.every(b=>b.getAttribute('@click')!=='restartService()')")
    _app_eval(page, "app.selectSettingsPage('general');")
    for name in ("lang", "appearance", "notification"):
        expect(page.locator(f'[data-page="{name}"]')).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    _app_eval(page, "await app.openMemoryCenter();")
    expect(page.locator(".settings-modal")).to_be_visible()
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


@pytest.mark.parametrize("scenario", ["successors", "evicted", "not_committed", "active_successor", "retry"])
def test_completed_history_recovers_without_status_waterfall(
    page, backend_url, auth_token, scenario,
):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId, st=app.tabState[sid];
      st.streaming=false; st.es=null; st._composerSubmitToken=''; st._queueAdmission=null;
      const originals=[app._fetchWithDeadline,app.loadSession,app._requestSessionSync];
      const requests=[], installs=[], retries=[];
      const messages=[{role:'user',uuid:'u-a',text:'first'},
        {role:'assistant',uuid:'a-final',text:'first complete'},
        {role:'user',uuid:'u-b',text:'next'},
        {role:'assistant',uuid:'b-final',text:'latest complete'}];
      const snapshot={messages,total:4,offset:0,has_later:false,
        completion_state:{stable:true,active:false,completed_turn_id:'turn-b'}};
      if(arg==='evicted') {snapshot.messages=messages.slice(2);snapshot.offset=1000;snapshot.total=1002;}
      if(arg==='not_committed') {snapshot.messages=messages.slice(2);snapshot.completion_state.completed_turn_id='turn-a';}
      if(arg==='active_successor') {snapshot.completion_state.active=true;snapshot.completion_state.turn_id='turn-b';}
      if(arg==='retry') snapshot.completion_state.stable=false;
      app._fetchWithDeadline=async url=>{requests.push(url);return new Response(JSON.stringify(snapshot));};
      app.loadSession=async (id,opts)=>{installs.push(opts.historySnapshot);return true;};
      app._requestSessionSync=(id,reason,opts)=>{retries.push(opts);return Promise.resolve(false);};
      try {
        const ok=await app._runCompletedTurnSync(sid,st,{
          expectedAssistantUuid:'a-final',completedTurnId:'turn-a',attempt:30});
        return {ok,installs:installs.length,retries:retries.length,
          delay:retries[0]?.delayMs || 0,requests};
      } finally {
        [app._fetchWithDeadline,app.loadSession,app._requestSessionSync]=originals;
        st._pendingCompletedTurnSync=null;
      }
    """, scenario)
    assert not any("/active" in url for url in result["requests"])
    if scenario in {"successors", "evicted"}:
        assert result["ok"] is True and result["installs"] == 1
    else:
        assert result["installs"] == 0 and result["retries"] == 1
        assert result["delay"] >= 5000
