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


def test_uncertain_delivery_accepts_new_input_and_keeps_original_attachments(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId, st=app._ensureTabState(sid);
      const original=app._submissionReceipt, schedule=app._scheduleOutgoing;
      app._submissionReceipt=async()=>({state:'unknown'});
      app._scheduleOutgoing=()=>{};
      try {
        st._queueAdmission={images:[{id:'image-fixture',mime:'image/png'}],
          docs:[{id:'document-fixture',name:'fixture.txt',kind:'text'}],
          pendingQuotes:[{id:'quote-fixture',path:'/fixture/source.md',text:'quoted fixture'}]};
        app._rememberUncertainSubmission(sid,'uncertain-fixture','turn','original fixture');
        app.input='new draft';
        const sent=await app.send();
        const originalRecord=st._outgoing.find(r=>r.requestId==='uncertain-fixture');
        return {sent,input:app.input,uncertain:!!st._uncertainSubmission,
          claimed:!!st._composerSubmitToken,images:originalRecord.pendingImages.map(i=>i.id),
          docs:originalRecord.pendingDocs.map(i=>i.id),quotes:originalRecord.pendingQuotes.map(i=>i.id),
          inputs:st._outgoing.map(r=>r.input)};
      } finally {clearTimeout(st._outgoingTimer);app._submissionReceipt=original;app._scheduleOutgoing=schedule;}
    """)
    assert result["sent"] is True and not result["claimed"] and result["uncertain"]
    assert result["input"] == ""
    assert result["inputs"] == ["original fixture", "new draft"]
    assert result["images"] == ["image-fixture"] and result["docs"] == ["document-fixture"]
    assert result["quotes"] == ["quote-fixture"]


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


@pytest.mark.parametrize("delay_ms", [0, 50, 200, 1000])
def test_send_then_stop_has_one_owner_and_no_outbox_bounce(page, backend_url, auth_token, delay_ms):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId, st=app._ensureTabState(sid);
      const names=['_fetchWithDeadline','_confirmSessionBusy','_ensureChatMux','_checkActiveTurn',
        'loadSession','_syncQueueFromServer','_scheduleCanonicalStreamReload'];
      const originals=Object.fromEntries(names.map(name=>[name,app[name]]));
      const originalFetch=window.fetch;
      let receipt={state:'pending',result:{}}, starts=0, cancels=0, interrupts=0;
      let channel;
      const input='Synthetic rapid stop fixture';
      app._confirmSessionBusy=async()=>false;
      app._ensureChatMux=async()=>true;
      app._checkActiveTurn=async()=>false;
      app._syncQueueFromServer=async()=>true;
      app._scheduleCanonicalStreamReload=()=>{};
      app.loadSession=async()=>true;
      app._fetchWithDeadline=async(url,opts,timeout)=>{
        if(url==='/api/chat/turns/start') {
          starts++;
          await new Promise((resolve,reject)=>{
            setTimeout(resolve,400);
            opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')), {once:true});
          });
          if(receipt.state==='cancelled') return new Response('{}',{status:409});
          receipt={state:'accepted',result:{accepted:true,turn_id:'rapid-stop-turn',started_at:1}};
          return new Response(JSON.stringify(receipt.result));
        }
        if(url.includes('/submissions/')) {
          if(url.split('?')[0].endsWith('/cancel')) {cancels++;receipt={state:'cancelled',result:receipt.result};}
          return new Response(JSON.stringify(receipt));
        }
        return originals._fetchWithDeadline.call(app,url,opts,timeout);
      };
      window.fetch=async(url,opts)=>{
        if(String(url).includes('/interrupt')) {
          interrupts++;
          return new Response(JSON.stringify({active:false,turn_id:'rapid-stop-turn'}));
        }
        if(String(url).endsWith('/active')) return new Response(JSON.stringify({active:false}));
        return originalFetch(url,opts);
      };
      app.input=input;
      st.streaming=false;st.es=null;st.activeTurnId='';st.pendingQueue=[];
      let maxCopies=0;
      const sample=()=>{
        const outbox=app.queueDisplayItems(st).filter(q=>q.displayText===input).length;
        const history=st.messages.filter(m=>m.role==='user'&&m.text===input).length;
        maxCopies=Math.max(maxCopies,outbox+history);
      };
      const timer=setInterval(sample,5);
      try {
        const sending=app.send();
        await new Promise(resolve=>setTimeout(resolve,arg));
        await app.stop();
        await sending;
        await new Promise(resolve=>setTimeout(resolve,100));
        sample();channel=st.es;
        return {starts,cancels,interrupts,maxCopies,
          pending:app.queueDisplayItems(st).length, uncertain:!!st._uncertainSubmission,
          submitting:!!st._composerSubmitToken, streaming:st.streaming,
          draft:app.input, users:st.messages.filter(m=>m.role==='user'&&m.text===input).length};
      } finally {
        clearInterval(timer);
        if(channel) channel.close();
        for(const [name,value] of Object.entries(originals)) app[name]=value;
        window.fetch=originalFetch;
        app._disposeSessionSync(st);
      }
    """, delay_ms)
    assert result["maxCopies"] <= 1, result
    assert result["pending"] == 0 and not result["uncertain"] and not result["submitting"], result
    assert not result["streaming"], result
    if delay_ms < 400:
        assert result["users"] == 0 and result["draft"] == "Synthetic rapid stop fixture", result
    else:
        assert result["starts"] == 1 and result["interrupts"] == 1 and result["users"] == 1, result


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


