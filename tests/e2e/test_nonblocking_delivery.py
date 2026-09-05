"""Browser acceptance for a composer independent of transport receipts."""
from playwright.sync_api import expect
from .test_chat_render_perf import _app_eval, _login


def test_repeated_send_during_admission_is_saved_and_cancellable(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId, st=app._ensureTabState(sid);
      st._composerSubmitToken='slow-primary';
      app.input='first additional input'; const first=await app.send();
      app.input='second additional input'; const second=await app.send();
      const saved=JSON.parse(localStorage.getItem('muselab_outgoing_'+sid));
      const ids=st._outgoing.map(r=>r.requestId);
      app.input='a new editable draft'; app._captureComposerState(sid);
      const disabled=app.composerDisabledReason(sid);
      await app.cancelOutgoing(sid,ids[0]);
      clearTimeout(st._outgoingTimer);
      return {first,second,saved:saved.map(r=>r.input),remaining:st._outgoing.map(r=>r.input),
        draft:st.draft.input,disabled,distinct:ids[0]!==ids[1]};
    """)
    assert result == {"first": True, "second": True,
        "saved": ["first additional input", "second additional input"],
        "remaining": ["second additional input"], "draft": "a new editable draft",
        "disabled": "", "distinct": True}
    expect(page.locator(".chat-toolbar-queue")).to_be_enabled()


def test_uncertain_delivery_does_not_lock_send_or_duplicate_inline_bubble(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      app._appendLiveMessage(st,{role:'user',text:'old fixture',_clientMessageId:'lost-receipt'});
      app._rememberUncertainSubmission(sid,'lost-receipt','turn','old fixture');
      st._composerSubmitToken='fixture-hold';
      app.input='new input'; const accepted=await app.send();
      clearTimeout(st._outgoingTimer);
      return {accepted,draft:st.draft.input,records:st._outgoing.length,
        duplicateOld:app.queueDisplayItems(st).some(q=>q._submitToken==='lost-receipt'),
        confirmation:document.body.innerText.includes('请先确认上一条消息')};
    """)
    assert result == {"accepted": True, "draft": "", "records": 2, "duplicateOld": False, "confirmation": False}


def test_lost_ack_is_recovered_without_reposting(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st._composerSubmitToken='fixture-hold';app.input='one execution';await app.send();
      clearTimeout(st._outgoingTimer);st._composerSubmitToken='';
      let posts=0,accepted=false;
      app._postSubmission=async()=>{posts++;accepted=true;throw new TypeError('connection lost');};
      app._submissionReceipt=async()=>accepted?{state:'accepted',result:{ok:true}}:{state:'not_found'};
      app._syncQueueFromServer=async()=>true;app._checkActiveTurn=async()=>{};
      app.loadSession=async()=>{};
      await app._pumpOutgoing(sid); const retained=st._outgoing.length;
      clearTimeout(st._outgoingTimer);
      await app._pumpOutgoing(sid);clearTimeout(st._outgoingTimer);
      return {posts,retained,remaining:st._outgoing.length};
    """)
    assert result == {"posts": 1, "retained": 1, "remaining": 0}


def test_manual_pause_preserves_new_input_and_refresh_recovery(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st._composerSubmitToken='fixture-hold';
      app.input='old pending';await app.send();
      app._fetchWithDeadline=async()=>new Response('{}');
      app._syncQueueFromServer=async()=>true;
      await app.pausePendingQueue(sid,true);
      app.input='new manual input';await app.send();
      clearTimeout(st._outgoingTimer);
      st._outgoing=[];st._outgoingLoaded=false;app._restoreOutgoing(sid,st);
      clearTimeout(st._outgoingTimer);
      return st._outgoing.map(r=>({input:r.input,held:!!r.held}));
    """)
    assert result == [{"input": "old pending", "held": True}, {"input": "new manual input", "held": False}]


def test_cancel_while_ack_is_in_flight_keeps_cancellation_intent(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st._composerSubmitToken='fixture-hold';app.input='cancel race';await app.send();
      clearTimeout(st._outgoingTimer);st._composerSubmitToken='';
      let release,cancels=0;
      app._postSubmission=()=>new Promise(resolve=>release=resolve);
      app._syncQueueFromServer=async()=>true;
      app._checkActiveTurn=async()=>{};app.loadSession=async()=>{};
      const sending=app._pumpOutgoing(sid);
      await new Promise(r=>setTimeout(r,0));
      await app.cancelOutgoing(sid,st._outgoing[0].requestId);
      release(new Response(JSON.stringify({ok:true})));await sending;
      clearTimeout(st._outgoingTimer);
      const retained=st._outgoing[0]?.cancelRequested;
      app._fetchWithDeadline=async()=>{cancels++;return new Response(JSON.stringify({state:'cancelled',result:{}}));};
      await app._pumpOutgoing(sid);clearTimeout(st._outgoingTimer);
      return {retained,cancels,remaining:st._outgoing.length};
    """)
    assert result == {"retained": True, "cancels": 1, "remaining": 0}


def test_stop_accepts_new_input_before_control_ack_and_holds_only_old_ids(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st.activeTurnId='stop-owner';st.streaming=true;st.es={close(){}};
      st.pendingQueue=[{id:'q-old',text:'old pending'}];
      let release,stopUrl='';
      const original=window.fetch;
      window.fetch=(url,opts)=>{
        if(String(url).includes('/api/chat/interrupt')){
          stopUrl=String(url);return new Promise(resolve=>release=resolve);
        }
        return original(url,opts);
      };
      app._syncQueueFromServer=async()=>true;app._syncSessionListQuiet=()=>{};
      app.refreshSessions=()=>{};
      const stopping=app.stop();
      app.input='new during stop';const accepted=await app.send();
      const held=st.pendingQueue[0].held,newHeld=!!st._outgoing[0].held;
      clearTimeout(st._outgoingTimer);
      release(new Response(JSON.stringify({active:true,turn_id:'stop-owner',stopping:true})));
      await stopping;window.fetch=original;
      return {accepted,held,newHeld,oldInSnapshot:decodeURIComponent(stopUrl).includes('q-old'),
        newInSnapshot:decodeURIComponent(stopUrl).includes(st._outgoing[0].requestId)};
    """)
    assert result == {"accepted": True, "held": True, "newHeld": False,
        "oldInSnapshot": True, "newInSnapshot": False}


def test_storage_failure_keeps_draft_and_does_not_claim_acceptance(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st._composerSubmitToken='fixture-hold';app.input='keep this input';
      app._persistOutgoing=()=>false;
      const accepted=await app.send();
      return {accepted,draft:st.draft.input,count:st._outgoing.length};
    """)
    assert result == {"accepted": False, "draft": "keep this input", "count": 0}
