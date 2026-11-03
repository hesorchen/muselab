"""A queued message that is being cancelled must look and behave cancelled."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login

CANCELLING = re.compile(r"\bis-cancelling\b")

_SEED = """
app.setLang('zh');
const sid = app.currentId, st = app._ensureTabState(sid);
app._syncQueueFromServer = async () => {};
st._queuePaused = false; st._queueAdmission = null; st._outboxCollapsed = false;
st._queueMutating = {};
st.pendingQueue = [
  {id: 'q-first', text: 'FIRST queued', displayText: 'FIRST queued', delivery: 'queue', deliveryStatus: 'queued'},
  {id: 'q-second', text: 'SECOND queued', displayText: 'SECOND queued', delivery: 'queue', deliveryStatus: 'queued'},
];
return sid;
"""


def _bubble(page, text):
    return page.locator(".queued-bubble").filter(has_text=text)


def test_cancelling_item_is_dimmed_busy_and_not_actionable(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, _SEED)
    first, second = _bubble(page, "FIRST queued"), _bubble(page, "SECOND queued")
    expect(first).to_be_visible()
    expect(first).not_to_have_class(CANCELLING)
    expect(first.locator(".queued-act").nth(1)).to_be_enabled()

    # Removing the first item: only that row turns into a busy, inert row.
    _app_eval(page, "const st = app._ensureTabState(app.currentId);"
                    "st._queueMutating = {'remove:q-first': true}; return true;")
    expect(first).to_have_class(CANCELLING)
    expect(first).to_have_attribute("aria-busy", "true")
    expect(first.locator(".queued-label")).to_have_text("正在取消")
    expect(first.locator(".queued-act").nth(0)).to_be_disabled()
    expect(first.locator(".queued-act").nth(1)).to_be_disabled()
    remove = first.locator(".queued-act").nth(1)
    expect(remove.locator(".spinner-sm")).to_be_visible()
    expect(remove.locator("svg")).to_be_hidden()
    assert float(first.locator(".queued-text").evaluate(
        "el => getComputedStyle(el).opacity")) <= 0.55
    expect(second).not_to_have_class(CANCELLING)
    expect(second.locator(".queued-act").nth(1)).to_be_enabled()

    # Clearing the whole list marks every row and relabels the button.
    _app_eval(page, "const st = app._ensureTabState(app.currentId);"
                    "st._queueMutating = {discard: true}; return true;")
    expect(first).to_have_class(CANCELLING)
    expect(second).to_have_class(CANCELLING)
    clear = page.locator(".queue-outbox-actions button")
    expect(clear).to_have_text("正在清空…")
    expect(clear).to_be_disabled()

    _app_eval(page, "const st = app._ensureTabState(app.currentId);"
                    "st._queueMutating = {}; return true;")
    expect(first).not_to_have_class(CANCELLING)
    expect(clear).to_have_text("清空列表")
    expect(clear).to_be_enabled()