def test_settings_themes_outbox_identity_and_mobile_geometry(page, backend_url, auth_token, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setLang('zh'); await app.openSettings('service');")
    expect(page.locator(".settings-modal")).to_be_visible()
    page.screenshot(animations="disabled", path=str(tmp_path / "settings-service-desktop.png"))
    _app_eval(page, """
      app.settings.show=false;
      const st=app._ensureTabState(app.currentId);
      st.pendingQueue=[{id:'visual-q1',displayText:'第一条待发送消息',text:'第一条待发送消息',
        images:[],docs:[],pendingQuotes:[],delivery:'queue',deliveryStatus:'queued',enqueuedAt:1},
        {id:'visual-q2',displayText:'第二条：'+'长内容 '.repeat(160),text:'long fixture',
        images:[],docs:[],pendingQuotes:[],delivery:'queue',deliveryStatus:'queued',enqueuedAt:2}];
      st._queuePaused=true;
    """)
    expect(page.locator(".queue-outbox")).to_be_visible()
    assert page.locator(".chat-body .msg.queued").count() == 0
    first = page.locator(".queue-outbox .msg.queued").first.element_handle()
    _app_eval(page, "const st=app._ensureTabState(app.currentId); st.pendingQueue=[...st.pendingQueue];")
    assert page.locator(".queue-outbox .msg.queued").first.evaluate("(node, original)=>node===original", first)
    for theme in ("dark", "light", "eyecare"):
        _app_eval(page, "app.setTheme(arg);", theme)
        contrast = page.locator(".queue-outbox .queued-text").first.evaluate("""node=>{
          const luminance=color=>{
            const rgb=color.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{
              const s=x/255;return s<=0.04045?s/12.92:((s+0.055)/1.055)**2.4;
            });
            return rgb[0]*0.2126+rgb[1]*0.7152+rgb[2]*0.0722;
          };
          const foreground=luminance(getComputedStyle(node).color);
          const background=luminance(getComputedStyle(node.closest('.bubble')).backgroundColor);
          return (Math.max(foreground,background)+0.05)/(Math.min(foreground,background)+0.05);
        }""")
        assert contrast >= 4.5, (theme, contrast)
    _app_eval(page, "app.setTheme('light');")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(animations="disabled", path=str(tmp_path / "outbox-mobile.png"))
    composer = page.locator(".chat-input-textarea:visible").bounding_box()
    outbox = page.locator(".queue-outbox").bounding_box()
    assert composer and outbox and composer["y"] >= outbox["y"] + outbox["height"], (composer, outbox)
    assert composer["y"] + composer["height"] <= 844, composer
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    page.locator(".queue-outbox-toggle").click()
    expect(page.locator(".queue-outbox-items")).not_to_be_visible()
    _app_eval(page, "await app.openMemoryCenter();")
    expect(page.locator(".settings-modal")).to_be_visible()
    expect(page.locator(".memory-center-section")).to_be_visible()
    page.screenshot(animations="disabled", path=str(tmp_path / "memory-mobile.png"))
    assert errors == []


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
