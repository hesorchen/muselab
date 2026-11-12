"""A running turn's live view recovers without a page refresh.

When the live stream had to fall back to canonical history (a resync, or the
reconnect budget ran out), the client waited for the turn to END before
loading anything. A long turn, or one waiting on a question to the user,
stayed frozen until a manual refresh. It now reattaches to the running turn.
Background reads share one connection so they cannot take the slots that
sending and stream recovery need.
"""
import json

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login

_SETUP = """
const sid = app.currentId, st = app._ensureTabState(sid);
window.__sends = [];
app.send = async (opts) => { window.__sends.push(opts || {}); return true; };
app.loadSession = async () => true;
st.streaming = true; st.activeTurnId = 'turn-live'; st.es = null;
st._streamStartedAt = Date.now() - 600000;
st._canonicalResyncPending = true; st._canonicalResyncReason = 'live_backlog';
st.sessionSync.canonicalStartedAt = Date.now() - arg;
st.sessionSync.canonicalRetryN = 0;
return sid;
"""


def _route_active(page, sid):
    page.route(f"**/api/chat/sessions/{sid}/active*", lambda route: route.fulfill(
        content_type="application/json",
        body=json.dumps({"active": True, "turn_id": "turn-live", "started_at": 1})))


def test_running_turn_reattaches_after_a_short_wait(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    _route_active(page, sid)
    _app_eval(page, _SETUP, 31_000)
    _app_eval(page, "app._requestSessionSync(app.currentId, 'canonical_replay');")
    page.wait_for_function("() => window.__sends.length > 0", timeout=8000)
    sent = _app_eval(page, "return window.__sends[0];")
    assert sent["reconnect"] is True and sent["turnId"] == "turn-live"
    assert _app_eval(page, "return app.tabState[app.currentId]._canonicalResyncPending;") is False


def test_footer_says_updates_paused_and_reconnect_reattaches_now(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid = _app_eval(page, "return app.currentId;")
    _route_active(page, sid)
    _app_eval(page, _SETUP, 0)
    _app_eval(page, """
const st = app.tabState[app.currentId];
st.messages.splice(0, st.messages.length,
  {role: 'user', text: 'go', _turnId: 'turn-live', _turnRoot: true, _k: 'u'},
  {role: 'assistant', text: 'working', _k: 'a'});
app.sessions = app.sessions.map(s => s.id === app.currentId
  ? {...s, active: true, turn_active: true} : s);
// Keep the fallback waiting: the turn is young and nothing is due yet.
app._requestSessionSync = (() => { const orig = app._requestSessionSync.bind(app);
  return (sid, reason, opts = {}) => reason === 'canonical_replay' && !opts.forceReattach
    ? Promise.resolve(false) : orig(sid, reason, opts); })();
""")
    note = page.locator(".msg-pane:visible .turn-live-recovery:visible")
    expect(note).to_contain_text("重新同步" if _app_eval(page, "return app.lang;") == "zh"
                                 else "resyncing")
    note.locator(".turn-live-reconnect").click()
    page.wait_for_function("() => window.__sends.length > 0", timeout=8000)
    assert _app_eval(page, "return window.__sends[0].reconnect;") is True


def test_background_reads_wait_for_one_slot_and_others_do_not(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
const realFetch = window.fetch;
let inFlight = 0, peak = 0; const release = [];
window.fetch = (url, opts) => {
  if (!String(url).includes('/__slot-probe')) return realFetch(url, opts);
  inFlight += 1; peak = Math.max(peak, inFlight);
  return new Promise(resolve => release.push(() => { inFlight -= 1;
    resolve(new Response('{}', {status: 200})); }));
};
const background = [1, 2, 3].map(i => app._fetchWithDeadline(
  '/__slot-probe/bg' + i, {lowPriority: true}, 20000));
await new Promise(r => setTimeout(r, 100));
const peakBackground = peak;
const urgent = app._fetchWithDeadline('/__slot-probe/send', {}, 20000);
await new Promise(r => setTimeout(r, 100));
const withUrgent = inFlight;
while (release.length) { release.shift()(); await new Promise(r => setTimeout(r, 50)); }
await Promise.all([...background, urgent]);
window.fetch = realFetch;
return {peakBackground, withUrgent, total: peak};
""")
    assert result["peakBackground"] == 1
    assert result["withUrgent"] == 2  # the urgent request did not queue
