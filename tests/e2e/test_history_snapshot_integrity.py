"""History replacement must not roll an already-visible final answer backward."""
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("case,accepted", [
    ("unstable", False), ("older", False), ("missing_boundary", False),
    ("shorter_without_boundary", False),
    ("compacted", True), ("current", True), ("live_count", True),
])
@pytest.mark.parametrize("quiet", [True, False])
def test_history_replacement_preserves_committed_tail(page, case, accepted, quiet):
    root = Path(__file__).resolve().parents[2]
    page.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<html><body></body></html>"))
    page.goto("http://history-fixture.invalid/")
    for script in ("i18n/index.js", "data/constants.js", "modules/task-delivery.js", "app.js"):
        page.add_script_tag(path=str(root / "frontend" / script))
    result = page.evaluate("""async ({caseName, quiet}) => {
      const app = portal();
      app.$nextTick = callback => Promise.resolve().then(callback);
      app.currentId = 'other-tab';
      for (const name of ['_scheduleHistoryViewport', '_syncSessionMessageStore',
          'hydrateSubagents', 'hydrateHookTraces', '_scheduleIdlePreload']) app[name] = () => {};
      let reason = '';
      app._reportHistoryLoadPerf = event => { reason = event.cancel_reason; };
      const sid = 'history-fixture';
      const st = app._ensureTabState(sid);
      st.messages = ['u1', 'a1', 'u2', 'a2'].map(uuid => ({
        uuid, role: uuid[0] === 'u' ? 'user' : 'assistant', text: uuid, _k: sid + ':' + uuid,
      }));
      st._loaded = true; st._seenUpdated = 200; st._installedCanonicalCount = 9; st._installedHistoryTotal = 4;
      st._lastTerminalTurnId = 'turn2'; st._lastTerminalAssistantUuid = 'a2';
      if (caseName === 'shorter_without_boundary') st._lastTerminalAssistantUuid = '';
      st.activeTurnId = 'turn2'; st.atBottom = true; st.runtimeUiRevision = 'new';
      Object.assign(st.messageRange, {visibleStart: 0, visibleEnd: 4, offset: 0,
        total: 4, order: 'normal', generation: 'generation1'});
      if (caseName === 'live_count') st.messageRange.total = 6;
      const incoming = ['current', 'live_count'].includes(caseName) ? st.messages : st.messages.slice(0, 2);
      const snapshot = {messages: incoming.map(message => ({...message})),
        total: caseName === 'missing_boundary' ? 4 : incoming.length, offset: 0, has_more: false, has_later: false,
        updated_at: caseName === 'older' ? 100 : 300, runtime_ui_revision: 'response',
        history_generation: caseName === 'compacted' ? 'generation2' : 'generation1',
        completion_state: {stable: caseName !== 'unstable', active: false,
          completed_turn_id: 'turn2'}};
      app._fetchWithDeadline = async (url, options, timeout, consume) => {
        const response = new Response(JSON.stringify(snapshot), {status: 200});
        if (consume) await consume(response);
        return response;
      };
      const loaded = await app.loadSession(sid, {quiet, probeActive: false});
      return {loaded, ids: st.messages.map(message => message.uuid), seen: st._seenUpdated,
        reason, range: [st.messageRange.visibleStart, st.messageRange.visibleEnd]};
    }""", {"caseName": case, "quiet": quiet})
    assert result["loaded"] is accepted, json.dumps(result)
    assert result["ids"] == (["u1", "a1"] if case == "compacted" else ["u1", "a1", "u2", "a2"])
    assert result["seen"] == (300 if accepted else 200)
    assert result["range"][1] > result["range"][0]


@pytest.mark.parametrize("enabled", [False, True])
def test_absolute_path_input_and_send_do_not_dispatch_slash(page, backend_url, auth_token, enabled):
    from tests.e2e.test_chat_render_perf import _app_eval, _login
    _login(page, backend_url, auth_token)
    _app_eval(page, """
      app.SLASH_ENABLED = arg;
      app._confirmSessionBusy = async () => false;
      app._submissionReceipt = async () => ({state: 'cancelled'});
      const gate = new Promise(resolve => window.releasePathSend = resolve);
      app._ensureChatMux = async () => {await gate; return false;};
      window.pathSlashCalls = 0;
      app._dispatchSlash = async () => {window.pathSlashCalls++; return false;};
    """, enabled)
    path = "/tmp/fixture-status.log inspect this file"
    page.locator('textarea[x-ref="chatInput"]').fill(path)
    assert _app_eval(page, "return app.slashShow;") is False
    result = _app_eval(page, """
      const sending = app.send();
      try {
        await new Promise(resolve => setTimeout(resolve, 100));
        const st = app._ensureTabState(app.currentId);
        return {slashCalls: window.pathSlashCalls,
          text: st.messages.filter(row => row.role === 'user').at(-1)?.text};
      } finally {
        window.releasePathSend(); await sending;
        app._disposeSessionSync(app._ensureTabState(app.currentId));
      }
    """)
    assert result == {"slashCalls": 0, "text": path}
