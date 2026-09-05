"""Browser contracts for non-blocking outbox review records."""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_review_records_are_visible_but_do_not_make_idle_session_busy(
    page, backend_url, auth_token,
):
    _login(page, backend_url, auth_token)
    page.route("**/api/chat/sessions/*/queue", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "revision": 100, "paused": True,
            "items": [{
                "id": "review-item", "text": "Synthetic uncertain input",
                "queue_issue": "delivery_unknown", "steering_state": "cancelled",
            }],
        })))
    result = _app_eval(page, """
      app.setLang('zh');
      const sid=app.currentId, st=app._ensureTabState(sid);
      st.streaming=false;st.backgroundActive=false;st.compacting=false;
      st._draining=false;st._outboxCollapsed=false;st._queueAdmission=null;
      await app._syncQueueFromServer(sid);
      return {busy:app._isBusy(sid), count:app._currentQueueLen(),
        paused:st._queuePaused, canAdjust:app._pendingQueueAllowsAdjustment(st),
        label:app.queueOutboxLabel(st)};
    """)
    assert result == {
        "busy": False, "count": 0, "paused": False, "canAdjust": True,
        "label": "待发送 0 · 需处理 1",
    }
    expect(page.locator(".queued-label")).to_have_text("送达状态待确认")
    expect(page.locator(".queued-review-hint")).to_be_visible()
    assert page.locator(".queue-paused-banner").count() == 0
    assert page.locator(".queued-paused-badge").count() == 0
    expect(page.locator(".queue-outbox")).not_to_contain_text("继续执行")


def test_cancellation_event_only_changes_the_matching_item(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const sid=app.currentId, st=app._ensureTabState(sid);
      const original=app._syncQueueFromServer;
      app._syncQueueFromServer=async()=>{};
      try {
        st._queuePaused=false;st._queueAdmission=null;
        st.pendingQueue=[
          {id:'cancel-one',text:'cancel',delivery:'adjust',deliveryStatus:'queued'},
          {id:'keep-two',text:'keep',delivery:'queue',deliveryStatus:'queued'}
        ];
        app._applyQueueSteeringEvent(sid,{item_id:'cancel-one',state:'cancelled'});
        return {paused:st._queuePaused,
          pending:app.queuePendingItems(st).map(item=>item.id),
          issues:st.pendingQueue.map(item=>app.queueItemIssue(item))};
      } finally {app._syncQueueFromServer=original;}
    """)
    assert result == {
        "paused": False, "pending": ["keep-two"], "issues": ["cancelled", ""],
    }


def test_filtered_outbox_actions_use_identity_not_display_index(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    requests = []

    def handle(route):
        if route.request.method == "DELETE":
            requests.append(route.request.url.rsplit("/", 1)[-1])
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/chat/sessions/*/queue/*", handle)
    _app_eval(page, """
      const sid=app.currentId, st=app._ensureTabState(sid);
      st.pendingQueue=[
        {id:'already-promoted',text:'hidden',delivery:'adjust',commandUuid:'promoted-uuid'},
        {id:'visible-review',text:'visible review',displayText:'visible review',queueIssue:'failed'}
      ];
      st.messages=[{role:'user',text:'hidden',uuid:'promoted-uuid',_queueItemId:'already-promoted'}];
      st._outboxCollapsed=false;
    """)
    expect(page.locator(".queued-label")).to_have_count(1)
    page.locator('.queued-act[title="Remove"], .queued-act[title="移除"]').click()
    assert requests == ["visible-review"]


@pytest.mark.parametrize("width", [1440, 1920])
def test_file_header_actions_align_to_right_of_wide_header(page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 1000})
    _login(page, backend_url, auth_token)
    geometry = page.locator(".files-head-workspace").evaluate("""workspace => {
      const header=workspace.parentElement;
      header.style.width='560px';
      const action=header.querySelector('.files-theme-toggle');
      const h=header.getBoundingClientRect(), a=action.getBoundingClientRect();
      return {gap:h.right-a.right, topGap:Math.abs(h.top+h.height/2-a.top-a.height/2)};
    }""")
    assert 8 <= geometry["gap"] <= 18
    assert geometry["topGap"] <= 1
