"""Visible feedback regressions: direct sends are not queued; settings stay readable."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect
from .test_chat_render_perf import _app_eval, _login


@pytest.mark.parametrize("discovery_pending", [False, True])
def test_idle_send_paints_one_inline_message_not_an_outbox(page, backend_url, auth_token, discovery_pending):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const st=app._ensureTabState(app.currentId);
      const names=['_confirmSessionBusy','_ensureChatMux','_submissionReceipt'];
      const originals=Object.fromEntries(names.map(n=>[n,app[n]]));
      let unblock;
      const gate=new Promise(resolve=>unblock=resolve);
      app._confirmSessionBusy=async()=>false;
      app._ensureChatMux=async()=>{await gate;return false;};
      app._submissionReceipt=async()=>({state:'cancelled'});
      st.streaming=false;st.es=null;st.pendingQueue=[];st._draining=arg;
      st.backgroundActive=false;st.compacting=false;
      app.input='Idle direct feedback fixture';
      const sending=app.send();
      try {
        await new Promise(r=>setTimeout(r,120));
        const rows=st.messages.filter(m=>m.role==='user'&&m.text==='Idle direct feedback fixture');
        const outbox=document.querySelector('.queue-outbox');
        return {outbox:!!outbox&&getComputedStyle(outbox).display!=='none',
          cards:app.queueDisplayItems(st).length, users:rows.length,
          pending:rows[0]?._admissionPending};
      } finally {
        unblock();await sending;
        for(const [n,v] of Object.entries(originals))app[n]=v;
        app._disposeSessionSync(st);
      }
    """, discovery_pending)
    assert result == {"outbox": False, "cards": 0, "users": 1, "pending": True}


@pytest.mark.parametrize("width", [1440, 390])
def test_settings_loading_text_is_inside_scroll_clip(page, backend_url, auth_token, width):
    _login(page, backend_url, auth_token)
    page.set_viewport_size({"width": width, "height": 844})
    _app_eval(page, """
      app.setLang('zh');
      window.__settingsReadOriginal=app._settingsRead;
      window.__settingsFixture=await app._settingsRead('/api/settings');
      app._settingsRead=()=>new Promise(resolve=>window.__finishSettingsRead=resolve);
      window.__settingsOpen=app.openSettings('general');
    """)
    loading = page.locator('.settings-content > [role="status"]').first
    expect(loading).to_be_visible()
    geometry = loading.evaluate("""el=>{
      const range=document.createRange();range.selectNodeContents(el);
      const text=range.getBoundingClientRect(),pane=el.parentElement.getBoundingClientRect();
      return {textTop:text.top,paneTop:pane.top,textHeight:text.height};
    }""")
    _app_eval(page, """
      window.__finishSettingsRead(window.__settingsFixture);
      await window.__settingsOpen; app._settingsRead=window.__settingsReadOriginal;
    """)
    assert geometry["textHeight"] > 0
    assert geometry["textTop"] >= geometry["paneTop"], geometry


def test_settings_timeout_is_explained_and_retry_recovers(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      app.setLang('zh');
      const original=window.fetch;
      const oldTimeout=app.SETTINGS_READ_TIMEOUT_MS;
      app.SETTINGS_READ_TIMEOUT_MS=30;
      window.fetch=(url,options)=>{
        if(String(url)==='/api/settings')return new Promise((resolve,reject)=>{
          options.signal.addEventListener('abort',()=>{
            reject(new DOMException('signal is aborted without reason','AbortError'));
          },{once:true});
        });
        return original(url,options);
      };
      try {
        await app.openSettings('general');
        const error=app.settings.error, loading=app.settings.loading;
        window.fetch=original;
        await app.openSettings('general');
        return {error,loading,recovered:!app.settings.error&&!app.settings.loading};
      } finally {window.fetch=original;app.SETTINGS_READ_TIMEOUT_MS=oldTimeout;}
    """)
    assert "超时" in result["error"], result
    assert "abort" not in result["error"].lower()
    assert not result["loading"] and result["recovered"]


def test_started_queue_row_is_not_readded_by_late_acceptance(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      const original=app._fetchWithDeadline,sync=app._syncQueueFromServer,attach=app._attachToServerTurn;
      const item={id:'q-owned-command',text:'same fixture',delivery:'queue'};
      app._syncQueueFromServer=async()=>{};
      app._attachToServerTurn=()=>{};
      app._fetchWithDeadline=async()=>new Response(JSON.stringify({
        ok:true,item,queue:{items:[item],revision:1},effective_delivery:'queue'
      }));
      try {
        app._installActiveTurnUser(st,'owned-turn','same fixture',[],[],'q-owned-command');
        await app._enqueueMessage(sid,{text:'same fixture',delivery:'queue',_submitToken:'owned-command'});
        return {queue:app.queueDisplayItems(st).length,
          owners:st.messages.filter(m=>m._queueItemId==='q-owned-command').length};
      } finally {
        app._fetchWithDeadline=original;app._syncQueueFromServer=sync;app._attachToServerTurn=attach;
        app._disposeSessionSync(st);
      }
    """)
    assert result == {"queue": 0, "owners": 1}


@pytest.mark.parametrize("background", [0, 1])
def test_finishing_probe_keeps_direct_admission_without_hiding_background_work(page, backend_url, auth_token, background):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid),original=app._fetchWithDeadline;
      st.pendingQueue=[];
      app._fetchWithDeadline=async()=>new Response(JSON.stringify({
        active:true,finishing:true,turn_id:'finished-fixture',background_tasks_pending:arg
      }));
      try {return await app._probeSessionBusy(sid,st);}
      finally {app._fetchWithDeadline=original;}
    """, background)
    assert result is bool(background)
