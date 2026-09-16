"""Final-history recovery must converge without a browser reload.

When the live UUID is missing, stable completed-turn identity can repair
incomplete text. A known UUID still requires its own canonical boundary.
Detached background work must not gate the foreground history. Keep these
rules covered alongside successor and quiet-rendering protections.
"""
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.e2e.test_chat_render_perf import (
    _app_eval,
    _assert_no_browser_errors,
    _capture_browser_errors,
    _login,
)


SID = "completion-visibility-fixture"
FINAL = "CANONICAL_FINAL_COMPLETE"


def _prepare(page, backend_url, auth_token, *, state=None, activity=None, history_failures=0):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    history = {
        "id": SID, "name": "Completion fixture", "model": "e2e-model",
        "permission": "bypassPermissions", "thinking": True,
        "messages": [
            {"role": "user", "text": "FIXTURE_PROMPT", "uuid": "fixture-user",
             "_turnRoot": True},
            {"role": "assistant", "text": FINAL, "uuid": "fixture-final"},
        ],
        "offset": 0, "total": 2, "message_count": 2, "pre_total": 0,
        "history_order": "normal", "history_generation": "fixture-generation",
        "has_more": False, "has_later": False, "updated_at": 2,
        "completion_state": {
            "stable": True, "active": False, "turn_id": "",
            "completed_turn_id": "fixture-turn", **(state or {}),
        },
    }
    reads = []

    def respond(route):
        nonlocal history_failures
        reads.append(route.request.url)
        if "?tail=" in route.request.url and history_failures > 0:
            history_failures -= 1
            route.fulfill(status=503, json={"detail": "fixture retry"})
            return
        route.fulfill(json=activity if route.request.url.endswith("/active") else history)

    page.route(f"**/api/chat/sessions/{SID}?*", respond)
    page.route(f"**/api/chat/sessions/{SID}/active", respond)
    _app_eval(page, """
        const sid = arg;
        app.refreshSessions = async () => {};
        app._syncSessionListQuiet = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app._checkActiveTurn = () => {};
        app._syncQueueFromServer = async () => {};
        app._drainPendingQueue = async () => {};
        app.sessions = [{id: sid, name: 'Completion fixture', message_count: 2}];
        app.openTabIds = [sid];
        app.tabState = {};
        app.currentId = sid;
        app.mobileTab = 'chat';
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.atBottom = true;
        st.messages.push(
          {role: 'user', text: 'FIXTURE_PROMPT', uuid: 'fixture-user',
           _turnRoot: true, _k: `${sid}:uuid:fixture-user`},
          {role: 'assistant', text: 'LIVE_PARTIAL', _k: `${sid}:live:partial`},
        );
        Object.assign(st.messageRange, {
          visibleStart: 0, visibleEnd: 2, offset: 0, total: 2,
          preTotal: 0, order: 'normal', generation: 'fixture-live',
        });
        app._activateTabState(sid);
        window.visibilityRequests = [];
        window.realVisibilityRequest = app._requestSessionSync.bind(app);
        app._requestSessionSync = (id, reason, options) => {
          window.visibilityRequests.push({id, reason, options});
          return Promise.resolve(false);
        };
        window.visibilityScrolls = [];
        const originalLoad = app.loadSession.bind(app);
        app.loadSession = async (id, options) => {
          window.visibilityScrolls.push(options.followTail === true);
          return originalLoad(id, options);
        };
    """, SID)
    return errors, history, reads


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("expected_uuid,expected_text", [
    ("", "LIVE_PARTIAL"), ("", ""),
])
def test_completed_identity_recovers_incomplete_live_reply(
    page, backend_url, auth_token, width, expected_uuid, expected_text,
):
    page.set_viewport_size({"width": width, "height": 900})
    errors, _, reads = _prepare(page, backend_url, auth_token)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st._userScrollAt = 12;
        const options = {
          expectedText: arg.text, expectedAssistantUuid: arg.uuid,
          completedTurnId: 'fixture-turn', followTail: true,
          followTailUserScrollAt: 11,
        };
        st._pendingCompletedTurnSync = options;
        const loaded = await app._runCompletedTurnSync(arg.sid, st, options);
        await new Promise(resolve => app.$nextTick(resolve));
        return {loaded, pending: st._pendingCompletedTurnSync,
          text: st.messages.at(-1).text, ready: st.messagesReady,
          loading: st.messagesLoading, requests: window.visibilityRequests.length,
          followTail: window.visibilityScrolls};
    """, {"sid": SID, "uuid": expected_uuid, "text": expected_text})
    assert result == {
        "loaded": True, "pending": None, "text": FINAL, "ready": True,
        "loading": False, "requests": 0, "followTail": [False],
    }
    assert len(reads) == 1
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("state,expected_uuid", [
    ({"stable": False}, ""),
    ({"active": True, "turn_id": "fixture-turn"}, ""),
    ({"active": True, "turn_id": "successor-turn"}, ""),
    ({"completed_turn_id": ""}, ""),
    ({}, "missing-live-uuid"),
])
def test_incomplete_live_reply_still_requires_stable_idle_commit(
    page, backend_url, auth_token, state, expected_uuid,
):
    errors, _, _ = _prepare(page, backend_url, auth_token, state=state)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        const loaded = await app._runCompletedTurnSync(arg.sid, st, {
          expectedText: 'LIVE_PARTIAL', completedTurnId: 'fixture-turn',
          expectedAssistantUuid: arg.uuid,
        });
        return {loaded, text: st.messages.at(-1).text,
          reasons: window.visibilityRequests.map(r => r.reason)};
    """, {"sid": SID, "uuid": expected_uuid})
    assert result == {"loaded": False, "text": "LIVE_PARTIAL", "reasons": ["completed_turn"]}
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("width", [1440, 390])
def test_replay_gap_loads_final_while_detached_background_is_active(
    page, backend_url, auth_token, width,
):
    page.set_viewport_size({"width": width, "height": 900})
    errors, _, reads = _prepare(page, backend_url, auth_token, activity={
        "active": True, "background": True, "attachable": False,
        "turn_id": "fixture-turn", "background_tasks_pending": 1,
    })
    result = _app_eval(page, """
        const st = app.tabState[arg];
        st._canonicalResyncPending = true;
        st.sessionSync.canonicalStartedAt = Date.now() - 5000;
        st.activeTurnId = 'fixture-turn';
        st.streaming = true;
        let closed = 0;
        st.es = {close: () => {closed += 1;}};
        const loaded = await app._runCanonicalReplaySync(arg, st);
        await new Promise(resolve => app.$nextTick(resolve));
        return {loaded, pending: st._canonicalResyncPending,
          text: st.messages.at(-1).text, streaming: st.streaming, closed,
          ready: st.messagesReady, loading: st.messagesLoading,
          background: st.backgroundActive,
          retries: window.visibilityRequests.filter(r => r.reason === 'canonical_replay').length};
    """, SID)
    assert result == {
        "loaded": True, "pending": False, "text": FINAL, "streaming": False,
        "closed": 1, "ready": True, "loading": False, "background": True,
        "retries": 0,
    }
    assert any("?tail=" in url for url in reads)
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("case", [
    "foreground", "attachable", "successor", "admission",
    "successor_during_probe", "stream_during_probe",
])
def test_replay_recovery_preserves_foreground_owner(
    page, backend_url, auth_token, case,
):
    errors, _, reads = _prepare(page, backend_url, auth_token, activity={
        "active": True, "background": case != "foreground",
        "attachable": case == "attachable", "turn_id": "fixture-turn",
    })
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st._canonicalResyncPending = true;
        st.sessionSync.canonicalStartedAt = Date.now() - 5000;
        st.activeTurnId = arg.testCase === 'successor' ? 'successor-turn' : 'fixture-turn';
        st._composerSubmitToken = arg.testCase === 'admission' ? 'new-submit' : null;
        st.streaming = true;
        let closed = 0;
        st.es = {close: () => {closed += 1;}};
        const originalFetch = app._fetchWithDeadline.bind(app);
        app._fetchWithDeadline = async (...args) => {
          const response = await originalFetch(...args);
          if (arg.testCase === 'successor_during_probe') {
            st.activeTurnId = 'successor-turn';
          }
          if (arg.testCase === 'stream_during_probe') {
            st.es = {close: () => {closed += 1;}};
          }
          return response;
        };
        await app._runCanonicalReplaySync(arg.sid, st);
        return {text: st.messages.at(-1).text, streaming: st.streaming, closed,
          pending: st._canonicalResyncPending,
          retries: window.visibilityRequests.filter(r => r.reason === 'canonical_replay').length};
    """, {"sid": SID, "testCase": case})
    assert result == {
        "text": "LIVE_PARTIAL", "streaming": True, "closed": 0,
        "pending": True, "retries": 1,
    }
    assert len(reads) == 1 and reads[0].endswith("/active")
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("mode", ["completed", "replay", "replay_retry"])
def test_final_visibility_converges_through_real_session_scheduler(
    page, backend_url, auth_token, mode,
):
    errors, _, reads = _prepare(page, backend_url, auth_token, activity={
        "active": True, "background": True, "attachable": False,
        "turn_id": "fixture-turn", "background_tasks_pending": 1,
    }, history_failures=int(mode == "replay_retry"))
    _app_eval(page, """
        app._requestSessionSync = window.realVisibilityRequest;
        const st = app.tabState[arg.sid];
        if (arg.mode === 'completed') {
          app._reconcileCompletedTurn(
            arg.sid, st, 'LIVE_PARTIAL', 0, '', 'fixture-turn');
        } else {
          st.activeTurnId = 'fixture-turn';
          st.streaming = true;
          st.es = {close: () => {}};
          app._scheduleCanonicalStreamReload(arg.sid, st);
        }
    """, {"sid": SID, "mode": mode})
    pane = page.locator(f'.msg-pane[data-tid="{SID}"]')
    expect(pane).to_contain_text(FINAL, timeout=10000)
    expect(pane).to_contain_text("FIXTURE_PROMPT")
    result = _app_eval(page, """
        const st = app.tabState[arg];
        app._stopBgContPoller(arg);
        return {streaming: st.streaming, pending: st._canonicalResyncPending,
          completedPending: st._pendingCompletedTurnSync || null,
          ready: st.messagesReady, loading: st.messagesLoading};
    """, SID)
    assert result == {
        "streaming": False, "pending": False, "completedPending": None,
        "ready": True, "loading": False,
    }
    assert sum("?tail=" in url for url in reads) == (2 if mode == "replay_retry" else 1)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("known_uuid", [True, False])
def test_completed_partial_viewport_adopts_final_without_refresh(
    page, backend_url, auth_token, width, known_uuid,
):
    """A verified completion keeps the scrolled live node as it acquires a UUID."""
    page.set_viewport_size({"width": width, "height": 900})
    errors, _, _ = _prepare(page, backend_url, auth_token)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st.atBottom = false;
        st.messageRange.visibleStart = 1;
        st.messageRange.visibleEnd = 2;
        st._userScrollAt = 10;
        const live = st.messages.at(-1);
        const loaded = await app._runCompletedTurnSync(arg.sid, st, {
          expectedText: 'LIVE_PARTIAL',
          expectedAssistantUuid: arg.known ? 'fixture-final' : '',
          completedTurnId: 'fixture-turn', followTail: false,
        });
        return {loaded, text: st.messages.at(-1).text,
          sameNodeOwner: st.messages.at(-1) === live,
          key: st.messages.at(-1)._k, atBottom: st.atBottom,
          pending: st._pendingCompletedTurnSync,
          followTail: window.visibilityScrolls};
    """, {"sid": SID, "known": known_uuid})
    assert result == {
        "loaded": True, "text": FINAL, "sameNodeOwner": True,
        "key": f"{SID}:live:partial", "atBottom": False,
        "pending": None, "followTail": [False],
    }
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("same_turn", [True, False])
def test_lost_done_viewport_uses_only_retired_turn_commit(
    page, backend_url, auth_token, same_turn,
):
    """List recovery gets the same mapping only for its exact retired stream."""
    errors, _, _ = _prepare(page, backend_url, auth_token)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st.atBottom = false;
        st.messageRange.visibleStart = 1;
        st.messageRange.visibleEnd = 2;
        st.activeTurnId = arg.same ? 'fixture-turn' : 'another-turn';
        st.streaming = true;
        const live = st.messages.at(-1);
        app._retireStaleSessionStream(arg.sid, st);
        const loaded = await app._runHistoryRevisionSync(arg.sid, st);
        return {loaded, text:st.messages.at(-1).text,
          sameOwner:st.messages.at(-1) === live, atBottom:st.atBottom};
    """, {"sid": SID, "same": same_turn})
    assert result == {
        "loaded": same_turn, "text": FINAL if same_turn else "LIVE_PARTIAL",
        "sameOwner": True, "atBottom": False,
    }
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("anchor_kind", ["interior", "transient_only"])
@pytest.mark.parametrize("background", [False, True])
def test_completion_recovers_removed_range_edges(
    page, backend_url, auth_token, anchor_kind, background, width,
):
    page.set_viewport_size({"width": width, "height": 900})
    errors, _, reads = _prepare(page, backend_url, auth_token)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st.atBottom = false;
        const prompt = st.messages[0];
        const partial = st.messages[1];
        const ephemeral = n => ({role:'assistant', text:'transient ' + n,
            _k:arg.sid + ':live:transient-' + n});
        st.messages = arg.kind === 'interior'
          ? [ephemeral(1), prompt, ephemeral(2), partial]
          : [prompt, ephemeral(1), partial];
        st.messageRange.visibleStart = arg.kind === 'interior' ? 0 : 1;
        st.messageRange.visibleEnd = arg.kind === 'interior' ? 3 : 2;
        st.messageRange.total = st.messages.length;
        st._userScrollAt = 10;
        if (arg.background) app.currentId = 'other-tab';
        const loaded = await app._runCompletedTurnSync(arg.sid, st, {
          expectedText:'LIVE_PARTIAL', expectedAssistantUuid:'fixture-final',
          completedTurnId:'fixture-turn', followTail:false,
        });
        const first = {loaded, final:st.messages.at(-1).text,
          atBottom:st.atBottom, pending:!!st._pendingCompletedTurnSync};
        if (arg.background) {app.currentId = arg.sid; app._activateTabState(arg.sid);}
        return first;
    """, {"sid": SID, "kind": anchor_kind, "background": background})
    assert result == {"loaded": True, "final": FINAL,
                      "atBottom": False, "pending": False}
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("boundary", ["partial", "full_order", "unrelated", "active"])
def test_missing_reader_anchor_does_not_authorize_unrelated_replacement(
    page, backend_url, auth_token, boundary,
):
    errors, history, _ = _prepare(page, backend_url, auth_token)
    if boundary == "partial":
        history.update(offset=20, total=22, has_more=True)
    elif boundary == "active":
        history["completion_state"]["active"] = True
    elif boundary == "unrelated":
        history["completion_state"]["completed_turn_id"] = "another-turn"
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        st.activeTurnId = 'fixture-turn';
        st.atBottom = false;
        st.messages.splice(1, 0, {role:'assistant', text:'reader anchor',
          _k:arg.sid + ':live:reader'});
        st.messageRange.visibleStart = 1;
        st.messageRange.visibleEnd = 2;
        st.messageRange.total = 3;
        if (arg.boundary === 'full_order') st.messageRange.order = 'full';
        const anchor = st.messages[1];
        const loaded = await app.loadSession(arg.sid, {quiet:true, probeActive:false});
        return {loaded, preserved:st.messages[1] === anchor, atBottom:st.atBottom};
    """, {"sid": SID, "boundary": boundary})
    assert result == {"loaded": False, "preserved": True, "atBottom": False}
    _assert_no_browser_errors(page, errors)


def test_removed_anchor_recovery_settles_real_scheduler(page, backend_url, auth_token):
    errors, _, reads = _prepare(page, backend_url, auth_token)
    _app_eval(page, """
        const st = app.tabState[arg];
        st.atBottom = false;
        st.messages.splice(1, 0, {role:'assistant', text:'transient reader block',
          _k:arg + ':live:reader'});
        Object.assign(st.messageRange, {visibleStart:1, visibleEnd:2, total:3});
        app._requestSessionSync = window.realVisibilityRequest;
        app._reconcileCompletedTurn(arg, st, 'LIVE_PARTIAL', 0,
          'fixture-final', 'fixture-turn', {followTail:false});
    """, SID)
    page.wait_for_function("""sid => {
        const st = document.querySelector('#app')._x_dataStack[0].tabState[sid];
        return !st._pendingCompletedTurnSync && st._installedCanonicalCount === 2;
    }""", arg=SID)
    result = _app_eval(page, """
        const st = app.tabState[arg];
        return {atBottom:st.atBottom, text:st.messages.at(-1).text};
    """, SID)
    assert result == {"atBottom": False, "text": FINAL}
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    assert sum('?tail=' in url for url in reads) == 1
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("width", [1440, 390])
def test_completed_tool_viewport_keeps_final_scroll_reachable(page, backend_url, auth_token, width):
    """A successful history install must expose the completed suffix to scrolling."""
    page.set_viewport_size({"width": width, "height": 900})
    errors, history, _ = _prepare(page, backend_url, auth_token)
    history["messages"].insert(1, {
        "role": "thinking", "text": "Checking system status", "uuid": "fixture-tool-tail",
    })
    history.update(total=3, message_count=3)
    result = _app_eval(page, """
        const st = app.tabState[arg];
        st.messages = [st.messages[0], {
          role:'thinking', text:'Checking system status', uuid:'fixture-tool-tail',
          _k:arg + ':uuid:fixture-tool-tail',
        }];
        Object.assign(st.messageRange, {visibleStart:0, visibleEnd:2, total:2});
        st.atBottom = false;
        st._userScrollAt = Date.now();
        const loaded = await app._runCompletedTurnSync(arg, st, {
          expectedText:'', expectedAssistantUuid:'fixture-final',
          completedTurnId:'fixture-turn', followTail:false,
        });
        return {loaded, final:st.messages.at(-1).text,
          exposed:st.messageRange.visibleEnd === st.messages.length,
          following:st.atBottom};
    """, SID)
    assert result == {"loaded": True, "final": FINAL, "exposed": True, "following": False}
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("gesture", ["wheel", "touch", "pointer"])
def test_nested_tool_scroll_does_not_freeze_live_tail(page, backend_url, auth_token, gesture):
    errors, _, _ = _prepare(page, backend_url, auth_token)
    result = _app_eval(page, """
        const st = app.tabState[arg.sid];
        const body = app._chatBodyElement();
        const nested = document.createElement('div');
        nested.style.cssText = 'height:100px;overflow:auto;width:100px';
        nested.innerHTML = '<div style="height:1000px">tool output</div>';
        body.appendChild(nested);
        nested.scrollTop = 300;
        st.atBottom = true;
        st._userScrollAt = 0;
        if (arg.gesture === 'wheel') {
          nested.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:-50}));
        } else if (arg.gesture === 'pointer') {
          const rect = nested.getBoundingClientRect();
          nested.dispatchEvent(new PointerEvent('pointerdown', {
            bubbles:true, clientX:rect.right-2, clientY:rect.top+10,
          }));
        } else {
          const event = type => {
            const ev = new Event(type, {bubbles:true});
            Object.defineProperty(ev, 'touches', {value:[{clientY:type === 'touchstart' ? 100 : 130}]});
            nested.dispatchEvent(ev);
          };
          event('touchstart'); event('touchmove');
        }
        const following = st.atBottom;
        nested.remove();
        return following;
    """, {"sid": SID, "gesture": gesture})
    assert result is True
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("reading", [False, True])
def test_long_tool_stream_done_exposes_final_without_reload(
    page, backend_url, auth_token, width, reading,
):
    from tests.e2e.test_chat_render_perf import _install_fake_event_source

    page.set_viewport_size({"width": width, "height": 900})
    _install_fake_event_source(page)
    page.route("**/api/chat/stream/start", lambda route: route.fulfill(
        status=200, json={"ticket": "long-completion-ticket"},
    ))
    errors, history, reads = _prepare(page, backend_url, auth_token)
    history["messages"] = [{"role": "user", "text": "FIXTURE_PROMPT",
                            "uuid": "fixture-user", "_turnRoot": True}]
    for i in range(70):
        history["messages"].extend([
            {"role": "thinking", "text": f"Inspect fixture {i}", "uuid": f"think-{i}"},
            {"role": "tool_use", "id": f"tool-{i}", "name": "Bash",
             "input": {"command": "true"}, "summary": "fixture check", "uuid": f"use-{i}"},
            {"role": "tool_result", "tool_use_id": f"tool-{i}", "tool_name": "Bash",
             "text": f"fixture output {i}", "preview": f"fixture output {i}",
             "uuid": f"result-{i}"},
        ])
    history["messages"].append({"role": "assistant", "text": FINAL, "uuid": "fixture-final"})
    history.update(total=len(history["messages"]), message_count=len(history["messages"]))
    _app_eval(page, """
        const st = app.tabState[arg];
        app._requestSessionSync = window.realVisibilityRequest;
        app._pullSessionList = async () => false;
        app._ensureSessionRegistered = async () => true;
        app._confirmSessionBusy = async () => false;
        app.availableModels = [{model:'e2e-model', label:'E2E', group:'e2e'}];
        app.model = 'e2e-model';
        app.defaultModel = 'e2e-model';
        st.permission = 'bypassPermissions';
        st.messages = [];
        Object.assign(st.messageRange, {visibleStart:0, visibleEnd:0, total:0});
        st.atBottom = true;
        app.input = 'FIXTURE_PROMPT';
        app.send();
    """, SID)
    page.wait_for_function("window.__fakeChatStreams().length === 1")
    page.evaluate("""async () => {
        let seq = 0;
        const emit = (kind, data) => window.__emitSse(kind, {
          ...data, turn_id:'fixture-turn', event_seq:++seq,
        });
        for (let i = 0; i < 70; i++) {
          emit('thinking', {text:'Inspect fixture ' + i});
          emit('tool_use', {id:'tool-' + i, name:'Bash', input:{command:'true'},
            summary:'fixture check'});
          emit('tool_result', {id:'tool-' + i, tool_use_id:'tool-' + i, tool_name:'Bash',
            text:'fixture output ' + i, preview:'fixture output ' + i});
          if (i % 10 === 0) await new Promise(requestAnimationFrame);
        }
        window.completionSequence = seq;
    }""")
    page.wait_for_timeout(250)
    if reading:
        body = page.locator(".chat-body")
        box = body.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -300)
        page.wait_for_function("""sid => !document.querySelector('#app')
            ._x_dataStack[0].tabState[sid].atBottom""", arg=SID)
    # Omit the final text delta: the real done listener/scheduler must recover it.
    page.evaluate("""() => window.__emitSse('done', {
        turn_id:'fixture-turn', event_seq:++window.completionSequence,
        assistant_uuid:'fixture-final', completed_at_ms:Date.now(),
        duration_ms:1000, model:'e2e-model',
    })""")
    page.wait_for_function("""sid => {
        const st = document.querySelector('#app')._x_dataStack[0].tabState[sid];
        return !st.streaming && !st._pendingCompletedTurnSync
          && st.messages.at(-1)?.text === 'CANONICAL_FINAL_COMPLETE';
    }""", arg=SID)
    result = _app_eval(page, """
        const st = app.tabState[arg];
        return {reachable:st.messageRange.visibleEnd === st.messages.length,
          following:st.atBottom, mounted:app._paneElement(arg).querySelectorAll('.msg').length};
    """, SID)
    assert result["reachable"] is True
    assert result["following"] is (not reading)
    assert result["mounted"] < len(history["messages"])
    # Ordinary scrolling can reach the answer; no refresh or jump-to-latest API.
    for _ in range(5):
        page.locator(".chat-body").evaluate("el => el.scrollTop = el.scrollHeight")
        page.wait_for_timeout(100)
    expect(page.locator(".msg-pane p").filter(has_text=FINAL)).to_be_visible()
    assert reads
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("anchor_survives,total", [(True, 202), (False, 202), (False, 2202)])
def test_history_revision_expands_window_then_latest_fetches_fresh_tail(
    page, backend_url, auth_token, anchor_survives, total,
):
    """A disjoint tail must neither loop unchanged nor strand the latest button."""
    from urllib.parse import urlparse, parse_qs

    errors, history, reads = _prepare(page, backend_url, auth_token)
    history["completion_state"]["completed_turn_id"] = "newer-external-turn"
    full = [{"role": "user", "text": "FIXTURE_PROMPT" if anchor_survives else "NEW_PROMPT", "uuid": "fixture-user"
             if anchor_survives else "different-user"}]
    full += [{"role": "assistant", "text": f"SYNTHETIC_ROW_{n}", "uuid": f"row-{n}"}
             for n in range(total - 2)]
    full.append({"role": "assistant", "text": FINAL, "uuid": "new-tail-final"})

    def window(route):
        reads.append(route.request.url)
        tail = int(parse_qs(urlparse(route.request.url).query)["tail"][0])
        offset = max(0, len(full) - tail)
        route.fulfill(json={**history, "messages": full[offset:], "offset": offset,
                            "total": len(full), "message_count": len(full),
                            "has_more": offset > 0})

    page.route(f"**/api/chat/sessions/{SID}?*", window)
    _app_eval(page, """
        const st = app.tabState[arg];
        st.activeTurnId = 'fixture-turn';
        st.atBottom = false;
        Object.assign(st.messageRange, {visibleStart:0, visibleEnd:1});
        st._seenUpdated = 1;
        st._installedCanonicalCount = 2;
        window.historyRecoveryPerf = [];
        app._reportHistoryLoadPerf = fields => window.historyRecoveryPerf.push({...fields});
        app._requestSessionSync = window.realVisibilityRequest;
        void app._requestSessionSync(arg, 'history_revision', {targetUpdated:2});
    """, SID)
    page.wait_for_function("""({sid, total}) => {
        const st = document.querySelector('#app')._x_dataStack[0].tabState[sid];
        return st._installedCanonicalCount === total || st._historyAnchorRecovery?.exhausted;
    }""", arg={"sid": SID, "total": total})
    assert [int(parse_qs(urlparse(url).query)["tail"][0]) for url in reads] == [100, min(2000, total)]
    before = _app_eval(page, """
        const st = app.tabState[arg];
        await app._runHistoryRevisionSync(arg, st, {targetUpdated:2});
        return {pending:!!st._pendingExternalUpdate, atBottom:st.atBottom,
          first:st.messages[st.messageRange.visibleStart].uuid,
          recoveries:window.historyRecoveryPerf.map(row => row.recovery)};
    """, SID) if not anchor_survives else _app_eval(page, """
        const st = app.tabState[arg];
        return {pending:!!st._pendingExternalUpdate, atBottom:st.atBottom,
          first:st.messages[st.messageRange.visibleStart].uuid,
          recoveries:window.historyRecoveryPerf.map(row => row.recovery)};
    """, SID)
    assert before["atBottom"] is False
    assert before["first"] == "fixture-user"
    assert before["pending"] is not anchor_survives
    assert before["recoveries"] == ["expand", "restored" if anchor_survives else "exhausted"]
    assert len(reads) == 2
    result = _app_eval(page, """
        const st = app.tabState[arg];
        const loaded = await app.returnToLatest(arg);
        return {loaded, text:st.messages.at(-1).text, total:st.messageRange.total,
          atBottom:st.atBottom, pending:!!st._pendingExternalUpdate};
    """, SID)
    assert result == {"loaded": True, "text": FINAL, "total": total,
                      "atBottom": True, "pending": False}
    assert len(reads) == (2 if anchor_survives else 3)
    expect(page.locator(f'.msg-pane[data-tid="{SID}"]')).to_contain_text(FINAL)
    _assert_no_browser_errors(page, errors)
