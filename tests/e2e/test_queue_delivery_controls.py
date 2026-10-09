"""Browser behavior for queue acknowledgements and exact-turn adjustment intent."""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_successful_removal_updates_outbox_when_refresh_is_unavailable(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/api/chat/sessions/*/queue", lambda route: route.fulfill(status=503, body="{}"))
    page.route("**/api/chat/sessions/*/queue/withdraw-one", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"revision": 101, "items": [], "inflight": None}),
    ))
    _app_eval(page, """
      app.setLang('zh');
      const st=app._ensureTabState(app.currentId);
      st._outboxCollapsed=false;st._queueAdmission=null;st._queueRevision=100;
      st.pendingQueue=[{id:'withdraw-one',text:'Synthetic pending message',delivery:'queue'}];
    """)
    expect(page.locator(".queued-label")).to_have_count(1)
    page.locator('.queued-act[title="移除"]').click()
    expect(page.locator(".queued-label")).to_have_count(0)


def test_cancel_wait_has_visible_feedback_and_preserves_message_until_ack(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    pending = []
    page.route("**/api/chat/sessions/*/queue/withdraw-one", lambda route: pending.append(route))
    _app_eval(page, """
      app.setLang('zh');
      const st=app._ensureTabState(app.currentId);
      st._outboxCollapsed=false;st._queueAdmission=null;st._queueRevision=100;
      st.pendingQueue=[{id:'withdraw-one',text:'Synthetic pending message',delivery:'queue'}];
    """)
    button = page.locator('.queued-act[title="移除"]')
    button.click()
    expect(button).to_be_disabled()
    expect(page.locator(".queued-label")).to_have_text("正在取消")
    assert len(pending) == 1
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({
        "revision": 101, "items": [], "inflight": None,
    }))
    expect(page.locator(".queued-label")).to_have_count(0)


@pytest.mark.parametrize("owner_changed", [False, True])
def test_rapid_followup_preserves_adjustment_only_for_original_stream(page, backend_url, auth_token, owner_changed):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid),sent=[];
      const saved={};
      for(const key of ['_scheduleOutgoing','_postSubmission','_syncQueueFromServer','_checkActiveTurn']) saved[key]=app[key];
      app._scheduleOutgoing=()=>{};
      app._postSubmission=async(owner,id,kind,payload)=>{sent.push({...payload});return {ok:true,json:async()=>({})};};
      app._syncQueueFromServer=async()=>true;app._checkActiveTurn=async()=>{};
      try {
        app.busySendMode='adjust';st.streaming=true;st.compacting=false;st.backgroundActive=false;
        st.parentTurnId='';st._draining=false;st._stoppingTurnId='';st._queueAdmission=null;
        st.pendingQueue=[];st._outgoing=[];
        st._composerSubmitToken='previous-adjustment';st._streamOwnerToken='original-stream';st.activeTurnId='';
        st.draft.input='Synthetic follow-up';st.draft.pendingImages=[];st.draft.pendingDocs=[];st.draft.pendingQuotes=[];
        const accepted=app._submitWhileBusy(sid,st);
        st._composerSubmitToken='';st.activeTurnId=arg?'successor-turn':'original-turn';
        if(arg) st._streamOwnerToken='successor-stream';
        await app._pumpOutgoing(sid);
        return {accepted,delivery:sent[0]?.delivery,turnId:sent[0]?.active_turn_id};
      } finally {
        Object.assign(app,saved);st._composerSubmitToken='';st.streaming=false;
        app._persistOutgoing(sid,st);
      }
    """, owner_changed)
    assert result == {"accepted": True, "delivery": "queue" if owner_changed else "adjust",
                      "turnId": "" if owner_changed else "original-turn"}


def test_removal_receipt_preserves_attachments_and_rejects_stale_snapshot(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/api/chat/sessions/*/queue/withdraw-one", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "revision": 101, "items": [{"id": "keep-image", "text": "synthetic image",
                                       "image_ids": "image-1"}],
        }),
    ))
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid);
      st._queueRevision=100;st._queueAdmission=null;
      st.pendingQueue=[{id:'withdraw-one',text:'withdraw'},
        {id:'keep-image',text:'synthetic image',image_ids:'image-1',
         images:[{id:'image-1',mime:'image/png',src:'data:image/png;base64,'}],docs:[]}];
      await app.removePendingQueueItem(sid,'withdraw-one');
      const applied=app._applyQueueSnapshot(sid,{revision:100,items:[{id:'withdraw-one',text:'stale'}]});
      return {ids:st.pendingQueue.map(q=>q.id),image:st.pendingQueue[0]?.images?.[0]?.id,
              revision:st._queueRevision,staleApplied:!!applied};
    """)
    assert result == {"ids": ["keep-image"], "image": "image-1", "revision": 101, "staleApplied": False}


@pytest.mark.parametrize("failure", ["conflict", "network"])
def test_failed_withdrawal_preserves_row_and_explains_outcome(page, backend_url, auth_token, failure):
    _login(page, backend_url, auth_token)
    page.route("**/api/chat/sessions/*/queue", lambda route: route.fulfill(status=503, body="{}"))
    page.route("**/api/chat/sessions/*/queue/withdraw-one", lambda route: (
        route.abort() if failure == "network" else route.fulfill(status=409, body="{}")))
    result = _app_eval(page, """
      const sid=app.currentId,st=app._ensureTabState(sid),messages=[],original=app.toast;
      app.setLang('zh');app.toast=(text)=>messages.push(text);
      st.pendingQueue=[{id:'withdraw-one',text:'synthetic'}];
      try {
        await app.removePendingQueueItem(sid,'withdraw-one');
        return {count:st.pendingQueue.length,feedback:messages.join(' '),busy:app.queueActionBusy(sid,'remove:withdraw-one')};
      } finally {app.toast=original;}
    """)
    assert result["count"] == 1
    assert result["busy"] is False
    assert ("可能已开始处理" if failure == "conflict" else "结果待确认") in result["feedback"]
