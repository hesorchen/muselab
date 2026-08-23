"""Browser stress checks for long chat rendering.

These tests deliberately run against the real frontend bundle and Alpine DOM,
but keep the model/provider path deterministic by injecting controlled session
state or a fake EventSource stream. They cover the long-history and long-stream
regression classes that static lint cannot see.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Browser, Page, TimeoutError, expect  # noqa: E402


SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_MOBILE_TAB = ".mobile-tab-bar button"


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(
        f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=15000
    )
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=15000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed === true && app.appReady
            && app._sessionsInitialized && app.currentId
            && app.openTabIds.includes(app.currentId) && app.sessions.length > 0;
        }"""
    )


def _capture_browser_errors(page: Page) -> list[str]:
    errors: list[str] = []

    def on_console(msg):
        if msg.type in {"error", "warning"}:
            text = msg.text
            # The app intentionally logs failed optional backend probes during
            # isolated e2e setup; pageerror and muse-capture are still fatal.
            if text.startswith("Failed to load resource:"):
                return
            if "[muse-capture]" in text or msg.type == "error":
                errors.append(f"console.{msg.type}: {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    return errors


def _assert_no_browser_errors(page: Page, errors: list[str]) -> None:
    muse_errors = page.evaluate("() => (window.__museErrors__ || []).map(e => e.message)")
    assert not errors, "\n".join(errors)
    assert not muse_errors, f"window.__museErrors__ not empty: {muse_errors}"


def _app_eval(page: Page, body: str, arg=None):
    return page.evaluate(
        """([body, arg]) => {
            const app = document.querySelector("#app")._x_dataStack[0];
            return (new Function("app", "arg", body))(app, arg);
        }""",
        [body, arg],
    )


def _make_mixed_messages(total: int, prefix: str) -> list[dict]:
    messages: list[dict] = []
    for i in range(total):
        marker = f"{prefix}_{i:03d}"
        kind = i % 8
        if kind in {0, 4}:
            messages.append({
                "role": "user",
                "text": f"{marker} user prompt " + ("mobile tail paging " * 5),
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-u-{i}",
            })
        elif kind in {1, 5, 7}:
            text = f"{marker} assistant reply " + ("rendered markdown paragraph " * 8)
            messages.append({
                "role": "assistant",
                "text": text,
                "html": f"<p>{text}</p>",
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-a-{i}",
            })
        elif kind == 2:
            messages.append({
                "role": "tool_use",
                "name": "Bash",
                "summary": f"{marker} inspect fixture",
                "input": {"command": f"printf {marker}"},
                "text": f"{marker} tool use",
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-tu-{i}",
            })
        else:
            messages.append({
                "role": "tool_result",
                "tool_name": "Bash",
                "preview": f"{marker} ok",
                "text": f"{marker} tool result\n" + ("stdout line\n" * 3),
                "truncated": False,
                "is_error": False,
                "ts": 1_700_000_000 + i,
                "uuid": f"{prefix}-tr-{i}",
            })
    if messages:
        marker = f"{prefix}_{total - 1:03d}"
        text = f"{marker} latest assistant reply " + ("rendered markdown paragraph " * 8)
        messages[-1] = {
            "role": "assistant",
            "text": text,
            "html": f"<p>{text}</p>",
            "ts": 1_700_000_000 + total,
            "uuid": f"{prefix}-latest",
        }
    return messages


def _route_windowed_session(page: Page, sid: str, messages: list[dict]) -> list[dict]:
    requests: list[dict] = []

    def handle(route):
        url = route.request.url
        qs = parse_qs(urlparse(url).query)
        total = len(messages)
        offset = 0
        window = messages
        if "tail" in qs:
            tail = int(qs["tail"][0])
            offset = max(0, total - tail)
            window = messages[offset:]
        elif "offset" in qs and "limit" in qs:
            offset = int(qs["offset"][0])
            limit = int(qs["limit"][0])
            window = messages[offset:offset + limit]
        requests.append({
            "url": url,
            "tail": int(qs["tail"][0]) if "tail" in qs else None,
            "offset": offset,
            "limit": int(qs["limit"][0]) if "limit" in qs else None,
            "count": len(window),
        })
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Perf windowed session",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": window,
                "offset": offset,
                "total": total,
                "has_more": offset > 0,
                "history_generation": "gen-e2e-1",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    return requests


def _install_fake_event_source(page: Page) -> None:
    page.add_init_script(
        """
        (() => {
          const streams = [];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 0;
              streams.push(this);
              setTimeout(() => {
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event("open"));
                this.dispatchEvent(new Event("open"));
              }, 0);
            }
            close() { this.readyState = 2; this.closed = true; }
          }
          window.EventSource = FakeEventSource;
          window.__fakeStreams = streams;
          window.__fakeChatStreams = () => streams.filter(
            es => String(es.url || "").includes("/api/chat/stream?")
          );
          window.__emitSse = (type, payload) => {
            const chatStreams = window.__fakeChatStreams();
            const es = chatStreams[chatStreams.length - 1];
            if (!es) throw new Error("no fake chat EventSource");
            es.dispatchEvent(new MessageEvent(type, {
              data: typeof payload === "string" ? payload : JSON.stringify(payload || {}),
            }));
          };
        })();
        """
    )


def _visible_pane_with_text_snapshot(page: Page, text: str):
    return page.evaluate(
        """expected => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none");
          const pane = panes.find(p => p.textContent.includes(expected)) || null;
          const msgs = pane ? Array.from(pane.querySelectorAll(".msg")) : [];
          return {
            visiblePaneCount: panes.length,
            msgCount: msgs.length,
            text: pane ? pane.textContent : "",
          };
        }""",
        text,
    )


def test_deferred_history_bodies_load_without_manual_body_action(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    requested: list[str] = []
    full = {
        "assistant-record:0:assistant": {
            "role": "assistant", "text": "ASSISTANT_FULL_BODY_MARKER",
        },
        "thinking-record:0:thinking": {
            "role": "thinking", "text": "THINKING_FULL_BODY_MARKER",
        },
        "tool-record:0:tool_result": {
            "role": "tool_result", "text": "TOOL_FULL_BODY_MARKER",
            "tool_name": "UnknownTool",
        },
        "compact-record:0:user": {
            "role": "user", "text": "COMPACT_FULL_BODY_MARKER",
            "_is_compact_summary": True,
        },
    }

    def handle_body(route) -> None:
        block_id = unquote(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        requested.append(block_id)
        payload = {
            **full[block_id],
            "block_id": block_id,
            "body_ref": block_id,
            "body_available": True,
            "body_state": "loaded",
            "body_length": len(full[block_id]["text"]),
        }
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload))

    page.route("**/api/chat/sessions/*/blocks/*", handle_body)
    sid = _app_eval(page, "return app.currentId;")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const deferred = (role, blockId, extra = {}) => ({
          role, block_id: blockId, body_ref: blockId,
          body_available: true, body_state: "unloaded", body_length: 12000,
          text: role + " preview", preview: role + " preview",
          _k: arg + ":block:" + blockId, _noAnim: true, ...extra,
        });
        st._loaded = true;
        st.messages = [
          deferred("assistant", "assistant-record:0:assistant", {
            html: "<p>assistant preview</p>",
          }),
          deferred("thinking", "thinking-record:0:thinking"),
          deferred("tool_result", "tool-record:0:tool_result", {
            tool_name: "UnknownTool", is_error: false,
          }),
          deferred("user", "compact-record:0:user", {
            _is_compact_summary: true,
          }),
        ];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        app._expandedMsgs = {};
        app._activateTabState(arg);
        app.messagesReady = true;
        return true;
        """,
        sid,
    )

    page.wait_for_function(
        """() => document.body.textContent.includes('ASSISTANT_FULL_BODY_MARKER')"""
    )
    assert requested == ["assistant-record:0:assistant"]
    expect(page.get_by_text("Load full body")).to_have_count(0)
    expect(page.get_by_text("加载完整正文")).to_have_count(0)

    page.locator(".thinking-head").click()
    expect(page.get_by_text("THINKING_FULL_BODY_MARKER")).to_be_visible()
    page.locator(".tool-result-head").click()
    expect(page.get_by_text("TOOL_FULL_BODY_MARKER")).to_be_visible()
    page.locator(".compact-summary-pill").click()
    expect(page.get_by_text("COMPACT_FULL_BODY_MARKER")).to_be_visible()
    assert requested == [
        "assistant-record:0:assistant",
        "thinking-record:0:thinking",
        "tool-record:0:tool_result",
        "compact-record:0:user",
    ]
    _assert_no_browser_errors(page, errors)


def test_context_recovery_replays_plain_text_exactly_once(
    page: Page, backend_url, auth_token,
):
    """A recovered SSE error changes sid and never loops/reuses uploads."""
    errors = _capture_browser_errors(page)
    _install_fake_event_source(page)
    ticket_bodies: list[dict] = []

    def handle_ticket(route) -> None:
        ticket_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": f"recovery-{len(ticket_bodies)}"}),
        )

    page.route("**/api/chat/stream/start", handle_ticket)
    _login(page, backend_url, auth_token)
    recovered_id = "792c513d-8a63-4805-9937-b6b79861ce4e"
    chained_id = "4f60bd20-0636-4d17-b5ce-94d6663da1d6"

    result = page.evaluate(
        """async ({recoveredId, chainedId}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sourceSid = app.currentId;
          const originalAdopt = app._adoptRecoveredSession;
          const originalBusy = app._confirmSessionBusy;
          const originalRuntimeWait = app._awaitRuntimeSettingPatches;
          app._confirmSessionBusy = async () => false;
          app._awaitRuntimeSettingPatches = async () => true;
          app.availableModels = [{
            model: 'e2e-recovery-model', label: 'E2E recovery', group: 'e2e',
            supports_thinking: true,
          }];
          app.model = 'e2e-recovery-model';
          app.sessions = app.sessions.map(session => session.id === sourceSid
            ? {...session, model: 'e2e-recovery-model'} : session);
          const adoptedIds = [];
          app._adoptRecoveredSession = async payload => {
            const raw = payload.recovered_session;
            const nextId = raw.id || raw.session_id;
            adoptedIds.push(nextId);
            const meta = {...raw, id: nextId, session_id: nextId};
            app.sessions = [meta, ...app.sessions.filter(s => s.id !== nextId)];
            const state = app._ensureTabState(nextId);
            state._loaded = true;
            if (!app.openTabIds.includes(nextId)) app.openTabIds.push(nextId);
            app.currentId = nextId;
            app._activateTabState(nextId);
            return {meta, state, shouldFocus: true, alreadyKnown: false};
          };
          try {
            const sourceState = app._ensureTabState(sourceSid);
            sourceState.draft.input = 'RECOVERY_PLAIN_TEXT_ONCE';
            sourceState.draft.pendingImages = [];
            sourceState.draft.pendingDocs = [];
            sourceState.draft.pendingQuotes = [];
            app._activateComposerState(sourceSid);
            await app.send();
            for (let i = 0; i < 100 && window.__fakeChatStreams().length < 1; i++) {
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            const sourceStream = window.__fakeChatStreams()[0];
            if (!sourceStream) throw new Error('source stream did not start');
            const payload = {
              error: 'context window could not be compacted; a recovery session was created',
              kind: 'context_window', retryable: false,
              cta: 'compact_or_fork', activity_source: 'direct',
              recovered_session: {
                id: recoveredId, session_id: recoveredId,
                name: 'Recovered E2E', model: app.model,
                permission: app.permission, cwd: app.currentWorkspacePath(),
              },
              recovery_stats: {estimated_post_tokens: 20000},
            };
            sourceStream.dispatchEvent(new MessageEvent('error', {
              data: JSON.stringify(payload),
            }));
            for (let i = 0; i < 150 && window.__fakeChatStreams().length < 2; i++) {
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            const afterFirst = window.__fakeChatStreams().length;
            // Replayed terminal frames must not launch another recovery turn.
            sourceStream.dispatchEvent(new MessageEvent('error', {
              data: JSON.stringify(payload),
            }));
            await new Promise(resolve => setTimeout(resolve, 80));
            const recoveryStream = window.__fakeChatStreams()[1];
            if (!recoveryStream) throw new Error('recovery stream did not start');
            recoveryStream.dispatchEvent(new MessageEvent('error', {
              data: JSON.stringify({
                ...payload,
                recovered_session: {
                  ...payload.recovered_session,
                  id: chainedId,
                  session_id: chainedId,
                  name: 'Chained recovery must stay unopened',
                },
              }),
            }));
            await new Promise(resolve => setTimeout(resolve, 120));
            return {
              sourceSid, currentId: app.currentId,
              afterFirst, afterReplay: window.__fakeChatStreams().length,
              adoptedIds,
              chainedKnown: app.sessions.some(s => s.id === chainedId),
              handled: !!app._contextRecoveryHandled?.[recoveredId],
              autoSent: !!app._contextRecoveryAutoSent?.[recoveredId],
              recoveryMessages: app._ensureTabState(recoveredId).messages
                .filter(message => message.role === 'user')
                .map(message => message.text),
            };
          } finally {
            app._adoptRecoveredSession = originalAdopt;
            app._confirmSessionBusy = originalBusy;
            app._awaitRuntimeSettingPatches = originalRuntimeWait;
            for (const stream of window.__fakeChatStreams()) stream.close();
          }
        }""",
        {"recoveredId": recovered_id, "chainedId": chained_id},
    )

    assert result["currentId"] == recovered_id
    assert result["afterFirst"] == result["afterReplay"] == 2
    assert result["adoptedIds"] == [recovered_id]
    assert result["chainedKnown"] is False
    assert result["handled"] is True
    assert result["autoSent"] is True
    assert result["recoveryMessages"] == ["RECOVERY_PLAIN_TEXT_ONCE"]
    assert [body["prompt"] for body in ticket_bodies] == [
        "RECOVERY_PLAIN_TEXT_ONCE", "RECOVERY_PLAIN_TEXT_ONCE",
    ]
    assert all(body["image_ids"] == "" for body in ticket_bodies)
    _assert_no_browser_errors(page, errors)


def _bootstrap_session_for_real_load(page: Page, sid: str, name: str) -> None:
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{ id: arg.sid, name: arg.name, updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg.sid];
        app.tabState = {};
        app.currentId = arg.sid;
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg.sid);
        return true;
        """,
        {"sid": sid, "name": name},
    )


def test_chat_ime_commit_enter_keeps_chinese_text_and_next_enter_sends(
    page: Page, backend_url, auth_token,
):
    """WebView compositionend→plain Enter must commit, not submit, Chinese."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-composition-chat"
    _bootstrap_session_for_real_load(page, sid, "IME composition")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "";
        app._activateTabState(arg);
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_visible(timeout=5000)
    textarea.focus()

    result = page.evaluate(
        """async () => {
          const textarea = document.querySelector(".chat-input-textarea");
          textarea.dispatchEvent(new CompositionEvent("compositionstart", {
            bubbles: true, data: "ni",
          }));
          textarea.value = "你";
          textarea.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            inputType: "insertCompositionText",
            data: "你",
            isComposing: true,
          }));
          // The physical Enter that confirms the highlighted IME candidate is
          // observed while composition is still active. Some WebViews then
          // emit a second plain keydown after compositionend for that same key.
          textarea.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", code: "Enter", bubbles: true, cancelable: true,
            isComposing: true,
          }));
          textarea.dispatchEvent(new CompositionEvent("compositionend", {
            bubbles: true, data: "你",
          }));
          // Chromium WebView/Safari ordering: the final model input lands,
          // then the candidate-confirming Enter is reported as a plain key.
          textarea.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            inputType: "insertText",
            data: "你",
            isComposing: false,
          }));
          const commitEnter = new KeyboardEvent("keydown", {
            key: "Enter", code: "Enter", bubbles: true, cancelable: true,
          });
          textarea.dispatchEvent(commitEnter);
          const app = document.querySelector("#app")._x_dataStack[0];
          const committed = {
            sendCalls: window.__imeSendCalls,
            input: app.input,
            value: textarea.value,
            prevented: commitEnter.defaultPrevented,
          };

          const sendEnter = new KeyboardEvent("keydown", {
            key: "Enter", code: "Enter", bubbles: true, cancelable: true,
          });
          textarea.dispatchEvent(sendEnter);
          return {
            committed,
            sendCalls: window.__imeSendCalls,
            sendPrevented: sendEnter.defaultPrevented,
            composing: !!textarea._museImeComposing,
          };
        }"""
    )

    assert result["committed"] == {
        "sendCalls": 0,
        "input": "你",
        "value": "你",
        "prevented": True,
    }
    assert result["sendCalls"] == 1
    assert result["sendPrevented"] is True
    assert result["composing"] is False
    _assert_no_browser_errors(page, errors)


def test_missing_compositionend_cannot_leave_chat_ime_stuck(
    page: Page, backend_url, auth_token,
):
    """A final non-composing input releases IME even with the old inputType."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-missing-compositionend"
    _bootstrap_session_for_real_load(page, sid, "Missing compositionend")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "";
        app._activateTabState(arg);
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_visible(timeout=5000)
    textarea.focus()

    state = page.evaluate(
        """() => {
          const textarea = document.querySelector('.chat-input-textarea');
          textarea.dispatchEvent(new CompositionEvent('compositionstart', {
            bubbles: true, data: 'ni',
          }));
          textarea.value = '你';
          textarea.dispatchEvent(new InputEvent('input', {
            bubbles: true, inputType: 'insertCompositionText',
            data: '你', isComposing: true,
          }));
          // Embedded Chromium occasionally omits compositionend but reports
          // the final event as explicitly non-composing while retaining the
          // insertCompositionText inputType.
          textarea.dispatchEvent(new InputEvent('input', {
            bubbles: true, inputType: 'insertCompositionText',
            data: '你', isComposing: false,
          }));
          const app = document.querySelector('#app')._x_dataStack[0];
          return { composing: !!textarea._museImeComposing,
            input: app.input, value: textarea.value };
        }"""
    )
    assert state == {"composing": False, "input": "你", "value": "你"}

    page.keyboard.press("Enter")
    sent = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          sends: window.__imeSendCalls };
        """,
    )
    assert sent == {"value": "你", "input": "你", "sends": 1}
    _assert_no_browser_errors(page, errors)


def test_stale_missing_compositionend_recovers_only_on_safe_plain_enter(
    page: Page, backend_url, auth_token,
):
    """A five-second stale flag heals; active IME signals always stay owned."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-stale-missing-compositionend"
    _bootstrap_session_for_real_load(page, sid, "Stale missing compositionend")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "";
        app._activateTabState(arg);
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_visible(timeout=5000)
    textarea.focus()

    guarded = page.evaluate(
        """() => {
          const textarea = document.querySelector('.chat-input-textarea');
          const app = document.querySelector('#app')._x_dataStack[0];
          textarea.dispatchEvent(new CompositionEvent('compositionstart', {
            bubbles: true, data: 'ni',
          }));
          textarea.value = '你';
          textarea.dispatchEvent(new InputEvent('input', {
            bubbles: true, inputType: 'insertCompositionText',
            data: '你', isComposing: true,
          }));

          const fresh = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          });
          textarea.dispatchEvent(fresh);

          textarea._museImeStartedAt = Date.now()
            - app.IME_STALE_AFTER_MS - 1;
          const modified = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
            shiftKey: true,
          });
          textarea.dispatchEvent(modified);

          const composing = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
            isComposing: true,
          });
          textarea.dispatchEvent(composing);

          const legacy229 = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          });
          Object.defineProperty(legacy229, 'keyCode', { get: () => 229 });
          Object.defineProperty(legacy229, 'which', { get: () => 229 });
          textarea.dispatchEvent(legacy229);

          const process = new KeyboardEvent('keydown', {
            key: 'Process', code: 'Enter', bubbles: true, cancelable: true,
          });
          app.onEnter(process);
          return {
            sends: window.__imeSendCalls,
            composing: !!textarea._museImeComposing,
            freshPrevented: fresh.defaultPrevented,
            modifiedPrevented: modified.defaultPrevented,
            composingPrevented: composing.defaultPrevented,
            legacyPrevented: legacy229.defaultPrevented,
            processPrevented: process.defaultPrevented,
            staleFor: Date.now() - textarea._museImeStartedAt,
          };
        }"""
    )
    assert guarded["sends"] == 0
    assert guarded["composing"] is True
    assert guarded["freshPrevented"] is False
    assert guarded["modifiedPrevented"] is False
    assert guarded["composingPrevented"] is False
    assert guarded["legacyPrevented"] is False
    assert guarded["processPrevented"] is False
    assert guarded["staleFor"] >= 5000

    # Use Chromium's native keyboard path for the recovery trigger. This Enter
    # has no isComposing, legacy 229, or Process marker, so the stale lifecycle
    # settles through the normal chat bridge and submits exactly once.
    page.keyboard.press("Enter")
    recovered = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          sends: window.__imeSendCalls,
          composing: !!ta._museImeComposing,
          startedAt: ta._museImeStartedAt || 0,
          hasOwner: Object.prototype.hasOwnProperty.call(
            ta, '_museImeOwnerSid') };
        """,
    )
    assert recovered == {
        "value": "你", "input": "你", "sends": 1,
        "composing": False, "startedAt": 0, "hasOwner": False,
    }
    _assert_no_browser_errors(page, errors)


def test_space_or_keyup_ime_commit_does_not_swallow_next_enter(
    page: Page, backend_url, auth_token,
):
    """Only a duplicate keydown from the same physical Enter is suppressed."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-physical-enter-guard"
    _bootstrap_session_for_real_load(page, sid, "IME physical Enter guard")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        app._activateTabState(arg);
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_visible(timeout=5000)
    textarea.focus()

    result = page.evaluate(
        """() => {
          const textarea = document.querySelector('.chat-input-textarea');
          const compose = (value, key) => {
            textarea.dispatchEvent(new CompositionEvent('compositionstart', {
              bubbles: true, data: value,
            }));
            textarea.value = value;
            textarea.dispatchEvent(new InputEvent('input', {
              bubbles: true, inputType: 'insertCompositionText',
              data: value, isComposing: true,
            }));
            textarea.dispatchEvent(new KeyboardEvent('keydown', {
              key, code: key, bubbles: true, cancelable: true,
              isComposing: true,
            }));
            textarea.dispatchEvent(new CompositionEvent('compositionend', {
              bubbles: true, data: value,
            }));
          };

          // Space-selected candidates never arm Enter suppression.
          compose('你', 'Space');
          const afterSpace = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          });
          textarea.dispatchEvent(afterSpace);
          const spaceSends = window.__imeSendCalls;

          // If Enter selected the candidate but no duplicate keydown followed,
          // its keyup ends the physical-key guard; the next Enter is deliberate.
          compose('你好', 'Enter');
          textarea.dispatchEvent(new KeyboardEvent('keyup', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          }));
          const afterKeyup = new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
          });
          textarea.dispatchEvent(afterKeyup);
          return { spaceSends, sends: window.__imeSendCalls,
            spacePrevented: afterSpace.defaultPrevented,
            keyupPrevented: afterKeyup.defaultPrevented,
            composing: !!textarea._museImeComposing };
        }"""
    )
    assert result == {
        "spaceSends": 1,
        "sends": 2,
        "spacePrevented": True,
        "keyupPrevented": True,
        "composing": False,
    }
    _assert_no_browser_errors(page, errors)


def test_native_post_ime_enter_cannot_insert_a_newline(
    page: Page, backend_url, auth_token,
):
    """A real Chromium key event after composition is consumed exactly once."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-native-enter"
    _bootstrap_session_for_real_load(page, sid, "Native IME Enter")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "你";
        app._activateTabState(arg);
        app._setChatInput("你");
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        const textarea = document.querySelector(".chat-input-textarea");
        const primeCommitEnter = ev => {
          if (ev.key !== "Enter") return;
          textarea.removeEventListener("keydown", primeCommitEnter, true);
          // Capture runs before Alpine's bubble listener. This reproduces a
          // WebView's plain keydown immediately following compositionend while
          // retaining Chromium's real textarea default action.
          textarea._museImeCommitEnterDown = true;
          textarea._museImeEndedAt = Math.max(0.001, ev.timeStamp - 1);
        };
        textarea.addEventListener("keydown", primeCommitEnter, true);
        textarea.focus();
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_focused()

    page.keyboard.press("Enter")
    first = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          sends: window.__imeSendCalls, endedAt: ta._museImeEndedAt || 0 };
        """,
    )
    assert first == {"value": "你", "input": "你", "sends": 0, "endedAt": 0}

    page.keyboard.press("Enter")
    second = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          sends: window.__imeSendCalls };
        """,
    )
    assert second == {"value": "你", "input": "你", "sends": 1}
    _assert_no_browser_errors(page, errors)


def test_wide_touch_pc_enter_sends_instead_of_inserting_newline(
    page: Page, backend_url, auth_token,
):
    """A touchscreen-capable PC is still a desktop composer at wide width."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "wide-touch-enter"
    _bootstrap_session_for_real_load(page, sid, "Wide touch Enter")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "send from touch PC";
        app._activateTabState(arg);
        app._setChatInput("send from touch PC");
        const realMatchMedia = window.matchMedia.bind(window);
        window.__restoreMatchMedia = () => { window.matchMedia = realMatchMedia; };
        window.matchMedia = query => {
          if (query === "(pointer: coarse)") {
            return { matches: true, media: query,
              addEventListener() {}, removeEventListener() {} };
          }
          return realMatchMedia(query);
        };
        window.__imeSendCalls = 0;
        app.send = () => { window.__imeSendCalls += 1; return true; };
        document.querySelector(".chat-input-textarea").focus();
        return true;
        """,
        sid,
    )
    try:
        page.keyboard.press("Enter")
        state = _app_eval(
            page,
            """
            const ta = document.querySelector('.chat-input-textarea');
            return { value: ta.value, input: app.input,
              sends: window.__imeSendCalls };
            """,
        )
        assert state == {
            "value": "send from touch PC",
            "input": "send from touch PC",
            "sends": 1,
        }
    finally:
        page.evaluate("() => window.__restoreMatchMedia?.()")
    _assert_no_browser_errors(page, errors)


def test_native_ime_buffer_survives_reactive_composer_reconciliation(
    page: Page, backend_url, auth_token,
):
    """Alpine model writes must not replace Chromium's native marked text."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "ime-native-reconciliation"
    _bootstrap_session_for_real_load(page, sid, "Native IME reconciliation")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.draft.input = "";
        app._activateTabState(arg);
        return true;
        """,
        sid,
    )
    textarea = page.locator(".chat-input-textarea")
    expect(textarea).to_be_visible(timeout=5000)
    textarea.click()
    page.wait_for_function(
        "() => document.activeElement === document.querySelector('.chat-input-textarea')"
    )

    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {
        "text": "ni",
        "selectionStart": 2,
        "selectionEnd": 2,
        "replacementStart": 0,
        "replacementEnd": 0,
    })
    composing = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          composing: !!ta._museImeComposing,
          privateHookWrapped:
            Object.prototype.hasOwnProperty.call(
              ta, '_museImeOriginalForceModelUpdate') };
        """,
    )
    assert composing == {
        "value": "ni", "input": "ni", "composing": True,
        "privateHookWrapped": False,
    }

    # This is the rare real-world race: a tab/draft reconciliation writes the
    # root x-model while native marked text is still active. Before the guard,
    # Alpine immediately assigned textarea.value and Chromium emitted no
    # compositionend, leaving the OS IME attached to stale editor state.
    protected = _app_eval(
        page,
        """
        app.input = 'reconciled draft';
        return new Promise(resolve => queueMicrotask(() => {
          const ta = document.querySelector('.chat-input-textarea');
          resolve({ value: ta.value, input: app.input,
            composing: !!ta._museImeComposing });
        }));
        """,
    )
    assert protected == {
        "value": "ni", "input": "reconciled draft", "composing": True,
    }

    cdp.send("Input.imeSetComposition", {
        "text": "你",
        "selectionStart": 1,
        "selectionEnd": 1,
        "replacementStart": 0,
        "replacementEnd": 2,
    })
    cdp.send("Input.insertText", {"text": "你"})
    committed = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          composing: !!ta._museImeComposing,
          privateHookWrapped:
            Object.prototype.hasOwnProperty.call(
              ta, '_museImeOriginalForceModelUpdate') };
        """,
    )
    assert committed == {
        "value": "你", "input": "你", "composing": False,
        "privateHookWrapped": False,
    }
    _assert_no_browser_errors(page, errors)


def test_native_ime_stays_attached_across_reconciliation_and_tab_switch(
    page: Page, backend_url, auth_token,
):
    """Repeated background refreshes and owner changes keep one healthy IME."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    first_sid = "ime-owner-first"
    second_sid = "ime-owner-second"
    _bootstrap_session_for_real_load(page, first_sid, "IME owner first")
    _app_eval(
        page,
        """
        const first = app._ensureTabState(arg.first);
        first._loaded = true;
        first.draft.input = "";
        const second = app._ensureTabState(arg.second);
        second._loaded = true;
        second.draft.input = "";
        app.sessions.push({
          id: arg.second, name: "IME owner second", updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        });
        app.openTabIds.push(arg.second);
        app._activateTabState(arg.first);
        const ta = document.querySelector('.chat-input-textarea');
        window.__imeStableNode = ta;
        window.__imeStableModelHook = ta._x_forceModelUpdate;
        return true;
        """,
        {"first": first_sid, "second": second_sid},
    )
    textarea = page.locator(".chat-input-textarea")
    textarea.click()
    page.wait_for_function(
        "() => document.activeElement === document.querySelector('.chat-input-textarea')"
    )
    cdp = page.context.new_cdp_session(page)

    for index, (phonetic, committed_text) in enumerate(
        [("ni", "你"), ("hao", "好"), ("zhong", "中")]
    ):
        _app_eval(page, "app._setChatInput(''); return true;")
        cdp.send("Input.imeSetComposition", {
            "text": phonetic,
            "selectionStart": len(phonetic),
            "selectionEnd": len(phonetic),
            "replacementStart": 0,
            "replacementEnd": 0,
        })
        # A canonical/background reconciliation may mutate the root model, but
        # must not write over Chromium's marked text while the control is focused.
        protected = _app_eval(
            page,
            """
            app.input = arg.model;
            return new Promise(resolve => queueMicrotask(() => {
              const ta = document.querySelector('.chat-input-textarea');
              resolve({ value: ta.value, composing: !!ta._museImeComposing });
            }));
            """,
            {"model": f"background-refresh-{index}"},
        )
        assert protected == {"value": phonetic, "composing": True}
        cdp.send("Input.imeSetComposition", {
            "text": committed_text,
            "selectionStart": len(committed_text),
            "selectionEnd": len(committed_text),
            "replacementStart": 0,
            "replacementEnd": len(phonetic),
        })
        cdp.send("Input.insertText", {"text": committed_text})
        committed = _app_eval(
            page,
            """
            const ta = document.querySelector('.chat-input-textarea');
            return {
              value: ta.value, input: app.input,
              composing: !!ta._museImeComposing,
              sameNode: ta === window.__imeStableNode,
              sameModelHook: ta._x_forceModelUpdate === window.__imeStableModelHook,
            };
            """,
        )
        assert committed == {
            "value": committed_text,
            "input": committed_text,
            "composing": False,
            "sameNode": True,
            "sameModelHook": True,
        }

    blur_commit = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        app.input = 'stale-background-model';
        return new Promise(resolve => queueMicrotask(() => {
          const visible = ta.value;
          ta.blur();
          resolve({
            visible,
            input: app.input,
            draft: app._ensureTabState(arg).draft.input,
          });
        }));
        """,
        first_sid,
    )
    assert blur_commit == {"visible": "中", "input": "中", "draft": "中"}
    textarea.click()

    # Change session ownership while marked text is live. The phonetic buffer
    # belongs to the old draft; the shared DOM then receives the new draft and
    # must accept another native composition without rebuilding the whole app.
    _app_eval(page, "app._setChatInput(''); return true;")
    cdp.send("Input.imeSetComposition", {
        "text": "wen",
        "selectionStart": 3,
        "selectionEnd": 3,
        "replacementStart": 0,
        "replacementEnd": 0,
    })
    switched = _app_eval(
        page,
        """
        app._captureComposerState(arg.first);
        app.currentId = arg.second;
        app._activateTabState(arg.second);
        const ta = document.querySelector('.chat-input-textarea');
        return {
          value: ta.value,
          firstDraft: app._ensureTabState(arg.first).draft.input,
          secondDraft: app._ensureTabState(arg.second).draft.input,
          composing: !!ta._museImeComposing,
          sameNode: ta === window.__imeStableNode,
        };
        """,
        {"first": first_sid, "second": second_sid},
    )
    assert switched == {
        "value": "",
        "firstDraft": "wen",
        "secondDraft": "",
        "composing": False,
        "sameNode": True,
    }

    textarea.click()
    cdp.send("Input.imeSetComposition", {
        "text": "wen",
        "selectionStart": 3,
        "selectionEnd": 3,
        "replacementStart": 0,
        "replacementEnd": 0,
    })
    cdp.send("Input.imeSetComposition", {
        "text": "文",
        "selectionStart": 1,
        "selectionEnd": 1,
        "replacementStart": 0,
        "replacementEnd": 3,
    })
    cdp.send("Input.insertText", {"text": "文"})
    recovered = _app_eval(
        page,
        """
        const ta = document.querySelector('.chat-input-textarea');
        return { value: ta.value, input: app.input,
          composing: !!ta._museImeComposing,
          sameNode: ta === window.__imeStableNode };
        """,
    )
    assert recovered == {
        "value": "文", "input": "文", "composing": False, "sameNode": True,
    }
    _assert_no_browser_errors(page, errors)


def test_chat_bubble_selection_quotes_as_attachment_and_asks_in_side_session(
    page: Page, backend_url, auth_token,
):
    """User/assistant正文 share the preview selection helper safely."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "selection-chat-source"
    _bootstrap_session_for_real_load(page, sid, "Selection source")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.messages = [
          {
            role: "user", text: "USER_SELECTION_MARKER user selected sentence",
            uuid: "selection-user", _k: "selection-user", _noAnim: true,
          },
          {
            role: "assistant",
            text: "ASSISTANT_SELECTION_MARKER assistant selected sentence",
            html: "<p>ASSISTANT_SELECTION_MARKER assistant selected sentence</p>",
            uuid: "selection-assistant", _k: "selection-assistant", _noAnim: true,
          },
        ];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        st.draft.input = "EXISTING_DRAFT";
        st.draft.pendingImages = [{
          id: "keep-image", uploading: false,
          preview: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
        }];
        st.draft.pendingDocs = [{ id: "keep-doc", uploading: false }];
        app._activateTabState(arg);
        app.mobileTab = "chat";
        app.lang = "zh";
        app.availableModels = [{ model: "e2e-model", label: "E2E" }];
        return true;
        """,
        sid,
    )
    page.wait_for_function(
        """sid => document.querySelectorAll(
          `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg`).length === 2""",
        arg=sid,
    )

    def select_text(selector: str) -> None:
        page.evaluate(
            """selector => {
              const node = document.querySelector(selector);
              if (!node) throw new Error(`missing selection node: ${selector}`);
              const range = document.createRange();
              range.selectNodeContents(node);
              const selection = window.getSelection();
              selection.removeAllRanges();
              selection.addRange(range);
              document.dispatchEvent(new Event('selectionchange'));
            }""",
            selector,
        )

    select_text(
        f'.msg-pane[data-tid="{sid}"] .msg.assistant .bubble p'
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.previewQuote.show && app.previewQuote.source === 'chat'
            && app.previewQuote.role === 'assistant';
        }"""
    )
    expect(page.locator(".preview-selection-actions")).to_be_visible()
    # iOS preserves the native selection by cancelling pointerdown, which can
    # suppress the follow-up synthetic click. Exercise the real touch path.
    page.locator(".preview-selection-actions button").first.dispatch_event(
        "pointerdown",
        {"pointerType": "touch", "button": 0, "bubbles": True, "cancelable": True},
    )
    quoted = _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        return {
          input: st.draft.input,
          quotes: st.draft.pendingQuotes.map(item => ({
            text: item.text, role: item.role, messageId: item.messageId,
          })),
          images: st.draft.pendingImages.map(item => item.id),
          docs: st.draft.pendingDocs.map(item => item.id),
        };
        """,
    )
    assert quoted["input"] == "EXISTING_DRAFT"
    assert len(quoted["quotes"]) == 1
    assert quoted["quotes"][0]["role"] == "assistant"
    assert quoted["quotes"][0]["messageId"] == "selection-assistant"
    assert "ASSISTANT_SELECTION_MARKER" in quoted["quotes"][0]["text"]
    assert quoted["images"] == ["keep-image"]
    assert quoted["docs"] == ["keep-doc"]

    _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        st.draft.input = "ASK_DRAFT_MUST_SURVIVE";
        st.draft.pendingQuotes.splice(0);
        app._activateComposerState(app.currentId);
        window.__selectionAskOriginalCreate = app._createPreviewSelectionAskSession;
        app._createPreviewSelectionAskSession = async (snapshot, question) => {
          const meta = {
            id: "chat-side-question", name: "Chat side question",
            model: app.model, permission: "default", active: false,
            cwd: app.currentWorkspacePath(),
          };
          app.sessions = [meta, ...app.sessions.filter(s => s.id !== meta.id)];
          const child = app._ensureTabState(meta.id);
          child._loaded = true;
          window.__selectionAskCreate = {snapshot, question};
          return meta;
        };
        app.send = async opts => {
          window.__selectionAskSend = opts;
          const child = app._ensureTabState(opts.sessionId);
          child.messages.splice(0, child.messages.length,
            {role: "user", text: opts.detachedText},
            {role: "assistant", text: "CHAT_SIDE_ANSWER"});
          child.streaming = false;
          return true;
        };
        """,
    )
    select_text(
        f'.msg-pane[data-tid="{sid}"] .msg.user .user-msg-text'
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.previewQuote.show && app.previewQuote.source === 'chat'
            && app.previewQuote.role === 'user';
        }"""
    )
    page.locator(".preview-selection-actions button").nth(1).dispatch_event(
        "pointerdown",
        {"pointerType": "touch", "button": 0, "bubbles": True, "cancelable": True},
    )
    ask = page.locator(".preview-selection-ask textarea")
    expect(ask).to_be_focused()
    ask.fill("这里的结论为什么成立？")
    ask.evaluate(
        """textarea => {
          textarea.dispatchEvent(new CompositionEvent('compositionstart', {
            bubbles: true, data: '？',
          }));
          textarea.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
            isComposing: true,
          }));
          textarea.dispatchEvent(new CompositionEvent('compositionend', {
            bubbles: true, data: '？',
          }));
        }"""
    )
    # A WebView can report the candidate-confirming Enter as a plain keydown
    # immediately after compositionend. It must neither submit nor add a
    # newline; the next Enter remains an immediate desktop submit.
    page.keyboard.press("Enter")
    assert page.evaluate("() => !window.__selectionAskSend") is True
    expect(ask).to_have_value("这里的结论为什么成立？")
    page.keyboard.press("Enter")
    page.wait_for_function("() => !!window.__selectionAskSend")
    expect(page.locator(".preview-selection-answer-body")).to_contain_text(
        "CHAT_SIDE_ANSWER"
    )
    sent = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return {
            opts: window.__selectionAskSend,
            create: window.__selectionAskCreate,
            currentId: app.currentId,
            draft: st.draft.input,
            images: st.draft.pendingImages.map(item => item.id),
            docs: st.draft.pendingDocs.map(item => item.id),
            popover: app.previewQuote.show,
            askSessionId: app.previewQuote.askSessionId,
          };
        }"""
    )
    assert sent["opts"]["sessionId"] == "chat-side-question"
    assert sent["opts"]["permissionMode"] == "default"
    assert "引用自我的消息：" in sent["opts"]["detachedText"]
    assert "USER_SELECTION_MARKER" in sent["opts"]["detachedText"]
    assert "这里的结论为什么成立？" in sent["opts"]["detachedText"]
    assert sent["create"]["snapshot"]["sessionId"] == sid
    assert sent["create"]["snapshot"]["messageId"] == "selection-user"
    assert sent["currentId"] == sid
    assert sent["draft"] == "ASK_DRAFT_MUST_SURVIVE"
    assert sent["images"] == ["keep-image"]
    assert sent["docs"] == ["keep-doc"]
    assert sent["popover"] is True
    assert sent["askSessionId"] == "chat-side-question"
    _app_eval(
        page,
        """
        app._createPreviewSelectionAskSession = window.__selectionAskOriginalCreate;
        return true;
        """,
    )
    _assert_no_browser_errors(page, errors)


def test_chat_wheel_preserves_selection_across_messages(
    page: Page, backend_url, auth_token,
):
    """Scrolling the transcript must not collapse a live browser selection."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "selection-wheel-source"
    _bootstrap_session_for_real_load(page, sid, "Selection wheel source")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.messages = [
          {
            role: "assistant", text: "FIRST_WHEEL_SELECTION_MARKER",
            html: "<p>FIRST_WHEEL_SELECTION_MARKER</p>",
            uuid: "selection-wheel-first", _k: "selection-wheel-first",
            _noAnim: true,
          },
          {
            role: "assistant", text: "SECOND_WHEEL_SELECTION_MARKER",
            html: "<p>SECOND_WHEEL_SELECTION_MARKER</p>",
            uuid: "selection-wheel-second", _k: "selection-wheel-second",
            _noAnim: true,
          },
        ];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        app._activateTabState(arg);
        app.mobileTab = "chat";
        return true;
        """,
        sid,
    )
    page.wait_for_function(
        """sid => document.querySelectorAll(
          `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant p`).length === 2""",
        arg=sid,
    )

    selected = page.evaluate(
        """sid => {
          const nodes = document.querySelectorAll(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant p`);
          const range = document.createRange();
          range.selectNodeContents(nodes[0]);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
          return selection.toString();
        }""",
        sid,
    )
    assert selected == "FIRST_WHEEL_SELECTION_MARKER"
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.previewQuote.show && app.previewQuote.source === 'chat';
        }"""
    )

    after_wheel = page.evaluate(
        """() => {
          const body = document.querySelector('.chat-body');
          body.dispatchEvent(new WheelEvent('wheel', {
            deltaY: 320, bubbles: true, cancelable: true,
          }));
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            text: window.getSelection().toString(),
            rangeCount: window.getSelection().rangeCount,
            popover: app.previewQuote.show,
          };
        }"""
    )
    assert after_wheel == {
        "text": "FIRST_WHEEL_SELECTION_MARKER",
        "rangeCount": 1,
        "popover": False,
    }

    extended = page.evaluate(
        """sid => {
          const nodes = document.querySelectorAll(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant p`);
          const range = document.createRange();
          range.setStart(nodes[0].firstChild, 0);
          range.setEnd(nodes[1].firstChild, nodes[1].firstChild.length);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
          return selection.toString();
        }""",
        sid,
    )
    assert "FIRST_WHEEL_SELECTION_MARKER" in extended
    assert "SECOND_WHEEL_SELECTION_MARKER" in extended
    _assert_no_browser_errors(page, errors)


def test_session_todo_modal_uses_large_desktop_board(
    page: Page, backend_url, auth_token,
):
    """The three priority lanes should use the desktop viewport, not 480px."""
    _login(page, backend_url, auth_token)
    page.set_viewport_size({"width": 1440, "height": 900})
    todo_state = _app_eval(
        page,
        """
        const originalId = app.currentId;
        app.userTodos = [
          {id: 'todo-high', text: 'High item', completed: false, priority: 'high'},
          {id: 'todo-medium', text: 'Medium item', completed: false, priority: 'medium'},
          {id: 'todo-low', text: 'Low item', completed: false, priority: 'low'},
        ];
        app._persistGlobalUserTodos();
        const beforeSwitch = app.sessionTodoItems().map(item => item.id);
        app.currentId = 'different-conversation';
        const afterSwitch = app.sessionTodoItems().map(item => item.id);
        app.currentId = originalId;
        const highIndicator = app.sessionTodoIndicatorPriority();
        app.userTodos = app.userTodos.map(item => item.id === 'todo-high'
          ? {...item, completed: true} : item);
        const mediumIndicator = app.sessionTodoIndicatorPriority();
        app.userTodos = app.userTodos.map(item => item.id === 'todo-medium'
          ? {...item, completed: true} : item);
        const lowOnlyIndicator = app.sessionTodoIndicatorPriority();
        app.userTodos = app.userTodos.map(item => ({...item, completed: false}));
        app.sessionTodoOpen = true;
        return {
          beforeSwitch,
          afterSwitch,
          storageKey: app._globalUserTodoStorageKey(),
          highIndicator,
          mediumIndicator,
          lowOnlyIndicator,
        };
        """,
    )
    assert todo_state == {
        "beforeSwitch": ["todo-high", "todo-medium", "todo-low"],
        "afterSwitch": ["todo-high", "todo-medium", "todo-low"],
        "storageKey": "muselab.userTodos.global",
        "highIndicator": "high",
        "mediumIndicator": "medium",
        "lowOnlyIndicator": "",
    }
    modal = page.locator(".session-todo-modal")
    expect(modal).to_be_visible()
    box = modal.bounding_box()
    assert box is not None
    assert box["width"] >= 900
    assert box["height"] >= 600
    expect(modal.locator(".session-todo-lane")).to_have_count(3)
    expect(modal.locator(".session-todo-priority-select")).to_have_count(0)
    expect(modal.locator(".session-todo-move")).to_have_count(0)

    high_item = modal.locator(".session-todo-item", has_text="High item")
    high_item.locator(".session-todo-edit-button").click()
    edit = high_item.locator(".session-todo-edit")
    expect(edit).to_be_visible()
    expect(edit).to_be_focused()
    edit.fill("Edited high item")
    edit.press("Enter")
    expect(high_item.locator("strong")).to_have_text("Edited high item")
    saved = page.evaluate(
        """() => JSON.parse(localStorage.getItem(
          'muselab.userTodos.global') || '[]').find(item => item.id === 'todo-high')?.text"""
    )
    assert saved == "Edited high item"

    medium_item = modal.locator(".session-todo-item", has_text="Medium item")
    medium_item.locator(".session-todo-edit-button").click()
    medium_edit = medium_item.locator(".session-todo-edit")
    medium_edit.fill("Must not save")
    medium_edit.press("Escape")
    expect(medium_item.locator("strong")).to_have_text("Medium item")
    expect(modal).to_be_visible()

    # The grip is now the one movement affordance. Left/right crosses priority
    # lanes and up/down reorders within a lane, using the same persisted model
    # as pointer/native dragging.
    medium_grip = modal.locator('[data-todo-id="todo-medium"] .session-todo-grip')
    medium_grip.focus()
    medium_grip.press("ArrowLeft")
    expect(modal.locator('.session-todo-lane.is-high [data-todo-id="todo-medium"]')).to_be_visible()
    modal.locator('[data-todo-id="todo-medium"] .session-todo-grip').press("ArrowUp")
    ordered = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const saved = JSON.parse(localStorage.getItem(
            'muselab.userTodos.global') || '[]');
          return {
            high: app.sessionTodosForPriority('high').map(item => item.id),
            saved: saved.find(item => item.id === 'todo-medium')?.priority,
          };
        }"""
    )
    assert ordered == {"high": ["todo-medium", "todo-high"], "saved": "high"}

    low_item = modal.locator('[data-todo-id="todo-low"]')
    high_lane = modal.locator(".session-todo-lane.is-high")
    lane_box = high_lane.bounding_box()
    assert lane_box is not None
    low_item.drag_to(
        high_lane,
        target_position={"x": lane_box["width"] / 2, "y": lane_box["height"] - 8},
    )
    dragged = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const saved = JSON.parse(localStorage.getItem(
            'muselab.userTodos.global') || '[]');
          return {
            high: app.sessionTodosForPriority('high').map(item => item.id),
            saved: saved.find(item => item.id === 'todo-low')?.priority,
          };
        }"""
    )
    assert dragged == {
        "high": ["todo-medium", "todo-high", "todo-low"],
        "saved": "high",
    }

    touch_dragged = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const grip = document.querySelector(
            '[data-todo-id="todo-high"] .session-todo-grip');
          const lane = document.querySelector('.session-todo-lane.is-low');
          const rect = lane.getBoundingClientRect();
          const init = {
            pointerId: 41, pointerType: 'touch', button: 0, buttons: 1,
            clientX: rect.left + rect.width / 2,
            clientY: rect.bottom - 8,
            bubbles: true, cancelable: true,
          };
          grip.dispatchEvent(new PointerEvent('pointerdown', init));
          window.dispatchEvent(new PointerEvent('pointermove', init));
          window.dispatchEvent(new PointerEvent('pointerup', {...init, buttons: 0}));
          const saved = JSON.parse(localStorage.getItem(
            'muselab.userTodos.global') || '[]');
          return {
            low: app.sessionTodosForPriority('low').map(item => item.id),
            saved: saved.find(item => item.id === 'todo-high')?.priority,
          };
        }"""
    )
    assert touch_dragged == {"low": ["todo-high"], "saved": "low"}


def test_effort_fast_capabilities_and_session_restore(
    page: Page, backend_url, auth_token,
):
    """Per-model levels, Fast, tab restore and clean new-session defaults agree."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    result = _app_eval(
        page,
        """
        return (async () => {
          const sidA = "runtime-settings-a";
          const sidB = "runtime-settings-b";
          app.availableModels = [
            {
              model: "codex:gpt-5.6-sol", label: "Sol", group: "Codex Gateway",
              supports_effort: true,
              effort_levels: ["auto", "low", "medium", "high", "xhigh", "max", "ultra"],
              supports_fast: true,
            },
            {
              model: "basic-model", label: "Basic", group: "Basic",
              supports_effort: true, effort_levels: ["auto", "low"],
              supports_fast: false,
            },
            {
              model: "claude-sonnet-4-6", label: "Sonnet", group: "Claude",
              supports_effort: true,
              effort_levels: ["auto", "low", "medium", "high", "xhigh", "max", "ultra"],
              supports_fast: false,
            },
            {
              model: "claude-opus-4-8", label: "Opus", group: "Claude",
              supports_effort: true,
              effort_levels: ["auto", "low", "medium", "high", "xhigh", "max", "ultra"],
              supports_fast: false,
            },
          ];
          app.sessions = [
            { id: sidA, name: "A", model: "codex:gpt-5.6-sol",
              permission: "bypassPermissions", effort: "ultra",
              service_tier: "fast", message_count: 2 },
            { id: sidB, name: "B", model: "basic-model",
              permission: "bypassPermissions", effort: "auto",
              service_tier: "", message_count: 0 },
          ];
          app.openTabIds = [sidA, sidB];
          app.tabState = {};
          app.currentId = sidA;
          app.model = "codex:gpt-5.6-sol";
          app._activateTabState(sidA);
          const sol = {
            levels: app.effortChoices(app.model).map(item => item.value),
            effort: app.effort,
            tier: app.serviceTier,
            fast: app._supportsFast(app.model),
          };

          app.currentId = sidB;
          app.model = "basic-model";
          app._activateTabState(sidB);
          const basic = {
            levels: app.effortChoices(app.model).map(item => item.value),
            effort: app.effort,
            tier: app.serviceTier,
            fast: app._supportsFast(app.model),
          };
          // Capability drift must not hide the only escape hatch from a
          // previously-persisted unsupported override.
          app.effort = "ultra";
          app.serviceTier = "fast";
          const drift = {
            effortVisible: app._showEffortControl(app.model),
            fastVisible: app._showFastControl(app.model),
            levels: app.effortChoices(app.model).map(item => item.value),
          };
          app.effort = "auto";
          app.serviceTier = "";
          const claude = {
            sonnet: app.effortChoices("claude-sonnet-4-6").map(item => item.value),
            opus: app.effortChoices("claude-opus-4-8").map(item => item.value),
          };

          // Cancelling a history-session model switch must not reset A's
          // persisted effort/tier before confirmation.
          app.currentId = sidA;
          app.model = "codex:gpt-5.6-sol";
          app._activateTabState(sidA);
          app.model = "basic-model";
          app.confirm = async () => false;
          await app.onModelChange();
          const source = app.sessions.find(session => session.id === sidA);
          const afterCancel = {
            model: app.model,
            effort: source.effort,
            tier: source.service_tier,
          };

          // A new tab is always server-compatible auto + standard tier, never
          // inherited from the previously active ultra/Fast session.
          app.defaultModel = "codex:gpt-5.6-sol";
          const realRegister = app._registerOptimisticSession;
          app._registerOptimisticSession = async () => true;
          const fresh = app.newSession();
          const freshState = {
            metaEffort: fresh.effort,
            metaTier: fresh.service_tier,
            rootEffort: app.effort,
            rootTier: app.serviceTier,
          };
          app._registerOptimisticSession = realRegister;
          return { sol, basic, drift, claude, afterCancel, freshState };
        })();
        """,
    )

    assert result["sol"] == {
        "levels": ["auto", "low", "medium", "high", "xhigh", "max", "ultra"],
        "effort": "ultra",
        "tier": "fast",
        "fast": True,
    }
    assert result["basic"] == {
        "levels": ["auto", "low"],
        "effort": "auto",
        "tier": "",
        "fast": False,
    }
    assert result["drift"] == {
        "effortVisible": True,
        "fastVisible": True,
        "levels": ["auto", "low", "ultra"],
    }
    assert result["claude"] == {
        "sonnet": ["auto", "low", "medium", "high", "max"],
        "opus": ["auto", "low", "medium", "high", "xhigh", "max"],
    }
    assert result["afterCancel"] == {
        "model": "codex:gpt-5.6-sol",
        "effort": "ultra",
        "tier": "fast",
    }
    assert result["freshState"] == {
        "metaEffort": "auto",
        "metaTier": "",
        "rootEffort": "auto",
        "rootTier": "",
    }

    race = _app_eval(
        page,
        """
        return (async () => {
          const realFetch = window.fetch;
          const realFetchTabUsage = app._fetchTabUsage;
          const realCheckActiveTurn = app._checkActiveTurn;
          const realScheduleIdlePreload = app._scheduleIdlePreload;
          const realRefreshSessions = app.refreshSessions;
          const realRefreshOutline = app.refreshOutlineFromBackend;
          const realEffortAllowed = app._effortAllowed;
          const realSupportsFast = app._supportsFast;
          const response = payload => ({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => payload,
            text: async () => "",
          });
          try {
            app._fetchTabUsage = async () => {};
            app._checkActiveTurn = () => {};
            app._scheduleIdlePreload = () => {};
            app.refreshSessions = async () => true;
            app.refreshOutlineFromBackend = async () => {};
            app._effortAllowed = () => true;
            app._supportsFast = () => true;
            const runtimeModel = {
              model: "codex:gpt-5.6-sol", label: "Sol", group: "Codex Gateway",
              supports_effort: true,
              effort_levels: ["auto", "low", "medium", "high", "xhigh", "max", "ultra"],
              supports_fast: true,
            };
            const basicModel = {
              model: "basic-model", label: "Basic", group: "Basic",
              supports_effort: true, effort_levels: ["auto", "low"],
              supports_fast: false,
            };
            app.availableModels = [runtimeModel, basicModel];

            // Model uses the same optimistic-registration barrier: PATCH must
            // not race ahead of the session POST and deterministically 404.
            const modelSid = "runtime-model-optimistic-race";
            const modelMeta = {
              id: modelSid, name: "Model optimistic",
              model: "codex:gpt-5.6-sol", permission: "bypassPermissions",
              effort: "auto", service_tier: "", message_count: 0,
            };
            app.sessions = [{ ...modelMeta }];
            app.openTabIds = [modelSid];
            app.tabState = {};
            app._optimisticMetas = { [modelSid]: { ...modelMeta } };
            app._sessionRegistrationPromises = {};
            app.currentId = modelSid;
            app.model = modelMeta.model;
            app.messages = app._ensureTabState(modelSid).messages;
            const modelCalls = [];
            let releaseModelRegistration;
            const modelRegistrationGate = new Promise(resolve => {
              releaseModelRegistration = resolve;
            });
            window.fetch = async (url, options = {}) => {
              const path = String(url);
              const method = options.method || "GET";
              if (path === "/api/chat/sessions" && method === "POST") {
                modelCalls.push("register:start");
                await modelRegistrationGate;
                modelCalls.push("register:return");
                return response({ ...modelMeta });
              }
              if (path === "/api/chat/sessions/" + modelSid
                  && method === "PATCH") {
                modelCalls.push("patch:" + JSON.stringify(JSON.parse(options.body)));
                return response({});
              }
              return realFetch(url, options);
            };
            app.model = "basic-model";
            const modelWrite = app.onModelChange();
            await new Promise(resolve => setTimeout(resolve, 0));
            const modelBeforeRelease = [...modelCalls];
            releaseModelRegistration();
            const modelWriteResult = await modelWrite;
            const modelRegistration = {
              beforeRelease: modelBeforeRelease,
              calls: modelCalls,
              result: modelWriteResult,
              rootModel: app.model,
              metaModel: app.sessions.find(row => row.id === modelSid)?.model,
            };

            // An optimistic tab must finish POST registration before either
            // runtime PATCH. Its stale auto/off response must also retain both
            // values the user selected while registration was in flight.
            const optimisticSid = "runtime-settings-optimistic-race";
            const optimisticMeta = {
              id: optimisticSid, name: "Optimistic", model: "codex:gpt-5.6-sol",
              permission: "bypassPermissions", effort: "auto", service_tier: "",
              message_count: 0,
            };
            app.sessions = [{ ...optimisticMeta }];
            app.openTabIds = [optimisticSid];
            app.tabState = {};
            app._optimisticMetas = { [optimisticSid]: { ...optimisticMeta } };
            app._sessionRegistrationPromises = {};
            app.currentId = optimisticSid;
            app.model = optimisticMeta.model;
            app._activateTabState(optimisticSid);

            const calls = [];
            let releaseRegistration;
            const registrationGate = new Promise(resolve => {
              releaseRegistration = resolve;
            });
            window.fetch = async (url, options = {}) => {
              const path = String(url);
              const method = options.method || "GET";
              if (path === "/api/chat/sessions" && method === "POST") {
                calls.push("register:start");
                await registrationGate;
                calls.push("register:return");
                return response({ ...optimisticMeta });
              }
              if (path === "/api/chat/sessions/" + optimisticSid
                  && method === "PATCH") {
                calls.push("patch:" + JSON.stringify(JSON.parse(options.body)));
                return response({});
              }
              return realFetch(url, options);
            };

            app.effort = "ultra";
            const effortWrite = app.onEffortChange();
            app.serviceTier = "fast";
            const tierWrite = app.onServiceTierChange(true);
            await new Promise(resolve => setTimeout(resolve, 0));
            const beforeRelease = [...calls];
            releaseRegistration();
            const writeResults = await Promise.all([effortWrite, tierWrite]);
            const optimisticState = app._ensureTabState(optimisticSid);
            const optimisticRow = app.sessions.find(row => row.id === optimisticSid);
            const optimistic = {
              beforeRelease,
              calls,
              writeResults,
              stateEffort: optimisticState.effort,
              stateTier: optimisticState.serviceTier,
              metaEffort: optimisticRow.effort,
              metaTier: optimisticRow.service_tier,
            };

            // A GET that started before the two PATCH intents returns auto/off
            // after their expected markers have already cleared. Generation
            // ownership must keep both the tab state and loaded row on the new
            // values instead of accepting that stale response.
            const loadSid = "runtime-settings-load-race";
            const loadMeta = {
              id: loadSid, name: "Load race", model: "codex:gpt-5.6-sol",
              permission: "bypassPermissions", effort: "auto", service_tier: "",
              message_count: 0,
            };
            app.availableModels = [runtimeModel, basicModel];
            app.sessions = [{ ...loadMeta }];
            app.openTabIds = [loadSid];
            app.tabState = {};
            app._optimisticMetas = {};
            app._sessionRegistrationPromises = {};
            app.currentId = loadSid;
            app.model = loadMeta.model;
            app._activateTabState(loadSid);

            let releaseLoad;
            const loadGate = new Promise(resolve => { releaseLoad = resolve; });
            let loadStarted = false;
            let generationAtFetch = null;
            window.fetch = async (url, options = {}) => {
              const path = String(url);
              const method = options.method || "GET";
              if (path.startsWith("/api/chat/sessions/" + loadSid + "?")
                  && method === "GET") {
                loadStarted = true;
                generationAtFetch = app._ensureTabState(loadSid)._runtimeSettingsGeneration;
                await loadGate;
                return response({
                  ...loadMeta, messages: [], offset: 0, total: 0,
                  history_generation: "stale-generation", updated_at: 1,
                });
              }
              if (path === "/api/chat/sessions/" + loadSid
                  && method === "PATCH") {
                return response({});
              }
              return realFetch(url, options);
            };

            const staleLoad = app.loadSession(loadSid, { quiet: true });
            app.model = "basic-model";
            const modelOk = await app.onModelChange();
            app.effort = "ultra";
            const effortOk = await app.onEffortChange();
            app.serviceTier = "fast";
            const tierOk = await app.onServiceTierChange(true);
            const loadState = app._ensureTabState(loadSid);
            const generationAfterWrites = loadState._runtimeSettingsGeneration;
            // Exercise generation ownership independently of the optimistic
            // expected leases: even after those markers are gone, the old GET
            // must not restore its pre-PATCH model/effort/tier snapshot.
            loadState._modelExpected = null;
            loadState._effortExpected = null;
            loadState._serviceTierExpected = null;
            releaseLoad();
            const loadOk = await staleLoad;
            const loadRow = app.sessions.find(row => row.id === loadSid);
            const staleRead = {
              loadStarted, modelOk, effortOk, tierOk, loadOk,
              generationAtFetch,
              generationAfterWrites,
              generationAfterLoad: loadState._runtimeSettingsGeneration,
              stateEffort: loadState.effort,
              stateTier: loadState.serviceTier,
              rootModel: app.model,
              metaModel: loadRow.model,
              metaEffort: loadRow.effort,
              metaTier: loadRow.service_tier,
            };

            // A missed echo is only a short optimistic lease. Once expired,
            // a cross-device authoritative value must flow through unchanged.
            const ttlSid = "runtime-settings-ttl";
            const ttlState = app._ensureTabState(ttlSid);
            const expiredAt = Date.now()
              - app.SESSION_SETTING_EXPECTED_TTL_MS - 1;
            ttlState._modelExpected = {
              value: "basic-model", fallback: "codex:gpt-5.6-sol",
              at: expiredAt,
            };
            ttlState._effortExpected = {
              value: "ultra", fallback: "auto", at: expiredAt,
            };
            ttlState._serviceTierExpected = {
              value: "fast", fallback: "", at: expiredAt,
            };
            const retainedAfterTtl = app._retainExpectedSessionSettings({
              id: ttlSid, model: "codex:gpt-5.6-sol",
              effort: "low", service_tier: "",
            });
            const ttl = {
              model: retainedAfterTtl.model,
              effort: retainedAfterTtl.effort,
              tier: retainedAfterTtl.service_tier,
              modelExpected: ttlState._modelExpected,
              effortExpected: ttlState._effortExpected,
              tierExpected: ttlState._serviceTierExpected,
            };

            // If both persisted overrides became unsupported, either visible
            // escape hatch must clear the complete tuple in one PATCH. Two
            // partial PATCHes would each fail backend target validation on the
            // other still-invalid value.
            app._effortAllowed = realEffortAllowed;
            app._supportsFast = realSupportsFast;
            const contractionCalls = [];
            window.fetch = async (url, options = {}) => {
              if ((options.method || "GET") === "PATCH") {
                contractionCalls.push(JSON.parse(options.body));
                return response({});
              }
              return realFetch(url, options);
            };
            const contractionRun = async (sid, action) => {
              app.sessions = [{
                id: sid, model: "basic-model", effort: "ultra",
                service_tier: "fast", message_count: 0,
              }];
              app.openTabIds = [sid];
              app.tabState = {};
              app.currentId = sid;
              app.model = "basic-model";
              app._activateTabState(sid);
              const ok = await action();
              const row = app.sessions[0];
              return {ok, effort: row.effort, tier: row.service_tier};
            };
            const clearViaEffort = await contractionRun(
              "runtime-contraction-effort",
              async () => {
                app.effort = "auto";
                return await app.onEffortChange();
              },
            );
            const clearViaTier = await contractionRun(
              "runtime-contraction-tier",
              async () => {
                app.serviceTier = "";
                return await app.onServiceTierChange(false);
              },
            );
            const contraction = {
              calls: contractionCalls, clearViaEffort, clearViaTier,
            };

            // x-model updates just before @change. Handler guards must restore
            // the old owner mirrors and issue no request during a cold
            // workspace transition, even if invoked programmatically.
            const guardSid = "runtime-settings-workspace-guard";
            app.sessions = [{
              id: guardSid, model: "codex:gpt-5.6-sol",
              effort: "low", service_tier: "", message_count: 0,
            }];
            app.openTabIds = [guardSid];
            app.tabState = {};
            app.currentId = guardSid;
            app.model = "codex:gpt-5.6-sol";
            app._activateTabState(guardSid);
            let guardedFetches = 0;
            window.fetch = async () => {
              guardedFetches += 1;
              return response({});
            };
            app.workspaceSwitching = true;
            app.model = "basic-model";
            const guardedModel = await app.onModelChange();
            app.effort = "ultra";
            const guardedEffort = await app.onEffortChange();
            app.serviceTier = "fast";
            const guardedTier = await app.onServiceTierChange(true);
            app.workspaceSwitching = false;
            const workspaceGuard = {
              results: [guardedModel, guardedEffort, guardedTier],
              fetches: guardedFetches,
              model: app.model,
              effort: app.effort,
              tier: app.serviceTier,
            };
            return {
              modelRegistration, optimistic, staleRead, ttl,
              contraction, workspaceGuard,
            };
          } finally {
            app.workspaceSwitching = false;
            window.fetch = realFetch;
            app._fetchTabUsage = realFetchTabUsage;
            app._checkActiveTurn = realCheckActiveTurn;
            app._scheduleIdlePreload = realScheduleIdlePreload;
            app.refreshSessions = realRefreshSessions;
            app.refreshOutlineFromBackend = realRefreshOutline;
            app._effortAllowed = realEffortAllowed;
            app._supportsFast = realSupportsFast;
          }
        })();
        """,
    )
    assert race["modelRegistration"] == {
        "beforeRelease": ["register:start"],
        "calls": [
            "register:start",
            "register:return",
            'patch:{"model":"basic-model","effort":"auto","service_tier":""}',
        ],
        "result": True,
        "rootModel": "basic-model",
        "metaModel": "basic-model",
    }
    assert race["optimistic"] == {
        "beforeRelease": ["register:start"],
        "calls": [
            "register:start",
            "register:return",
            'patch:{"effort":"ultra","service_tier":""}',
            'patch:{"effort":"ultra","service_tier":"fast"}',
        ],
        "writeResults": [True, True],
        "stateEffort": "ultra",
        "stateTier": "fast",
        "metaEffort": "ultra",
        "metaTier": "fast",
    }
    assert race["staleRead"] == {
        "loadStarted": True,
        "modelOk": True,
        "effortOk": True,
        "tierOk": True,
        "loadOk": True,
        "generationAtFetch": 0,
        "generationAfterWrites": 3,
        "generationAfterLoad": 3,
        "stateEffort": "ultra",
        "stateTier": "fast",
        "rootModel": "basic-model",
        "metaModel": "basic-model",
        "metaEffort": "ultra",
        "metaTier": "fast",
    }
    assert race["ttl"] == {
        "model": "codex:gpt-5.6-sol",
        "effort": "low",
        "tier": "",
        "modelExpected": None,
        "effortExpected": None,
        "tierExpected": None,
    }
    assert race["contraction"] == {
        "calls": [
            {"effort": "auto", "service_tier": ""},
            {"effort": "auto", "service_tier": ""},
        ],
        "clearViaEffort": {"ok": True, "effort": "auto", "tier": ""},
        "clearViaTier": {"ok": True, "effort": "auto", "tier": ""},
    }
    assert race["workspaceGuard"] == {
        "results": [False, False, False],
        "fetches": 0,
        "model": "codex:gpt-5.6-sol",
        "effort": "low",
        "tier": "",
    }
    _assert_no_browser_errors(page, errors)


def test_mobile_completed_turn_can_fork_from_that_point(
    page: Page, backend_url, auth_token,
):
    """The point-fork action stays tappable and sends the completed turn UUID."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-fork-source"
    fork_sid = "perf-fork-result"
    boundary = "fork-boundary-assistant"
    requests: list[dict] = []

    def handle_fork(route):
        requests.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": fork_sid,
                "session_id": fork_sid,
                "name": "Fork source · 分支",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "cwd": "",
                "forked_from": sid,
                "forked_from_name": "Fork source",
                "forked_from_message_id": boundary,
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}/fork", handle_fork)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Fork source")
    _app_eval(
        page,
        """
        const source = app.sessions[0];
        source.message_count = 2;
        source.turn_count = 1;
        const st = app._ensureTabState(arg.sid);
        st._loaded = true;
        st.messages = [
          {
            role: "user", text: "FORK_SOURCE_USER",
            uuid: "fork-source-user", ts: 1700020000,
            _k: "fork-source-user", _noAnim: true,
          },
          {
            role: "assistant", text: "FORK_SOURCE_ASSISTANT",
            html: "<p>FORK_SOURCE_ASSISTANT</p>",
            uuid: arg.boundary, ts: 1700020001,
            _k: arg.boundary, _noAnim: true,
          },
        ];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        app._activateTabState(arg.sid);
        app.openTab = async id => {
          if (!app.openTabIds.includes(id)) app.openTabIds.push(id);
          app.currentId = id;
          const forkState = app._ensureTabState(id);
          forkState._loaded = true;
          forkState.messages = [];
          app._activateTabState(id);
        };
        return true;
        """,
        {"sid": sid, "forkSid": fork_sid, "boundary": boundary},
    )

    action = page.locator(".msg-pane:visible .turn-fork-btn:visible")
    expect(action).to_be_visible(timeout=3000)
    box = action.bounding_box()
    assert box is not None and box["width"] >= 30 and box["height"] >= 30
    action.click()
    page.wait_for_function(
        """expected => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.currentId === expected;
        }""",
        arg=fork_sid,
    )
    assert requests
    assert requests[0]["up_to_message_id"] == boundary
    assert requests[0]["title"] == "Fork source · Fork"
    lineage = _app_eval(
        page,
        """
        const forked = app.sessions.find(s => s.id === arg);
        return forked && {
          source: forked.forked_from,
          boundary: forked.forked_from_message_id,
        };
        """,
        fork_sid,
    )
    assert lineage == {"source": sid, "boundary": boundary}
    _assert_no_browser_errors(page, errors)


def test_mobile_long_history_switching_does_not_blank(page: Page, backend_url, auth_token):
    """Switch repeatedly between long normalized histories on a mobile viewport."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        const sessionIds = Array.from({ length: 6 }, (_, i) => `perf-history-${i}`);
        app.sessions = sessionIds.map((id, idx) => ({
          id, name: `Perf history ${idx}`, updated_at: now / 1000 - idx,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }));
        app.openTabIds = sessionIds.slice();
        app.tabState = {};
        for (const [idx, id] of sessionIds.entries()) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messages = [];
          for (let i = 0; i < 90; i++) {
            st.messages.push({
              role: i % 2 === 0 ? "user" : "assistant",
              text: `history ${idx}:${i} `.repeat(18),
              html: i % 2 === 0 ? "" : `<p>history ${idx}:${i} ${"tail ".repeat(18)}</p>`,
              ts: now + i,
              _k: `${id}-${i}`,
              _noAnim: true,
            });
          }
          st.messageRange.visibleEnd = st.messages.length;
          st.messageRange.total = st.messages.length;
          app.tabState[id] = st;
          app._ensureTabState(id);
          app._scheduleLiveMessageViewport(st);
        }
        app.currentId = sessionIds[0];
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app._activateTabState(app.currentId);
        app.$nextTick(() => app.scrollToBottom(true));
        return true;
        """,
    )
    history_size = 90

    page.wait_for_function(
        """historySize => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none");
          const rendered = panes.length === 1
            ? panes[0].querySelectorAll(".msg").length : 0;
          return rendered > 0 && rendered < historySize;
        }""",
        arg=history_size,
        timeout=5000,
    )

    for sid in [f"perf-history-{i}" for i in [1, 2, 3, 4, 5, 0, 5]]:
        _app_eval(
            page,
            """
            app.currentId = arg;
            app.messagesReady = true;
            app.messagesLoading = false;
            app._activateTabState(arg);
            app.$nextTick(() => app.scrollToBottom(true));
            """,
            sid,
        )
        expected_tail = f"history {sid.rsplit('-', 1)[1]}:89"
        try:
            page.wait_for_function(
                """({ expected, historySize }) => {
                  const panes = Array.from(document.querySelectorAll(".msg-pane"))
                    .filter(p => getComputedStyle(p).display !== "none");
                  return panes.some(p => p.textContent.includes(expected)
                    && p.querySelectorAll(".msg").length > 0
                    && p.querySelectorAll(".msg").length < historySize);
                }""",
                arg={"expected": expected_tail, "historySize": history_size},
                timeout=5000,
            )
        except TimeoutError as exc:
            diag = page.evaluate(
                """() => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  return {
                    currentId: app.currentId,
                    paneCount: document.querySelectorAll(".msg-pane").length,
                    openTabIds: app.openTabIds,
                    messagesLength: app.messages.length,
                    visiblePanes: Array.from(document.querySelectorAll(".msg-pane"))
                      .filter(p => getComputedStyle(p).display !== "none")
                      .map(p => ({ count: p.querySelectorAll(".msg").length,
                                   text: p.textContent.slice(0, 400) })),
                  };
                }"""
            )
            raise AssertionError(f"target tail not visible: {expected_tail}; diag={diag}") from exc
        snap = _visible_pane_with_text_snapshot(page, expected_tail)
        assert 0 < snap["msgCount"] < history_size
        assert expected_tail in snap["text"]
        assert page.locator(".msg-pane").count() <= 1
        assert page.locator(".msg-pane").count() <= 1

    _assert_no_browser_errors(page, errors)


def test_desktop_session_switch_remounts_one_pane_and_keeps_composer_stable(
    page: Page, backend_url: str, auth_token: str,
):
    """Desktop remounts one virtualized pane without footer/layout jumps."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    session_ids = [f"desktop-warm-{i}" for i in range(4)]
    payload = {
        "ids": session_ids,
        "messages": {
            sid: _make_mixed_messages(40, f"DESKTOP_WARM_{idx}")
            for idx, sid in enumerate(session_ids)
        },
    }
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.sessions = arg.ids.map((id, index) => ({
          id, name: `Desktop warm ${index}`, updated_at: Date.now() / 1000 - index,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }));
        app.openTabIds = arg.ids.slice();
        app.tabState = {};
        for (const id of arg.ids) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messages = app._historyEnvelopes(id, arg.messages[id]);
          st.messageRange.visibleEnd = st.messages.length;
          st.messageRange.total = st.messages.length;
          st.messagesReady = true;
          st.messagesLoading = false;
          st.atBottom = true;
          app.tabState[id] = st;
          app._ensureTabState(id);
        }
        app.currentId = arg.ids[0];
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(app.currentId);
        return true;
        """,
        payload,
    )
    page.wait_for_function(
        """() => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"));
          const visible = panes.filter(
            pane => getComputedStyle(pane).display !== "none");
          return panes.length === 1
            && visible.length === 1
            && visible[0].querySelectorAll(".msg").length === 40;
        }"""
    )
    before = page.locator(".chat-input").bounding_box()
    assert before is not None

    switches = page.evaluate(
        """async ids => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const out = [];
          for (const id of ids) {
            const started = performance.now();
            app.currentId = id;
            await app.switchSession();
            await new Promise(resolve =>
              requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const panes = Array.from(document.querySelectorAll(".msg-pane"));
            const visible = panes.filter(
              pane => getComputedStyle(pane).display !== "none");
            out.push({
              elapsed: performance.now() - started,
              paneCount: document.querySelectorAll(".msg-pane").length,
              panes: panes.length,
              visible: visible.length,
              visibleMessages: visible[0]?.querySelectorAll(".msg").length || 0,
              ready: app.messagesReady,
              skeleton: getComputedStyle(
                document.querySelector(".chat-skeleton")).display,
            });
          }
          return out;
        }""",
        session_ids[1:] + session_ids[:1],
    )
    after = page.locator(".chat-input").bounding_box()
    assert after is not None
    elapsed = sorted(row["elapsed"] for row in switches)
    # Shared/low-memory CI can produce one scheduling outlier; the sustained
    # interaction is what users feel across repeated remounts.
    assert elapsed[len(elapsed) // 2] < 700, switches
    assert max(elapsed) < 1500, switches
    assert all(row["panes"] == 1 and row["visible"] == 1 for row in switches)
    assert all(row["visibleMessages"] == 40 for row in switches)
    assert all(row["ready"] and row["skeleton"] == "none" for row in switches)
    assert abs(after["y"] - before["y"]) < 1
    assert abs(after["height"] - before["height"]) < 1
    _assert_no_browser_errors(page, errors)


def test_mobile_windowed_load_session_pages_older_history(page: Page, backend_url, auth_token):
    """Drive real loadSession/tail and loadEarlierMessages server paging."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-windowed-history"
    messages = _make_mixed_messages(180, "WINDOW_MSG")
    requests = _route_windowed_session(page, sid, messages)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Perf windowed history")

    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && app.messagesLoading === false
            && app.messages.some(m => (m.text || "").includes("WINDOW_MSG_179"));
        }""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant").last).to_contain_text(
        "WINDOW_MSG_179", timeout=10000
    )

    state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          messages: st.messages.length,
          visible: st.messageRange.visibleEnd - st.messageRange.visibleStart,
          earlier: st.messageRange.visibleStart,
          later: st.messages.length - st.messageRange.visibleEnd,
          loadedOffset: st.messageRange.offset,
          total: st.messageRange.total,
          hasMore: st._hasMoreHistory,
          paneCount: document.querySelectorAll(".msg-pane").length,
          ready: app.messagesReady,
          bodyText: document.querySelector(".chat-body")?.textContent || "",
        };
        """,
        sid,
    )
    assert requests and requests[0]["tail"] == 75
    assert state["messages"] == 75
    assert state["visible"] <= 60
    assert state["loadedOffset"] == 105
    assert state["total"] == 180
    assert state["hasMore"] is True
    assert state["paneCount"] <= 1
    assert "WINDOW_MSG_179" in state["bodyText"]
    assert "WINDOW_MSG_000" not in state["bodyText"]
    assert page.locator(".msg-pane").count() <= 1
    assert page.locator(".msg-pane:visible .msg").count() <= 75

    # Traverse the full server-paged history. Normalized envelopes stay reachable
    # while the viewport scheduler mounts message 0 only when it enters view.
    for _ in range(24):
        _app_eval(
            page,
            """
            const body = app.$refs.chatBody;
            app.atBottom = false;
            app._ensureTabState(arg).atBottom = false;
            body.scrollTop = 0;
            app._syncMessageViewport(arg);
            return app.loadEarlierMessages(arg);
            """,
            sid,
        )
        page.wait_for_timeout(50)
        if _app_eval(
            page,
            """return app.messages.some(m => (m.text || "").includes("WINDOW_MSG_000"));""",
        ):
            break

    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && app.messages.some(m => (m.text || "").includes("WINDOW_MSG_000"));
        }""",
        timeout=10000,
    )
    final_state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          messages: st.messages.length,
          visible: st.messageRange.visibleEnd - st.messageRange.visibleStart,
          earlier: st.messageRange.visibleStart,
          later: st.messages.length - st.messageRange.visibleEnd,
          loadedOffset: st.messageRange.offset,
          total: st.messageRange.total,
          hasMore: st._hasMoreHistory,
          hasServerLater: st._hasServerLater,
          cached: st.messages.length,
          ready: app.messagesReady,
          visibleText: Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none")
            .map(p => p.textContent).join("\\n"),
          bodyHeight: document.querySelector(".chat-body")?.getBoundingClientRect().height || 0,
        };
        """,
        sid,
    )
    assert any(req["offset"] == 0 for req in requests), requests
    assert any("history_generation=gen-e2e-1" in req["url"] for req in requests[1:]), requests
    assert final_state["loadedOffset"] == 0
    assert final_state["total"] == 180
    assert final_state["ready"] is True
    assert final_state["bodyHeight"] > 100
    assert "WINDOW_MSG_000" in final_state["visibleText"]
    assert final_state["messages"] == 180
    assert final_state["cached"] == 180
    assert final_state["later"] == 0
    assert final_state["hasServerLater"] is False
    latest_after_load_earlier = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          latestInMessages: st.messages.some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInLater: st.messages.slice(st.messageRange.visibleEnd)
            .some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInDom: document.querySelector(".chat-body")?.textContent.includes("WINDOW_MSG_179"),
          hasServerLater: st._hasServerLater,
          ready: st.messagesReady,
        };
        """,
        sid,
    )
    assert latest_after_load_earlier == {
        "latestInMessages": True,
        "latestInLater": False,
        "latestInDom": False,
        "hasServerLater": False,
        "ready": True,
    }
    _app_eval(page, "app.returnToLatest(arg); return true;", sid)
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.msg-pane'))
          .filter(p => getComputedStyle(p).display !== 'none')
          .some(p => p.textContent.includes('WINDOW_MSG_179'))""",
        timeout=5000,
    )
    assert _app_eval(
        page,
        """const st = app._ensureTabState(arg);
        return st.messages.length - st.messageRange.visibleEnd;""",
        sid,
    ) == 0
    assert page.locator(".msg-pane").count() <= 1
    assert page.locator(".msg-pane:visible .msg").count() <= 60

    _assert_no_browser_errors(page, errors)


def test_history_pagination_keeps_stable_cross_page_keys_without_remounting(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-cross-page-render-keys"
    messages = _make_mixed_messages(180, "CROSS_PAGE_KEY")
    requests = _route_windowed_session(page, sid, messages)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Cross-page render keys")

    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady && !app.messagesLoading;
        }""",
        timeout=10000,
    )

    # Drain only the tail response's local stash. The next click must execute
    # the real offset/limit history fetch and prepend a page across the boundary.
    for _ in range(20):
        local_earlier = _app_eval(
            page, "return app._ensureTabState(arg).messageRange.visibleStart;", sid
        )
        if local_earlier == 0:
            break
        _app_eval(page, "return app.loadEarlierMessages(arg);", sid)
    assert local_earlier == 0

    before = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const mounted = st.messages.find(m => m.uuid === "CROSS_PAGE_KEY-tr-110");
        window.__crossPageMountedObject = mounted;
        return {
          found: !!mounted,
          key: mounted?._k || "",
          loadedOffset: st.messageRange.offset,
        };
        """,
        sid,
    )
    assert before["found"] is True
    assert before["loadedOffset"] > 0

    _app_eval(page, "return app.loadEarlierMessages(arg);", sid)
    after = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const all = st.messages;
        const mounted = all.find(m => m.uuid === "CROSS_PAGE_KEY-tr-110");
        const olderUser = all.find(m => m.uuid === "CROSS_PAGE_KEY-u-104");
        const olderAssistant = all.find(m => m.uuid === "CROSS_PAGE_KEY-a-101");
        const olderTool = all.find(m => m.uuid === "CROSS_PAGE_KEY-tr-102");
        const keys = all.map(m => m._k);
        return {
          sameMountedObject: mounted === window.__crossPageMountedObject,
          mountedKey: mounted?._k || "",
          olderUserKey: olderUser?._k || "",
          olderAssistantKey: olderAssistant?._k || "",
          olderToolKey: olderTool?._k || "",
          allNonempty: keys.every(key => typeof key === "string" && key.trim()),
          unique: new Set(keys).size === keys.length,
        };
        """,
        sid,
    )

    assert any(request["offset"] < 105 for request in requests[1:]), requests
    assert after["sameMountedObject"] is True
    assert after["mountedKey"] == before["key"] == f"{sid}:uuid:CROSS_PAGE_KEY-tr-110"
    assert after["olderUserKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-u-104"
    assert after["olderAssistantKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-a-101"
    assert after["olderToolKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-tr-102"
    assert after["allNonempty"] is True
    assert after["unique"] is True
    _assert_no_browser_errors(page, errors)


def test_outline_around_conflict_retries_and_returns_to_real_tail(
    page: Page, backend_url, auth_token,
):
    """A stale outline generation retries its target and preserves full order."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-around-conflict"
    target_uuid = "around-target"
    calls: list[dict] = []
    around_messages = _make_mixed_messages(85, "AROUND_MSG")
    around_messages[70] = {
        "role": "user",
        "text": "AROUND_TARGET_VISIBLE",
        "uuid": target_uuid,
        "ts": 1_700_020_070,
    }
    latest_messages = _make_mixed_messages(75, "LATEST_MSG")
    latest_messages[-1] = {
        "role": "assistant",
        "text": "CANONICAL_LATEST_VISIBLE",
        "html": "<p>CANONICAL_LATEST_VISIBLE</p>",
        "uuid": "canonical-latest",
        "ts": 1_700_030_000,
    }

    def handle(route):
        qs = parse_qs(urlparse(route.request.url).query)
        calls.append({key: values[0] for key, values in qs.items()})
        if "around_uuid" in qs and qs.get("history_generation") == ["gen-old"]:
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({
                    "detail": {
                        "error": "history_generation_mismatch",
                        "history_generation": "gen-new",
                    },
                }),
            )
            return
        if "around_uuid" in qs:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "id": sid,
                    "messages": around_messages,
                    "offset": 200,
                    "total": 500,
                    "has_more": True,
                    "has_later": True,
                    "history_generation": "gen-new",
                    "history_order": "full",
                }),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Perf around conflict",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": latest_messages,
                "offset": 425,
                "total": 500,
                "has_more": True,
                "has_later": False,
                "history_generation": "gen-new",
                "history_order": "normal",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Perf around conflict")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st.messageRange.generation = "gen-old";
        st._loaded = true;
        st.messagesReady = true;
        return true;
        """,
        sid,
    )

    loaded = _app_eval(
        page,
        "return app._loadAroundMessage(arg.sid, arg.uuid);",
        {"sid": sid, "uuid": target_uuid},
    )
    assert loaded is True
    page.wait_for_function(
        """uuid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return st.messages.some(m => m.uuid === uuid)
            && document.querySelector(`.msg[data-uuid="${CSS.escape(uuid)}"]`);
        }""",
        arg=target_uuid,
        timeout=10000,
    )
    around_state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          mounted: st.messages.length,
          earlier: st.messageRange.visibleStart,
          later: st.messages.length - st.messageRange.visibleEnd,
          targetMounted: st.messages.some(m => m.uuid === "around-target"),
          order: st.messageRange.order,
          offset: st.messageRange.offset,
          total: st.messageRange.total,
          generation: st.messageRange.generation,
          hasServerLater: st._hasServerLater,
        };
        """,
        sid,
    )
    assert around_state["mounted"] == len(around_messages)
    assert around_state["targetMounted"] is True
    assert around_state["earlier"] == 0
    assert around_state["later"] == 0
    assert around_state["order"] == "full"
    assert around_state["offset"] == 200
    assert around_state["total"] == 500
    assert around_state["generation"] == "gen-new"
    assert around_state["hasServerLater"] is True
    assert len([call for call in calls if "around_uuid" in call]) == 2
    assert len([call for call in calls if "tail" in call]) == 1

    returned = _app_eval(page, "return app.returnToLatest(arg);", sid)
    assert returned is True
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return st.messageRange.order === 'normal' && !st._hasServerLater
            && st.messages.some(m => (m.text || '').includes('CANONICAL_LATEST_VISIBLE'));
        }""",
        timeout=10000,
    )
    assert len([call for call in calls if "tail" in call]) == 2
    assert page.locator(".msg-pane:visible .msg").count() <= 60
    _assert_no_browser_errors(page, errors)


def test_viewport_virtualization_keeps_history_and_scroll_anchor(
    page: Page, backend_url, auth_token,
):
    """Only viewport rows mount; canonical history and keyed anchors survive shifts."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    sid = "perf-viewport-virtual-history"
    _bootstrap_session_for_real_load(page, sid, "Viewport virtual history")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const make = i => ({
          role: i % 2 ? "assistant" : "user",
          uuid: `virtual-uuid-${i}`, _k: `virtual-key-${i}`, _noAnim: true,
          text: `VIRTUAL_MESSAGE_${i} ` + "variable height ".repeat(8 + (i % 7) * 6),
          html: i % 2 ? `<p>VIRTUAL_MESSAGE_${i} ${"tail ".repeat(20)}</p>` : "",
        });
        st.messages.splice(0, st.messages.length,
          ...Array.from({ length: 600 }, (_, i) => make(i)));
        Object.assign(st.messageRange, {
          visibleStart: 0,
          visibleEnd: 600,
          offset: 0,
          total: 600,
          preTotal: 0,
          order: "normal",
          generation: "",
        });
        st._hasServerLater = false;
        st.messagesReady = true;
        st.messagesLoading = false;
        st.atBottom = true;
        app.currentId = arg;
        app._activateTabState(arg);
        app.mobileTab = "chat";
        app.$nextTick(() => app.scrollToBottom(true));
        return true;
        """,
        sid,
    )
    page.wait_for_function(
        """() => {
          const pane = document.querySelector('.msg-pane:visible');
          return pane && pane.textContent.includes('VIRTUAL_MESSAGE_599')
            && pane.querySelectorAll('.msg').length > 0
            && pane.querySelectorAll('.msg').length < 80;
        }""",
        timeout=10000,
    )
    initial = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const pane = document.querySelector('.msg-pane:visible');
        return { canonical: st.messages.length, normalized: st.messages.length,
          mounted: pane.querySelectorAll('.msg').length,
          spacers: pane.querySelectorAll('.msg-virtual-spacer').length };
        """,
        sid,
    )
    assert initial["canonical"] == initial["normalized"] == 600
    assert 0 < initial["mounted"] < 80
    assert initial["spacers"] >= 1

    _app_eval(
        page,
        """
        const body = app.$refs.chatBody;
        app.atBottom = false;
        app._ensureTabState(app.currentId).atBottom = false;
        body.scrollTop = Math.floor(body.scrollHeight * 0.45);
        app._syncMessageViewport(app.currentId);
        return true;
        """,
    )
    page.wait_for_timeout(100)
    anchor = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const body = app.$refs.chatBody;
          const rows = Array.from(document.querySelectorAll('.msg-pane:visible .msg'));
          const row = rows.find(el => {
            const r = el.getBoundingClientRect();
            return r.bottom > body.getBoundingClientRect().top;
          });
          return {key: row?.dataset.messageKey || '', top: row?.getBoundingClientRect().top || 0};
        }"""
    )
    assert anchor["key"]
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.$refs.chatBody.scrollTop += 500;
          app._syncMessageViewport(app.currentId);
        }"""
    )
    page.wait_for_timeout(100)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.$refs.chatBody.scrollTop -= 500;
          app._syncMessageViewport(app.currentId);
        }"""
    )
    page.wait_for_timeout(100)
    shifted = page.evaluate(
        """key => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const row = document.querySelector(
            `.msg[data-message-key="${CSS.escape(key)}"]`);
          const st = app._ensureTabState(app.currentId);
          return {canonical: st.messages.length, mounted: document.querySelectorAll(
            '.msg-pane:visible .msg').length, top: row?.getBoundingClientRect().top || 0};
        }""",
        anchor["key"],
    )
    assert shifted["canonical"] == 600
    assert shifted["mounted"] < 80
    assert abs(shifted["top"] - anchor["top"]) < 3

    streaming = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st.streaming = true;
        st.atBottom = false;
        app.atBottom = false;
        const body = app.$refs.chatBody;
        body.scrollTop = 0;
        app._syncMessageViewport(arg);
        return new Promise(resolve => app.$nextTick(() => resolve({
          tailMounted: !!document.querySelector(
            '.msg[data-message-key="virtual-key-599"]'),
          mounted: document.querySelectorAll('.msg-pane:visible .msg').length,
          canonical: st.messages.length,
        })));
        """,
        sid,
    )
    # A reader inspecting old content keeps one viewport window. The live tail
    # remains in the normalized repository and is mounted only when follow resumes;
    # a second streaming-only tail was removed because streaming=false tore it down
    # at completion and collapsed the scroll layout.
    assert streaming["tailMounted"] is False
    assert streaming["mounted"] < 80
    assert streaming["canonical"] == 600

    rebased = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st.streaming = false;
        st.atBottom = true;
        st.messageRange.visibleStart = 540;
        st.messageRange.visibleEnd = 600;
        st._virtualStart = 48;
        st._virtualEnd = 60;
        const snapshot = app._captureMessageVirtualWindow(st);
        st.messageRange.visibleStart = 0;
        app._rebaseMessageVirtualWindow(st, snapshot, true);
        return new Promise(resolve => app.$nextTick(() => resolve({
          start: st._virtualStart,
          end: st._virtualEnd,
          tailMounted: !!document.querySelector(
            '.msg[data-message-key="virtual-key-599"]'),
          headMounted: !!document.querySelector(
            '.msg[data-message-key="virtual-key-048"]'),
        })));
        """,
        sid,
    )
    assert rebased["end"] == 600
    assert rebased["start"] > 500
    assert rebased["tailMounted"] is True
    assert rebased["headMounted"] is False
    _assert_no_browser_errors(page, errors)


def test_load_session_reconnects_active_turn_and_renders_live_assistant(
    page: Page, backend_url, auth_token
):
    """Real loadSession() calls _checkActiveTurn(), which reconnects SSE live."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _install_fake_event_source(page)

    sid = "perf-active-reconnect"
    active_requests: list[str] = []
    ticket_requests: list[dict] = []
    messages = [
        {
            "role": "user",
            "text": "ACTIVE_RECONNECT_USER original prompt still running",
            "ts": 1_700_010_000,
            "uuid": "active-user",
        },
    ]
    _route_windowed_session(page, sid, messages)
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: (
            active_requests.append(route.request.url),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "active": True,
                    "turn_id": "active-turn-1",
                    "started_at": 1_700_010_001,
                    "continuation": False,
                }),
            ),
        )[-1],
    )

    def handle_stream_ticket(route):
        try:
            body = route.request.post_data_json
        except Exception:
            body = {}
        ticket_requests.append(body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "active-reconnect-ticket"}),
        )

    page.route("**/api/chat/stream/start", handle_stream_ticket)
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{ id: arg, name: "Perf active reconnect",
          updated_at: Date.now() / 1000, model: "e2e-model",
          permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg];
        app.tabState = {};
        app.currentId = arg;
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg);
        return true;
        """,
        sid,
    )

    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.streaming === true && app.messagesReady === true
            && app.messages.some(m => (m.text || "").includes("ACTIVE_RECONNECT_USER"));
        }""",
        timeout=10000,
    )
    assert active_requests, "loadSession did not call /active"
    assert ticket_requests and ticket_requests[-1]["prompt"] == ""
    assert ticket_requests[-1]["session_id"] == sid
    assert ticket_requests[-1]["turn_id"] == "active-turn-1"
    assert ticket_requests[-1]["mobile"] is True

    page.evaluate(
        """() => {
          window.__emitSse("text", {
            text: "ACTIVE_RECONNECT_LIVE_VISIBLE",
            turn_id: "active-turn-1", event_seq: 1,
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && app.messagesReady === true
            && last && last.role === "assistant"
            && last.text.includes("ACTIVE_RECONNECT_LIVE_VISIBLE")
            && body.includes("ACTIVE_RECONNECT_LIVE_VISIBLE");
        }""",
        timeout=10000,
    )

    page.evaluate(
        """() => {
          window.__emitSse("done", {
            total_cost_usd: 0.001,
            session_usage: { context_used_pct: 5, context_used: 500, context_limit: 100000 },
            turn_id: "active-turn-1", event_seq: 2,
          });
        }"""
    )
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].streaming === false""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant").last).to_contain_text(
        "ACTIVE_RECONNECT_LIVE_VISIBLE", timeout=5000
    )
    assert _app_eval(page, "return app.messagesReady === true && !app.messagesLoading;") is True
    _assert_no_browser_errors(page, errors)


def test_desktop_done_reconcile_preserves_live_message_dom_identity(
    page: Page, backend_url, auth_token,
):
    """SSE done → quiet canonical reload keeps the rendered reply node mounted."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-done-canonical-identity"
    prompt = "DOM_IDENTITY_USER_PROMPT"
    final_text = "DOM_IDENTITY_FINAL_REPLY " + ("stable canonical text " * 40)
    canonical_messages: list[dict] = []
    requests = _route_windowed_session(page, sid, canonical_messages)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"done-reconcile-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._ensureSessionRegistered = async () => true;
        app._confirmSessionBusy = async () => false;
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.sessions = [{
          id: sid, name: "Done reconciliation", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st._seenUpdated = 1;
        app.currentId = sid;
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app.input = arg.prompt;
        app.atBottom = true;
        return true;
        """,
        {"sid": sid, "prompt": prompt},
    )

    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """text => window.__emitSse("text", {
          text, turn_id: "done-reconcile-turn", event_seq: 1,
        })""",
        final_text,
    )
    page.wait_for_function(
        """text => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          const last = st.messages[st.messages.length - 1];
          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(app.currentId)}"]`);
          return st.streaming && last?.role === "assistant" && last.text === text
            && pane?.querySelector(".msg.assistant");
        }""",
        arg=final_text,
        timeout=10000,
    )
    live = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          const last = st.messages[st.messages.length - 1];
          const nodes = document.querySelectorAll(".msg-pane .msg.assistant");
          window.__doneReconcileLiveNode = nodes[nodes.length - 1];
          window.__doneReconcileLiveKey = last._k;
          return { key: last._k, uuid: last.uuid || "" };
        }"""
    )
    assert ":live:" in live["key"]
    assert live["uuid"] == ""

    canonical_messages.extend([
        {
            "role": "user",
            "text": prompt,
            "uuid": "done-canonical-user",
            "ts": 1_700_020_000,
        },
        {
            "role": "assistant",
            "text": final_text,
            "uuid": "done-canonical-assistant",
            "ts": 1_700_020_001,
        },
    ])
    page.evaluate(
        """() => window.__emitSse("done", {
          total_cost_usd: 0.001,
          session_usage: { context_used_pct: 5, context_used: 500, context_limit: 100000 },
          turn_id: "done-reconcile-turn", event_seq: 2,
        })"""
    )
    page.wait_for_function(
        """sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._ensureTabState(sid).streaming === false;
        }""",
        arg=sid,
        timeout=10000,
    )

    result = page.evaluate(
        """async ({ sid, text }) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          st._pendingExternalUpdate = true;
          app._reconcileOpenSession([{
            ...app.sessions[0], id: sid, updated_at: 2, active: false,
          }]);
          const frames = [];
          for (let i = 0; i < 12; i++) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const pane = document.querySelector(
              `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
            frames.push({
              ready: st.messagesReady,
              loading: st.messagesLoading,
              visible: !!pane && pane.textContent.includes(text.trim()),
              count: pane ? pane.querySelectorAll(".msg").length : 0,
            });
            if (!st._reconcilePromise && i >= 2) break;
          }
          if (st._reconcilePromise) await st._reconcilePromise;
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const last = st.messages[st.messages.length - 1];
          const nodes = document.querySelectorAll(".msg-pane .msg.assistant");
          const canonicalNode = nodes[nodes.length - 1];
          return {
            frames,
            sameNode: canonicalNode === window.__doneReconcileLiveNode,
            oldKey: window.__doneReconcileLiveKey,
            key: last._k,
            uuid: last.uuid || "",
            text: last.text || "",
          };
        }""",
        {"sid": sid, "text": final_text},
    )

    assert requests, "canonical reconciliation did not request session history"
    assert result["sameNode"] is True, result
    assert result["key"] == result["oldKey"]
    assert result["uuid"] == "done-canonical-assistant"
    assert result["text"] == final_text
    assert result["frames"]
    assert all(frame["ready"] and not frame["loading"] for frame in result["frames"]), result
    assert all(frame["visible"] and frame["count"] > 0 for frame in result["frames"]), result
    _assert_no_browser_errors(page, errors)


def test_desktop_cancelled_snapshot_reconcile_never_blanks_or_replaces_live_nodes(
    page: Page, backend_url, auth_token,
):
    """A forced interrupt quietly adopts its durable display snapshot."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-cancelled-snapshot-identity"
    prompt = "CANCELLED_SNAPSHOT_USER_PROMPT"
    first_text = "CANCELLED_SNAPSHOT_FIRST_REPLY " + ("kept before tool " * 20)
    thinking = "CANCELLED_SNAPSHOT_THINKING"
    tool_result = "CANCELLED_SNAPSHOT_TOOL_RESULT\nsecond line"
    final_text = "CANCELLED_SNAPSHOT_FINAL_SEGMENT " + ("still visible " * 20)
    cancelled_at_ms = int(time.time() * 1000)
    snapshot_messages: list[dict] = []
    requests = _route_windowed_session(page, sid, snapshot_messages)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"cancelled-snapshot-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._ensureSessionRegistered = async () => true;
        app._confirmSessionBusy = async () => false;
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.sessions = [{
          id: sid, name: "Cancelled snapshot", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st._seenUpdated = 1;
        app.currentId = sid;
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app.input = arg.prompt;
        app.atBottom = true;
        return true;
        """,
        {"sid": sid, "prompt": prompt},
    )

    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """payload => {
          window.__emitSse("text", {
            text: payload.first, turn_id: "cancelled-snapshot-turn", event_seq: 1,
          });
          window.__emitSse("thinking", {
            text: payload.thinking, turn_id: "cancelled-snapshot-turn", event_seq: 2,
          });
          window.__emitSse("tool_use", {
            id: "toolu_cancelled_snapshot", name: "Read",
            summary: "cancelled snapshot fixture",
            input: {file_path: "fixture.txt"},
            turn_id: "cancelled-snapshot-turn", event_seq: 3,
          });
          window.__emitSse("tool_result", {
            id: "toolu_cancelled_snapshot", tool_name: "Read",
            preview: payload.toolResult, text: payload.toolResult,
            truncated: false, text_truncated: false, is_error: false,
            turn_id: "cancelled-snapshot-turn", event_seq: 4,
          });
          window.__emitSse("text", {
            text: payload.final, turn_id: "cancelled-snapshot-turn", event_seq: 5,
          });
        }""",
        {
            "first": first_text,
            "thinking": thinking,
            "toolResult": tool_result,
            "final": final_text,
        },
    )
    page.wait_for_function(
        """expected => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          return app.streaming && app.messages.length === 6
            && pane?.textContent.includes(expected.first)
            && pane?.textContent.includes(expected.final);
        }""",
        arg={"first": first_text, "final": final_text},
        timeout=10000,
    )
    before = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          const nodes = Array.from(pane.querySelectorAll(".msg"));
          window.__cancelledSnapshotNodes = nodes;
          window.__cancelledSnapshotKeys = app.messages.map(m => m._k);
          window.__cancelledSnapshotMinCount = nodes.length;
          window.__cancelledSnapshotObserver = new MutationObserver(() => {
            const visible = Array.from(document.querySelectorAll(".msg-pane"))
              .find(el => getComputedStyle(el).display !== "none");
            const count = visible ? visible.querySelectorAll(".msg").length : 0;
            window.__cancelledSnapshotMinCount = Math.min(
              window.__cancelledSnapshotMinCount, count);
          });
          window.__cancelledSnapshotObserver.observe(
            document.querySelector("#app"), {childList: true, subtree: true});
          return {count: nodes.length, keys: window.__cancelledSnapshotKeys};
        }"""
    )
    assert before["count"] == 6
    assert all(":live:" in key for key in before["keys"])

    snapshot_messages.extend([
        {
            "role": "user", "text": prompt,
            "_key": "cancelled:cancelled-snapshot-turn:0", "_interrupted": True,
        },
        {
            "role": "assistant", "text": first_text, "model": "e2e-model",
            "_key": "cancelled:cancelled-snapshot-turn:1", "_interrupted": True,
        },
        {
            "role": "thinking", "text": thinking,
            "_key": "cancelled:cancelled-snapshot-turn:2", "_interrupted": True,
        },
        {
            "role": "tool_use", "id": "toolu_cancelled_snapshot", "name": "Read",
            "summary": "cancelled snapshot fixture", "input": {"file_path": "fixture.txt"},
            "_key": "cancelled:cancelled-snapshot-turn:3", "_interrupted": True,
        },
        {
            "role": "tool_result", "id": "toolu_cancelled_snapshot", "tool_name": "Read",
            "preview": tool_result, "text": tool_result, "is_error": False,
            "_key": "cancelled:cancelled-snapshot-turn:4", "_interrupted": True,
        },
        {
            "role": "assistant", "text": final_text, "model": "e2e-model",
            "ts": cancelled_at_ms, "elapsed": 5.0,
            "turn_status": "cancelled",
            "_key": "cancelled:cancelled-snapshot-turn:5", "_interrupted": True,
        },
    ])
    page.evaluate(
        """() => window.__emitSse("cancelled", {
          snapshot_ready: true,
          turn_id: "cancelled-snapshot-turn",
          event_seq: 6,
        })"""
    )
    page.wait_for_function(
        """expected => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(expected.sid);
          return !app.streaming && !st.streaming && st._loaded
            && st.messages.length === expected.count
            && st.messages.every(message => message._interrupted === true);
        }""",
        arg={"sid": sid, "count": len(snapshot_messages)},
        timeout=10000,
    )
    result = page.evaluate(
        """async ({ first, final }) => {
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          window.__cancelledSnapshotObserver.disconnect();
          const app = document.querySelector("#app")._x_dataStack[0];
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          const nodes = Array.from(pane.querySelectorAll(".msg"));
          const tail = app.messages[app.messages.length - 1];
          const footer = nodes[nodes.length - 1]?.querySelector('.turn-footer');
          return {
            minCount: window.__cancelledSnapshotMinCount,
            count: nodes.length,
            sameNodes: nodes.every(
              (node, index) => node === window.__cancelledSnapshotNodes[index]),
            keys: app.messages.map(message => message._k),
            ready: app.messagesReady,
            loading: app.messagesLoading,
            firstVisible: pane.textContent.includes(first),
            finalVisible: pane.textContent.includes(final),
            tailStatus: tail.turn_status,
            tailModel: tail.model,
            tailTs: tail.ts,
            tailElapsed: tail.elapsed,
            footerVisible: !!footer?.getClientRects().length,
            footerText: footer?.textContent.replace(/\\s+/g, ' ').trim() || '',
          };
        }""",
        {"first": first_text, "final": final_text},
    )

    assert requests, "cancelled reconciliation did not request the display snapshot"
    assert result["minCount"] == before["count"], result
    assert result["count"] == before["count"]
    assert result["sameNodes"] is True, result
    assert result["keys"] == before["keys"]
    assert result["ready"] is True and result["loading"] is False
    assert result["firstVisible"] is True and result["finalVisible"] is True
    assert result["tailStatus"] == "cancelled"
    assert result["tailModel"] == "e2e-model"
    # The live terminal timestamp is deliberately preserved during a quiet
    # same-DOM reconcile; a cold reload reads the durable value asserted by
    # the backend snapshot test. Both must remain populated here.
    assert result["tailTs"] > 0
    assert result["tailElapsed"] >= 1
    assert result["footerVisible"] is True
    assert ("E2E model" in result["footerText"]
            or "e2e-model" in result["footerText"])
    assert ("已中断" in result["footerText"]
            or "Interrupted" in result["footerText"])
    _assert_no_browser_errors(page, errors)


def test_fast_completed_queued_turn_reconciles_footer_without_refresh(
    page: Page, backend_url, auth_token,
):
    """A queued turn that finishes before attach falls back to quiet history."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    sid = "fast-completed-queued-footer"
    completed_at_ms = int(time.time() * 1000)
    history_requests: list[str] = []

    page.route(
        f"**/api/chat/sessions/{sid}/queue",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": [], "paused": False, "revision": 2}),
        ),
    )
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "active": False,
                "activity_source": "queued",
                "background_tasks_pending": 0,
            }),
        ),
    )

    def canonical_history(route):
        history_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Fast queued footer",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "updated_at": completed_at_ms / 1000,
                "messages": [
                    {
                        "role": "user",
                        "text": "FAST_QUEUED_PROMPT",
                        "uuid": "fast-queued-user",
                    },
                    {
                        "role": "assistant",
                        "text": "FAST_QUEUED_REPLY",
                        "uuid": "fast-queued-assistant",
                        "ts": completed_at_ms,
                        "elapsed": 4.2,
                        "model": "e2e-model",
                        "turn_status": "completed",
                    },
                ],
                "offset": 0,
                "total": 2,
                "has_more": False,
                "history_generation": "fast-queued-footer-e2e",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", canonical_history)
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        if (app._sessionsSyncTimer) clearInterval(app._sessionsSyncTimer);
        app._sessionsSyncTimer = null;
        app.refreshSessions = async () => {};
        app._syncSessionListQuiet = async () => false;
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: 'e2e-model', label: 'E2E model', group: 'e2e',
          supports_thinking: true,
        }];
        app.sessions = [{
          id: sid, name: 'Fast queued footer', updated_at: 1,
          model: 'e2e-model', permission: 'bypassPermissions', thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st.messages.push(...app._historyEnvelopes(sid, [{
          role: 'assistant', text: 'KEEP_VISIBLE_DURING_RECONCILE',
          html: '<p>KEEP_VISIBLE_DURING_RECONCILE</p>',
          uuid: 'prior-visible-assistant', ts: Date.now() - 10000,
          elapsed: 2, model: 'e2e-model', turn_status: 'completed',
        }]));
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        st._loaded = true;
        st._seenUpdated = 1;
        st._draining = true;
        st.messagesReady = true;
        app.currentId = sid;
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = 'chat';
        return true;
        """,
        {"sid": sid},
    )
    page.wait_for_function(
        """([sid]) => {
          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
          return pane?.textContent.includes('KEEP_VISIBLE_DURING_RECONCILE');
        }""",
        arg=[sid],
    )
    _app_eval(
        page,
        """
        const sid = arg;
        const pane = document.querySelector(
          `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
        window.__fastQueueMinMessages = pane.querySelectorAll('.msg').length;
        window.__fastQueueObserver = new MutationObserver(() => {
          const current = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
          const count = current ? current.querySelectorAll('.msg').length : 0;
          window.__fastQueueMinMessages = Math.min(
            window.__fastQueueMinMessages, count);
        });
        window.__fastQueueObserver.observe(
          document.querySelector('#app'), {childList: true, subtree: true});
        window.__fastQueueAttach = app._attachToServerTurn(
          sid, 2, 'previous-completed-turn');
        return true;
        """,
        sid,
    )
    page.evaluate("() => window.__fastQueueAttach")
    page.wait_for_function(
        """([sid, completedAt]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[sid];
          const tail = st?.messages?.[st.messages.length - 1];
          return tail?.uuid === 'fast-queued-assistant'
            && tail.ts === completedAt && tail.turn_status === 'completed'
            && st._draining === false;
        }""",
        arg=[sid, completed_at_ms],
        timeout=5000,
    )
    result = _app_eval(
        page,
        """
        window.__fastQueueObserver.disconnect();
        const st = app.tabState[arg];
        return {
          minMessages: window.__fastQueueMinMessages,
          ready: st.messagesReady,
          loading: st.messagesLoading,
          streaming: st.streaming,
        };
        """,
        sid,
    )
    assert history_requests, "completed queued turn did not pull canonical history"
    assert result == {
        "minMessages": 1,
        "ready": True,
        "loading": False,
        "streaming": False,
    }
    footer = page.locator(
        f'.msg-pane[data-tid="{sid}"] .turn-footer'
    ).last
    expect(footer).to_be_visible()
    expected_time = _app_eval(
        page, "return app.fmtTurnTime(arg);", completed_at_ms
    )
    expected_status = _app_eval(
        page, "return app.lang === 'zh' ? '已完成' : 'Completed';"
    )
    expect(footer.locator(
        ".turn-status > span:not(.turn-running-dots)"
        ":not(.turn-background-running)"
    )).to_have_text(expected_status)
    expect(footer.locator(".msg-ts")).to_have_text(expected_time)
    expect(footer.locator(".msg-elapsed")).to_have_text("· 4s")
    expected_model = _app_eval(
        page, "return '· ' + app.modelLabel(arg);", "e2e-model"
    )
    expect(footer.locator(".turn-model")).to_have_text(expected_model)
    _assert_no_browser_errors(page, errors)


def test_tool_result_tail_done_metadata_renders_footer_before_canonical_reload(
    page: Page, backend_url, auth_token,
):
    """Early done metadata completes the visual tail without touching identity."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-tool-tail-done-metadata"
    assistant_uuid = "tool-tail-assistant-boundary"
    completed_at_ms = int(time.time() * 1000)
    duration_ms = 125_000
    active_requests: list[str] = []
    history_requests: list[str] = []

    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"tool-tail-done-ticket"}',
        ),
    )
    page.route(
        "**/api/chat/providers",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "models": [{
                    "model": "e2e-model",
                    "label": "E2E model",
                    "group": "e2e",
                    "supports_thinking": True,
                }],
                "default_model": "e2e-model",
                "default_permission": "bypassPermissions",
            }),
        ),
    )

    def active_stays_true(route):
        active_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"active":true}',
        )

    def unexpected_history(route):
        history_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Tool tail done",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": [],
                "offset": 0,
                "total": 0,
                "has_more": False,
                "history_generation": "blocked-canonical-e2e",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}/active", active_stays_true)
    page.route(f"**/api/chat/sessions/{sid}?*", unexpected_history)
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._ensureSessionRegistered = async () => true;
        app._confirmSessionBusy = async () => false;
        app.ackCurrentActivity = () => {};
        // The app's 10s cross-device poll would correctly interpret this
        // test's intentionally permanent /active=true response as a stalled
        // stream and reconnect it. Disable that unrelated timer so the test
        // observes only the done-time canonical barrier under test.
        if (app._sessionsSyncTimer) clearInterval(app._sessionsSyncTimer);
        app._sessionsSyncTimer = null;
        app._syncSessionListQuiet = async () => false;
        app.appReady = true;
        app.availableModels = [{
          model: 'e2e-model', label: 'E2E model', group: 'e2e',
          supports_thinking: true,
        }];
        app.model = 'e2e-model';
        app.defaultModel = 'e2e-model';
        app.sessions = [{
          id: sid,
          name: 'Tool tail done',
          updated_at: 1,
          model: 'e2e-model',
          permission: 'bypassPermissions',
          thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st._seenUpdated = 1;
        app.currentId = sid;
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = 'chat';
        app.input = 'Finish on a tool result';
        app.atBottom = true;
        return true;
        """,
        {"sid": sid},
    )

    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """() => {
          window.__emitSse('text', {
            text: 'TOOL_TAIL_ASSISTANT_TEXT',
            turn_id: 'tool-tail-turn',
            event_seq: 1,
          });
          window.__emitSse('tool_use', {
            id: 'toolu_tool_tail',
            name: 'Bash',
            summary: 'produce final status',
            input: {command: 'printf done'},
            turn_id: 'tool-tail-turn',
            event_seq: 2,
          });
          window.__emitSse('tool_result', {
            id: 'toolu_tool_tail',
            tool_name: 'Bash',
            preview: 'done',
            text: 'done\\n',
            truncated: false,
            is_error: false,
            bash: {stdout: 'done\\n', stderr: '', exit_code: 0},
            turn_id: 'tool-tail-turn',
            event_seq: 3,
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const tail = app.messages[app.messages.length - 1];
          return app.streaming && tail?.role === 'tool_result';
        }"""
    )
    before_done = _app_eval(
        page,
        """
        const tail = app.messages[app.messages.length - 1];
        const assistant = [...app.messages].reverse()
          .find(message => message.role === 'assistant');
        return {
          tailKey: tail._k,
          tailUuid: tail.uuid,
          tailForkUuid: tail.forkUuid,
          assistantUuid: assistant?.uuid || '',
        };
        """,
    )
    tail_key = before_done.pop("tailKey")
    assert ":live:" in tail_key
    assert before_done == {
        "tailUuid": "",
        "tailForkUuid": "",
        "assistantUuid": "",
    }

    with page.expect_request(
        lambda request: request.url.endswith(
            f"/api/chat/sessions/{sid}/active"
        ),
        timeout=5000,
    ):
        page.evaluate(
            """arg => window.__emitSse('done', {
              assistant_uuid: arg.assistantUuid,
              completed_at_ms: arg.completedAtMs,
              duration_ms: arg.durationMs,
              total_cost_usd: 0.001,
              model: 'e2e-model',
              memory_recall: {
                id: 'tool-tail-memory-trace', count: 1,
                latency_ms: 8, status: 'ok', items: [{
                  id: 'tool-tail-memory-item', kind: 'preference',
                  content: 'A deliberately long memory detail '.repeat(36),
                }],
              },
              session_usage: {
                context_used_pct: 5,
                context_used: 500,
                context_limit: 100000,
              },
              turn_id: 'tool-tail-turn',
              event_seq: 4,
            })""",
            {
                "assistantUuid": assistant_uuid,
                "completedAtMs": completed_at_ms,
                "durationMs": duration_ms,
            },
        )

    page.wait_for_function(
        """arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const tail = app.messages[app.messages.length - 1];
          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(arg.sid)}"]`);
          const tailNode = pane?.querySelector(
            `.msg[data-message-key="${CSS.escape(arg.tailKey)}"]`);
          const footer = tailNode?.querySelector('.turn-footer');
          return !app.streaming
            && tail?.role === 'tool_result'
            && tail.ts === arg.completedAtMs
            && tail.elapsed === arg.durationMs / 1000
            && tail.model === 'e2e-model'
            && tail.turn_status === 'completed'
            && tail.memoryRecall?.id === 'tool-tail-memory-trace'
            && tail.forkUuid === arg.assistantUuid
            && tailNode?.classList.contains('tool_result')
            && footer?.getClientRects().length
            && footer.querySelector('.msg-ts')?.textContent
              === app.fmtTurnTime(arg.completedAtMs)
            && footer.querySelector('.msg-elapsed')?.textContent === '· 2m05s';
        }""",
        arg={
            "sid": sid,
            "tailKey": tail_key,
            "assistantUuid": assistant_uuid,
            "completedAtMs": completed_at_ms,
            "durationMs": duration_ms,
        },
        timeout=5000,
    )
    footer = page.locator(
        f'.msg-pane[data-tid="{sid}"] '
        f'.msg[data-message-key="{tail_key}"] .turn-footer'
    )
    expect(footer).to_be_visible()
    expected_time = _app_eval(
        page, "return app.fmtTurnTime(arg);", completed_at_ms
    )
    expected_status = _app_eval(
        page, "return app.lang === 'zh' ? '已完成' : 'Completed';"
    )
    expect(footer.locator(".msg-ts")).to_have_text(expected_time)
    expect(footer.locator(".msg-elapsed")).to_have_text("· 2m05s")
    expect(footer.locator(".turn-model")).to_have_text("· E2E model")
    expect(footer.locator(".turn-status > span").first).to_have_text(
        expected_status
    )
    recall_trigger = footer.locator(".memory-recall-trace")
    expect(recall_trigger).to_be_visible()
    expect(footer.locator(".turn-fork-btn")).to_be_visible()

    recall_trigger.click()
    recall = page.locator(".memory-recall-global")
    expect(recall).to_be_visible()
    recall_geometry = recall.evaluate(
        """node => {
          const rect = node.getBoundingClientRect();
          return {
            position: getComputedStyle(node).position,
            zIndex: Number(getComputedStyle(node).zIndex),
            insideChatScroller: !!node.closest('.chat-body'),
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
          };
        }"""
    )
    assert recall_geometry["position"] == "fixed"
    assert recall_geometry["zIndex"] >= 800
    assert recall_geometry["insideChatScroller"] is False
    assert recall_geometry["left"] >= 0
    assert recall_geometry["top"] >= 0
    assert recall_geometry["right"] <= recall_geometry["viewportWidth"]
    assert recall_geometry["bottom"] <= recall_geometry["viewportHeight"]
    page.keyboard.press("Escape")
    expect(recall).to_be_hidden()

    state = _app_eval(
        page,
        """
        const tail = app.messages[app.messages.length - 1];
        const assistant = [...app.messages].reverse()
          .find(message => message.role === 'assistant');
        return {
          role: tail.role,
          tailUuid: tail.uuid,
          tailForkUuid: tail.forkUuid,
          assistantUuid: assistant?.uuid || '',
          forkBoundary: app.turnForkMessageId(
            app.messages, app.messages.length - 1),
          ts: tail.ts,
          elapsed: tail.elapsed,
          model: tail.model,
          turnStatus: tail.turn_status,
          memoryRecallId: tail.memoryRecall?.id || '',
          liveKey: tail._k,
          streaming: app.streaming,
        };
        """,
    )
    live_key = state.pop("liveKey")
    assert state == {
        "role": "tool_result",
        "tailUuid": "",
        "tailForkUuid": assistant_uuid,
        # done metadata is an affordance, not canonical message identity.
        "assistantUuid": "",
        "forkBoundary": assistant_uuid,
        "ts": completed_at_ms,
        "elapsed": duration_ms / 1000,
        "model": "e2e-model",
        "turnStatus": "completed",
        "memoryRecallId": "tool-tail-memory-trace",
        "streaming": False,
    }
    assert ":live:" in live_key
    assert active_requests, "canonical barrier never checked /active"
    assert history_requests == [], (
        "canonical history loaded even though /active still reported true"
    )
    _assert_no_browser_errors(page, errors)


def test_stable_message_identity_needs_no_repair_telemetry(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = "stable-render-identity";
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.appReady = true;
          app.sessions = [{id: sid, name: "Stable", model: "e2e-model"}];
          app.openTabIds = [sid];
          app.tabState = {};
          const st = app._ensureTabState(sid);
          st._loaded = true;
          const payload = [
            {role: "user", text: "question", block_id: "record-1:0:user"},
            {role: "assistant", text: "answer", block_id: "record-2:0:assistant"},
            {role: "tool_result", text: "tool", block_id: "record-2:1:tool_result"},
          ];
          const first = app._historyEnvelopes(sid, payload);
          const second = app._historyEnvelopes(sid, payload.map(message => ({...message})));
          st.messages.push(...first);
          st.messageRange.visibleEnd = st.messages.length;
          st.messageRange.total = st.messages.length;
          app.currentId = sid;
          app._activateTabState(sid);
          app.messagesReady = true;
          app.messagesLoading = false;
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const pane = document.querySelector(`.msg-pane[data-tid="${sid}"]`);
          const domKeys = pane ? Array.from(pane.querySelectorAll(".msg"))
            .map(el => el.dataset.messageKey) : [];
          const live = Array.from({length: 25}, (_, i) => app._appendLiveMessage(st, {
            role: "thinking", text: `live ${i}`,
          }));
          const followedEnd = st.messageRange.visibleEnd;
          st.messageRange.visibleEnd = 3;
          app._appendLiveMessage(st, {role: "thinking", text: "hidden live"});
          const hiddenEnd = st.messageRange.visibleEnd;
          return {
            keys: first.map(message => message._k),
            sameObjects: first.every((message, i) => Alpine.raw(second[i]) === Alpine.raw(message)),
            domKeys,
            liveKeys: live.map(message => message._k),
            followedEnd,
            hiddenEnd,
            repositoryLength: st.messages.length,
            normalizedCount: app._sessionWindows.get(sid)?.size || 0,
            paneCount: document.querySelectorAll(".msg-pane").length,
            telemetryBuffer: typeof window.__museTelemetry__,
            telemetryReporter: typeof window.__museReportTelemetry__,
          };
        }"""
    )

    assert result["keys"] == [
        "stable-render-identity:block:record-1:0:user",
        "stable-render-identity:block:record-2:0:assistant",
        "stable-render-identity:block:record-2:1:tool_result",
    ]
    assert result["sameObjects"] is True
    assert result["domKeys"] == result["keys"]
    assert len(result["liveKeys"]) == len(set(result["liveKeys"])) == 25
    assert all(":live:" in key for key in result["liveKeys"])
    assert result["followedEnd"] == 28
    assert result["repositoryLength"] == 29
    assert result["hiddenEnd"] == 3
    assert result["normalizedCount"] == 3
    assert result["paneCount"] == 1
    assert result["telemetryBuffer"] == "undefined"
    assert result["telemetryReporter"] == "undefined"
    _assert_no_browser_errors(page, errors)


def test_canonical_reload_stays_quiet_when_background_tab_becomes_current(
    page: Page, backend_url, auth_token,
):
    """A completion reload started off-screen must not blank a tab selected mid-fetch."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    sid = "perf-canonical-race-target"
    other_sid = "perf-canonical-race-other"
    final_text = "CANONICAL_RACE_REPLY remains visible"
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"active":false}',
        ),
    )

    def delayed_history(route):
        # Keep loadSession in flight long enough for the user to activate this
        # formerly-background tab. The browser renderer continues running the
        # scheduled switch while this intercepted response is delayed.
        time.sleep(0.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Canonical race",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": [{
                    "role": "assistant",
                    "text": final_text,
                    "uuid": "canonical-race-assistant",
                    "ts": 1_700_030_000,
                }],
                "offset": 0,
                "total": 1,
                "has_more": False,
                "history_generation": "gen-canonical-race",
                "updated_at": 2,
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", delayed_history)
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const target = arg.sid;
        const other = arg.otherSid;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._syncQueueFromServer = async () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.sessions = [
          { id: other, name: "Other", updated_at: 1, model: "e2e-model" },
          { id: target, name: "Target", updated_at: 1, model: "e2e-model" },
        ];
        app.openTabIds = [other, target];
        app.tabState = {};
        const otherState = app._ensureTabState(other);
        otherState._loaded = true;
        otherState.messages.push({
          role: "assistant", text: "OTHER_VISIBLE",
          html: "<p>OTHER_VISIBLE</p>", _k: `${other}:existing`, _noAnim: true,
        });
        otherState.messageRange.visibleEnd = otherState.messages.length;
        otherState.messageRange.total = otherState.messages.length;
        const targetState = app._ensureTabState(target);
        targetState._loaded = true;
        targetState.messages.push({
          role: "assistant", text: arg.finalText,
          html: `<p>${arg.finalText}</p>`, _k: `${target}:live:1`,
        });
        targetState.messageRange.visibleEnd = targetState.messages.length;
        targetState.messageRange.total = targetState.messages.length;
        targetState.messagesReady = true;
        targetState.messagesLoading = false;
        app.currentId = other;
        app.mobileTab = "chat";
        app._activateTabState(other);
        return new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
        """,
        {"sid": sid, "otherSid": other_sid, "finalText": final_text},
    )
    page.wait_for_function(
        """key => document.querySelector(
          `.msg[data-message-key="${CSS.escape(key)}"]`) !== null""",
        arg=f"{sid}:live:1",
        timeout=10000,
    )

    result = page.evaluate(
        """async ({ sid, finalText }) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          const liveKey = `${sid}:live:1`;
          const liveNode = document.querySelector(
            `.msg[data-message-key="${CSS.escape(liveKey)}"]`);
          const frames = [];
          app._scheduleCanonicalStreamReload(sid, st);
          setTimeout(async () => {
            app.currentId = sid;
            await app.switchSession();
          }, 320);
          const deadline = performance.now() + 3000;
          while ((!st.messages.some(m => m.uuid === "canonical-race-assistant")
                  || app.currentId !== sid) && performance.now() < deadline) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            if (app.currentId !== sid) continue;
            const pane = Array.from(document.querySelectorAll(".msg-pane"))
              .find(el => getComputedStyle(el).display !== "none");
            const visibleMessages = pane ? Array.from(pane.querySelectorAll(".msg"))
              .filter(el => getComputedStyle(el).display !== "none") : [];
            frames.push({
              ready: app.messagesReady,
              loading: app.messagesLoading,
              targetVisible: !!pane && pane.textContent.includes(finalText),
              visibleCount: visibleMessages.length,
            });
          }
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const canonical = st.messages[st.messages.length - 1];
          const canonicalNode = document.querySelector(
            `.msg[data-message-key="${CSS.escape(canonical._k)}"]`);
          const visiblePane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          return {
            frames,
            pending: st._canonicalResyncPending,
            sameNode: canonicalNode === liveNode,
            key: canonical._k,
            uuid: canonical.uuid || "",
            finalVisible: !!visiblePane && visiblePane.textContent.includes(finalText),
          };
        }""",
        {"sid": sid, "finalText": final_text},
    )

    assert result["pending"] is False, result
    assert result["sameNode"] is True, result
    assert result["key"] == f"{sid}:live:1"
    assert result["uuid"] == "canonical-race-assistant"
    assert result["frames"], result
    assert all(frame["ready"] and not frame["loading"] for frame in result["frames"]), result
    assert all(frame["visibleCount"] > 0 for frame in result["frames"]), result
    assert result["finalVisible"] is True, result
    _assert_no_browser_errors(page, errors)


def test_mobile_turn_footer_keeps_complete_metadata_inside_chat(
    page: Page, backend_url, auth_token,
):
    """Status/time/duration/model/fork all fit at the 320px floor."""
    page.set_viewport_size({"width": 320, "height": 700})
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length, ...app._historyEnvelopes(sid, [{
            role: 'assistant', text: 'MOBILE_FOOTER_COMPLETE',
            html: '<p>MOBILE_FOOTER_COMPLETE</p>',
            uuid: 'mobile-footer-assistant',
            forkUuid: 'mobile-footer-assistant',
            ts: Date.now(), elapsed: 3725,
            model: 'codex:a-very-long-model-name-for-footer',
            turn_status: 'completed',
          }]));
          st.messageRange.visibleEnd = st.messages.length;
          st.messageRange.total = st.messages.length;
          st._loaded = true;
          st.messagesReady = true;
          st.streaming = false;
          app._activateTabState(sid);
          app.messagesReady = true;
          app.mobileTab = 'chat';
        }"""
    )
    footer = page.locator(".msg-pane:visible .turn-footer")
    expect(footer).to_be_visible()
    expect(footer.locator(".turn-status")).to_be_visible()
    expect(footer.locator(".msg-ts")).to_be_visible()
    expect(footer.locator(".msg-elapsed")).to_be_visible()
    expect(footer.locator(".turn-model")).to_be_visible()
    expect(footer.locator(".turn-fork-btn")).to_be_visible()

    geometry = footer.evaluate(
        """node => {
          const body = node.closest('.chat-body').getBoundingClientRect();
          const rect = node.getBoundingClientRect();
          const visible = Array.from(node.children)
            .filter(child => child.getClientRects().length)
            .map(child => {
              const r = child.getBoundingClientRect();
              return {left: r.left, right: r.right};
            });
          return {
            bodyLeft: body.left, bodyRight: body.right,
            left: rect.left, right: rect.right,
            clientWidth: node.clientWidth, scrollWidth: node.scrollWidth,
            visible,
          };
        }"""
    )
    assert geometry["left"] >= geometry["bodyLeft"] - 1, geometry
    assert geometry["right"] <= geometry["bodyRight"] + 1, geometry
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
    assert all(
        child["left"] >= geometry["bodyLeft"] - 1
        and child["right"] <= geometry["bodyRight"] + 1
        for child in geometry["visible"]
    ), geometry


def test_failed_queue_edit_never_duplicates_and_stopping_turn_rejects_send(
    page: Page, backend_url, auth_token,
):
    """Exercise both queue failure guards through Alpine's live state."""
    _login(page, backend_url, auth_token)
    sid = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId"
    )
    mutations: list[str] = []

    def fail_queue_mutation(route):
        mutations.append(route.request.method)
        route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"temporary queue failure"}',
        )

    page.route(
        f"**/api/chat/sessions/{sid}/queue/q-edit-failure",
        fail_queue_mutation,
    )
    result = page.evaluate(
        """async sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(sid);
          app._syncQueueFromServer = async () => {};
          st.pendingQueue = [{
            id: 'q-edit-failure', text: 'SERVER ORIGINAL',
            displayText: 'SERVER ORIGINAL', pendingQuotes: [],
            images: [], docs: [],
          }];
          st.draft.input = '';
          app._activateComposerState(sid);
          await app.editPendingQueueItem(sid, 0);
          const afterEdit = {
            queueLength: st.pendingQueue.length,
            queueText: st.pendingQueue[0]?.text || '',
            draft: st.draft.input,
            busy: Object.keys(st._queueMutating || {}),
          };

          st._stopping = true;
          st.streaming = true;
          st.draft.input = 'SEND DURING STOP';
          app._activateComposerState(sid);
          const sendResult = await app.send();
          return {
            afterEdit,
            sendResult,
            stoppingDraft: st.draft.input,
            pendingAfterStop: st.pendingQueue.length,
          };
        }""",
        sid,
    )

    assert mutations == ["DELETE"]
    assert result["afterEdit"] == {
        "queueLength": 1,
        "queueText": "SERVER ORIGINAL",
        "draft": "",
        "busy": [],
    }
    assert result["sendResult"] is False
    assert result["stoppingDraft"] == "SEND DURING STOP"
    assert result["pendingAfterStop"] == 1


def test_background_task_gap_leaves_composer_usable_without_empty_reconnect(
    page: Page, backend_url, auth_token,
):
    """A detached task reader is busy but has no SSE broadcast to attach to."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _install_fake_event_source(page)

    sid = "perf-background-gap"
    origin_started_at = int(time.time()) - 90
    tickets: list[dict] = []
    messages = [
        {
            "role": "user",
            "text": "BACKGROUND_GAP_USER",
            "ts": 1_700_020_000,
            "uuid": "background-user",
        },
        {
            "role": "assistant",
            "text": "BACKGROUND_GAP_ASSISTANT",
            "ts": 1_700_020_001,
            "uuid": "background-assistant",
        },
        {
            "role": "tool_use",
            "name": "Task",
            "text": "BACKGROUND_GAP_TASK",
            "id": "task-gap-1",
            "uuid": "background-task",
            "task_status": {"state": "running", "task_id": "task-gap-1"},
        },
    ]
    requests = _route_windowed_session(page, sid, messages)
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "active": True,
                "attachable": False,
                "background": True,
                "continuation": False,
                "turn_id": "background-origin-turn",
                "started_at": origin_started_at,
                "background_tasks_pending": 1,
            }),
        ),
    )

    def capture_ticket(route):
        tickets.append(route.request.post_data_json)
        route.fulfill(
            status=500,
            content_type="application/json",
            body="{}",
        )

    page.route("**/api/chat/stream/start", capture_ticket)
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{ id: arg, name: "Background gap",
          updated_at: Date.now() / 1000, model: "e2e-model",
          permission: "bypassPermissions", thinking: true,
          active: true, turn_active: false, background_active: true,
          message_count: 3, turn_count: 1 }];
        app.openTabIds = [arg];
        app.tabState = {};
        app.currentId = arg;
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg);
        return true;
        """,
        sid,
    )

    _app_eval(page, "return app.loadSession(arg);", sid)
    # A pending background task remains an internal queue-routing state so a
    # new prompt can be handed to a successor runtime safely.  It must not,
    # however, disable the composer or open an empty foreground SSE.
    page.wait_for_function(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[sid];
          const input = document.querySelector(".chat-input-textarea");
          return st && st.backgroundActive === true
            && st.streaming === false && input && !input.disabled
            && st.streamElapsed >= 89;
        }""",
        arg=sid,
        timeout=10000,
    )
    # The old blocking strip is gone, and the turn footer no longer spins for
    # a task that is not this turn's work.
    expect(page.locator(".background-task-strip")).to_have_count(0)
    expect(page.locator(".msg-pane:visible .thinking-dots:visible")).to_have_count(0)
    # The tab dot still surfaces that something is running in the background.
    expect(page.locator(".chat-tab.active .chat-tab-stream-dot.is-background")).to_be_visible(
        timeout=5000,
    )

    # The foreground turn may finish while its detached task keeps /active
    # true.  Canonicalize that completed foreground turn once, but never hide
    # or remount the already-visible assistant bubble while doing so.
    reconciliation = page.evaluate(
        """async sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          const node = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant`);
          const key = st.messages.find(m => m.role === "assistant")?._k || "";
          app._reconcileCompletedTurn(
            sid, st, "BACKGROUND_GAP_ASSISTANT",
          );
          const frames = [];
          for (let i = 0; i < 24; i += 1) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const pane = document.querySelector(
              `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
            frames.push({
              ready: app.messagesReady,
              loading: app.messagesLoading,
              visible: !!pane && pane.textContent.includes("BACKGROUND_GAP_ASSISTANT"),
              count: pane ? pane.querySelectorAll(".msg").length : 0,
            });
          }
          await new Promise(resolve => setTimeout(resolve, 120));
          const current = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant`);
          return {frames, sameNode: current === node, key,
            currentKey: st.messages.find(m => m.role === "assistant")?._k || ""};
        }""",
        sid,
    )
    assert len(requests) >= 3, requests
    assert reconciliation["sameNode"] is True, reconciliation
    assert reconciliation["currentKey"] == reconciliation["key"]
    assert all(
        frame["ready"] and not frame["loading"]
        and frame["visible"] and frame["count"] > 0
        for frame in reconciliation["frames"]
    ), reconciliation

    # A task-progress/JSONL-mtime-only session-list update is metadata, not new
    # chat content.  Repeated waiting ticks must not issue another ?tail=300 or
    # replace any message node.
    request_baseline = len(requests)
    waiting = page.evaluate(
        """async sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          st._seenUpdated = 1;
          app.sessions[0] = {...app.sessions[0], updated_at: 1,
            active: true, turn_active: false, background_active: true,
            message_count: 3, turn_count: 1};
          const node = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant`);
          const frames = [];
          for (let revision = 2; revision <= 8; revision += 1) {
            app._applySessionList([{...app.sessions[0], updated_at: revision}]);
            await new Promise(resolve => requestAnimationFrame(resolve));
            const pane = document.querySelector(
              `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
            frames.push({
              ready: app.messagesReady,
              loading: app.messagesLoading,
              visible: !!pane && pane.textContent.includes("BACKGROUND_GAP_ASSISTANT"),
              count: pane ? pane.querySelectorAll(".msg").length : 0,
            });
          }
          const current = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"] .msg.assistant`);
          app._stopBgContPoller(sid);
          return {frames, sameNode: current === node};
        }""",
        sid,
    )
    assert len(requests) == request_baseline, requests
    assert waiting["sameNode"] is True, waiting
    assert all(
        frame["ready"] and not frame["loading"]
        and frame["visible"] and frame["count"] > 0
        for frame in waiting["frames"]
    ), waiting
    assert tickets == [], "background-only state must not open an empty SSE"
    _assert_no_browser_errors(page, errors)


def test_inherited_projection_unread_requires_new_runtime_event(
    page: Page, backend_url, auth_token,
):
    """A revision digest alone is not proof that a new Agent reply arrived."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)

    result = _app_eval(
        page,
        """
        return (async () => {
          const originalFetch = window.fetch;
          const originalReload = app._reloadSessionCoalesced;
          const originalCurrent = app.currentId;
          const cases = [
            {
              name: "overlay-only", before: "same", desired: "same",
              append: null, current: false,
            },
            {
              name: "cancelled-snapshot", before: "old-cancel", desired: "new-cancel",
              append: { role: "assistant", display_kind: "cancelled_turn" },
              current: false,
            },
            {
              name: "runtime-offscreen", before: "old-runtime", desired: "new-runtime",
              append: {
                role: "assistant", display_kind: "runtime_continuation",
                runtime_event_id: "runtime-new-offscreen",
              },
              current: false,
            },
            {
              name: "runtime-current", before: "old-current", desired: "new-current",
              append: {
                role: "assistant", display_kind: "runtime_continuation",
                runtime_event_id: "runtime-new-current",
              },
              current: true,
            },
          ];
          const specs = new Map();
          try {
            app._reloadSessionCoalesced = async sid => {
              const spec = specs.get(sid);
              spec.loads += 1;
              if (spec.append) {
                spec.st.messages.push({
                  ...spec.append,
                  text: spec.name,
                  _k: `${sid}:idx:${spec.name}`,
                  _noAnim: true,
                });
                spec.st.messageRange.visibleEnd = spec.st.messages.length;
                spec.st.messageRange.total = spec.st.messages.length;
              }
              spec.st.runtimeUiRevision = spec.desired;
              return true;
            };
            const outcomes = [];
            for (const item of cases) {
              const child = `unread-child-${item.name}`;
              const source = `unread-source-${item.name}`;
              const st = app._blankTabState();
              st._sid = child;
              st._loaded = true;
              st.runtimeUiRevision = item.before;
              // An already-visible runtime event must not make an unrelated
              // cancelled snapshot look like a newly-arrived continuation.
              st.messages.push({
                role: "assistant",
                display_kind: "runtime_continuation",
                runtime_event_id: `runtime-existing-${item.name}`,
                text: "existing",
                _k: `${child}:idx:runtime-existing-${item.name}`,
                _noAnim: true,
              });
              st.messageRange.visibleEnd = st.messages.length;
              st.messageRange.total = st.messages.length;
              app.tabState[child] = st;
              specs.set(child, { ...item, st, loads: 0 });
              app.currentId = item.current ? child : "different-visible-tab";
              window.fetch = async (input, init) => {
                const url = String((input && input.url) || input || "");
                if (url.includes(`/sessions/${encodeURIComponent(source)}/active`)) {
                  return {
                    ok: true,
                    json: async () => ({
                      runtime_background_tasks_pending: 0,
                      runtime_continuation_pending: false,
                      runtime_ui_revision: item.desired,
                    }),
                  };
                }
                return originalFetch(input, init);
              };
              app._ensureInheritedTaskPoller(child, source);
              const deadline = performance.now() + 1000;
              while (st._inheritedTaskPoller && performance.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 5));
              }
              const spec = specs.get(child);
              outcomes.push({
                name: item.name,
                unread: !!st.unread,
                revision: st.runtimeUiRevision,
                loads: spec.loads,
              });
              if (st._inheritedTaskPoller) clearInterval(st._inheritedTaskPoller);
              delete app.tabState[child];
              specs.delete(child);
            }
            return outcomes;
          } finally {
            window.fetch = originalFetch;
            app._reloadSessionCoalesced = originalReload;
            app.currentId = originalCurrent;
          }
        })();
        """,
    )

    assert result == [
        {"name": "overlay-only", "unread": False, "revision": "same", "loads": 1},
        {
            "name": "cancelled-snapshot", "unread": False,
            "revision": "new-cancel", "loads": 1,
        },
        {
            "name": "runtime-offscreen", "unread": True,
            "revision": "new-runtime", "loads": 1,
        },
        {
            "name": "runtime-current", "unread": False,
            "revision": "new-current", "loads": 1,
        },
    ]
    _assert_no_browser_errors(page, errors)


def test_background_completion_no_active_fallback_never_blanks_visible_messages(
    page: Page, backend_url, auth_token,
):
    """A settled continuation may race out of /active after its card update."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)

    sid = "perf-background-completion-no-active"
    final_text = "BACKGROUND_COMPLETION_REPLY stays mounted"
    history_requests: list[str] = []
    canonical_messages = [
        {
            "role": "user",
            "text": "BACKGROUND_COMPLETION_USER",
            "ts": 1_700_040_000,
            "uuid": "background-completion-user",
        },
        {
            "role": "assistant",
            "text": final_text,
            "ts": 1_700_040_001,
            "uuid": "background-completion-assistant",
        },
        {
            "role": "tool_use",
            "name": "Task",
            "id": "background-completion-tool",
            "summary": "Background completion fixture",
            "uuid": "background-completion-tool-uuid",
            "task_status": {
                "state": "completed",
                "task_id": "background-completion-task",
                "summary": "fixture complete",
            },
        },
    ]

    def delayed_history(route):
        history_requests.append(route.request.url)
        # Keep the canonical fetch in flight long enough to observe whether the
        # already-rendered pane is hidden/cleared behind a cold-load skeleton.
        time.sleep(0.35)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Background completion no-active",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": canonical_messages,
                "offset": 0,
                "total": len(canonical_messages),
                "has_more": False,
                "history_generation": "gen-background-completion",
                "updated_at": 2,
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", delayed_history)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"background-completion-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._syncQueueFromServer = async () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.sessions = [{
          id: sid, name: "Background completion no-active", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st._seenUpdated = 1;
        st.messages.push(
          {
            role: "user", text: "BACKGROUND_COMPLETION_USER",
            uuid: "background-completion-user", html: "",
            _k: `${sid}:live:1`, _noAnim: true,
          },
          {
            role: "assistant", text: arg.finalText,
            html: `<p>${arg.finalText}</p>`,
            _k: `${sid}:live:2`, _noAnim: true,
          },
          {
            role: "tool_use", name: "Task", id: "background-completion-tool",
            summary: "Background completion fixture",
            task_status: {
              state: "running", task_id: "background-completion-task",
            },
            _k: `${sid}:live:3`, _noAnim: true,
          },
        );
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        app.currentId = sid;
        app.mobileTab = "chat";
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.atBottom = true;
        return new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
        """,
        {"sid": sid, "finalText": final_text},
    )
    expect(page.locator(".msg-pane:visible .msg.assistant")).to_contain_text(
        final_text, timeout=5000,
    )

    _app_eval(
        page,
        """
        return app.send({
          reconnect: true, continuation: true, sessionId: arg,
          turnId: "background-completion-turn", startedAt: Date.now() / 1000,
        });
        """,
        sid,
    )
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """() => window.__emitSse("task_notification", {
          status: "completed",
          tool_use_id: "background-completion-tool",
          task_id: "background-completion-task",
          summary: "fixture complete",
          background_tasks_pending: 0,
          turn_id: "background-completion-turn",
          event_seq: 1,
        })"""
    )
    page.wait_for_function(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const task = app.tabState[sid].messages.find(
            m => m.id === "background-completion-tool");
          return task?.task_status?.state === "completed";
        }""",
        arg=sid,
        timeout=5000,
    )
    # task_notification owns only the original task card. Give the former
    # 700 ms toast batch window time to elapse, then prove neither a transient
    # completion toast nor an eager unread dot was emitted.
    page.wait_for_timeout(800)
    settle_feedback = page.evaluate(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return {
            unread: !!app.tabState[sid].unread,
            completionToast: app.toasts.some(
              toast => /后台任务已完成|Background task finished/
                .test(toast.msg || toast.message || "")),
          };
        }""",
        sid,
    )
    assert settle_feedback == {"unread": False, "completionToast": False}

    result = page.evaluate(
        """async ({ sid, finalText }) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[sid];
          const liveNode = document.querySelector(
            `.msg[data-message-key="${CSS.escape(`${sid}:live:2`)}"]`);
          const frames = [];
          window.__emitSse("error", {
            error: "no active turn",
            turn_id: "background-completion-turn",
            event_seq: 2,
          });
          const deadline = performance.now() + 3000;
          while (!st.messages.some(m => m.uuid === "background-completion-assistant")
                 && performance.now() < deadline) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const pane = Array.from(document.querySelectorAll(".msg-pane"))
              .find(el => getComputedStyle(el).display !== "none");
            const visibleMessages = pane ? Array.from(pane.querySelectorAll(".msg"))
              .filter(el => getComputedStyle(el).display !== "none") : [];
            frames.push({
              ready: app.messagesReady,
              loading: app.messagesLoading,
              textVisible: !!pane && pane.textContent.includes(finalText),
              visibleCount: visibleMessages.length,
            });
          }
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const assistant = st.messages.find(
            m => m.uuid === "background-completion-assistant");
          const canonicalNode = assistant ? document.querySelector(
            `.msg[data-message-key="${CSS.escape(assistant._k)}"]`) : null;
          const finalPane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          return {
            frames,
            sameNode: canonicalNode === liveNode,
            key: assistant?._k || "",
            finalVisible: !!finalPane && finalPane.textContent.includes(finalText),
          };
        }""",
        {"sid": sid, "finalText": final_text},
    )

    assert history_requests, "no-active completion fallback did not reload history"
    assert result["frames"], result
    assert all(frame["ready"] and not frame["loading"] for frame in result["frames"]), result
    assert all(frame["textVisible"] and frame["visibleCount"] > 0
               for frame in result["frames"]), result
    assert result["sameNode"] is True, result
    assert result["key"] == f"{sid}:live:2"
    assert result["finalVisible"] is True, result
    _assert_no_browser_errors(page, errors)


def test_incomplete_background_continuation_keeps_user_queue_runnable(
    page: Page, backend_url, auth_token,
):
    """A missed auto-reaction is not a failure of the queued user prompt."""
    errors = _capture_browser_errors(page)
    _install_fake_event_source(page)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"continuation-queue-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    sid = "continuation-queue-runnable"
    _bootstrap_session_for_real_load(page, sid, "Continuation queue")
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st._loaded = true;
        st.messages = [{
          role: "assistant", text: "BACKGROUND RESULT",
          html: "<p>BACKGROUND RESULT</p>",
          uuid: "continuation-queue-assistant",
          _k: "continuation-queue-assistant", _noAnim: true,
        }];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        st.pendingQueue = [{
          id: "queued-user-followup", text: "USER FOLLOWUP",
          pendingImages: [], pendingDocs: [],
        }];
        st._queuePaused = false;
        app._activateTabState(arg);
        app.model = "e2e-model";
        app._syncSessionListQuiet = () => {};
        app._syncQueueFromServer = async () => {};
        app._reconcileCompletedContinuation = () => {};
        app.ackCurrentActivity = () => {};
        app.highlightCode = async () => {};
        app._ensureBgContPoller = () => {};
        app._drainPendingQueue = (drainSid, turnId) => {
          window.__continuationQueueDrain = { sid: drainSid, turnId };
        };
        return true;
        """,
        sid,
    )

    _app_eval(
        page,
        """
        return app.send({
          reconnect: true, continuation: true, sessionId: arg,
          turnId: "continuation-queue-turn", startedAt: Date.now() / 1000,
        });
        """,
        sid,
    )
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """() => window.__emitSse("done", {
          is_error: true,
          kind: "background_continuation_incomplete",
          error: "background task completed but no final auto-reaction arrived",
          continuation: true,
          background_tasks_pending: 0,
          turn_id: "continuation-queue-turn",
          event_seq: 1,
        })"""
    )
    page.wait_for_function("() => !!window.__continuationQueueDrain")
    result = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          paused: st._queuePaused,
          pending: st.pendingQueue.map(item => item.text),
          drain: window.__continuationQueueDrain,
        };
        """,
        sid,
    )
    assert result == {
        "paused": False,
        "pending": ["USER FOLLOWUP"],
        "drain": {"sid": sid, "turnId": "continuation-queue-turn"},
    }
    _assert_no_browser_errors(page, errors)


def test_mobile_pwa_tabs_preview_rotation_keep_chat_usable(page: Page, backend_url, auth_token):
    """Mobile files/preview/chat switching and rotation keep long chat usable."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    sid = "perf-mobile-pwa"
    messages = _make_mixed_messages(170, "PWA_MSG")
    messages[-1] = {
        "role": "assistant",
        "text": "PWA_LATEST_ASSISTANT visible after rotation " + ("tail " * 80),
        "html": "<p>PWA_LATEST_ASSISTANT visible after rotation tail tail tail</p>",
        "ts": 1_700_001_000,
        "uuid": "pwa-latest",
    }
    _route_windowed_session(page, sid, messages)
    def handle_preview_read(route):
        qs = parse_qs(urlparse(route.request.url).query)
        if qs.get("path", [""])[0] != "reports/perf-preview.md":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="text/markdown",
            body="# Perf preview\n\nThis markdown file is opened through real openFile().\n\n"
                 + "\n".join(f"- preview line {i}" for i in range(40)),
        )

    page.route("**/api/files/read?*", handle_preview_read)
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Perf mobile PWA")
    _app_eval(page, "return app.loadSession(arg);", sid)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.messagesReady === true
            && document.body.textContent.includes("PWA_LATEST_ASSISTANT");
        }""",
        timeout=10000,
    )
    _app_eval(
        page,
        """
        app.__resumeCounts = { health: 0, sessions: 0 };
        app._pingHealth = async () => { app.__resumeCounts.health += 1; };
        app.refreshSessions = async () => { app.__resumeCounts.sessions += 1; };
        return true;
        """,
    )

    _app_eval(
        page,
        """
        return app.openFile({
          path: "reports/perf-preview.md",
          name: "perf-preview.md",
          is_dir: false,
        }, { preview: false, reveal: true });
        """,
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.mobileTab === "preview"
            && app.previewMode === "md"
            && app.rawText.includes("Perf preview")
            && document.body.textContent.includes("Perf preview");
        }""",
        timeout=10000,
    )

    page.locator(SEL_MOBILE_TAB).nth(0).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "files" """,
        timeout=5000,
    )
    page.locator(SEL_MOBILE_TAB).nth(2).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "preview" """,
        timeout=5000,
    )
    page.set_viewport_size({"width": 844, "height": 390})
    page.wait_for_timeout(150)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    page.evaluate(
        """() => {
          Object.defineProperty(document, "visibilityState", {
            value: "hidden", configurable: true,
          });
          document.dispatchEvent(new Event("visibilitychange"));
          Object.defineProperty(document, "visibilityState", {
            value: "visible", configurable: true,
          });
          document.dispatchEvent(new Event("visibilitychange"));
          window.dispatchEvent(new Event("focus"));
        }"""
    )
    page.wait_for_function(
        """() => {
          const c = document.querySelector("#app")._x_dataStack[0].__resumeCounts;
          return c && c.health >= 1 && c.sessions >= 1;
        }""",
        timeout=5000,
    )
    page.locator(SEL_MOBILE_TAB).nth(1).click()
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].mobileTab === "chat" """,
        timeout=5000,
    )
    _app_eval(page, "app.scrollToBottom(true); return true;")
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body");
          return app.messagesReady === true
            && body && body.textContent.includes("PWA_LATEST_ASSISTANT")
            && Math.abs((body.scrollHeight - body.clientHeight) - body.scrollTop) < 48;
        }""",
        timeout=10000,
    )

    layout = page.evaluate(
        """() => {
          const input = document.querySelector(".chat-input-textarea");
          const toolbar = document.querySelector(".chat-toolbar");
          const latest = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none")
            .flatMap(p => Array.from(p.querySelectorAll(".msg.assistant")))
            .find(el => el.textContent.includes("PWA_LATEST_ASSISTANT"));
          const rect = el => {
            const r = el.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                     width: r.width, height: r.height };
          };
          return {
            ready: document.querySelector("#app")._x_dataStack[0].messagesReady,
            mobileTab: document.querySelector("#app")._x_dataStack[0].mobileTab,
            input: rect(input),
            toolbar: rect(toolbar),
            latest: rect(latest),
            viewport: { width: innerWidth, height: innerHeight },
          };
        }"""
    )
    assert layout["ready"] is True
    assert layout["mobileTab"] == "chat"
    resume_counts = _app_eval(page, "return app.__resumeCounts;")
    assert resume_counts["health"] >= 1
    assert resume_counts["sessions"] >= 1
    for key in ("input", "toolbar"):
        box = layout[key]
        assert box["height"] > 0
        assert 0 <= box["top"] < layout["viewport"]["height"]
        assert 0 < box["bottom"] <= layout["viewport"]["height"]
        assert 0 <= box["left"] < layout["viewport"]["width"]
        assert 0 < box["right"] <= layout["viewport"]["width"]
    assert layout["input"]["bottom"] <= layout["toolbar"]["top"] + 2
    assert layout["latest"]["height"] > 0
    assert page.locator(".msg-pane").count() <= 1
    assert _app_eval(page, "return app.messagesReady === true && !app.messagesLoading;") is True

    _assert_no_browser_errors(page, errors)


def test_mobile_composer_focus_closes_activity_group_menu(
    page: Page, backend_url, auth_token,
):
    """Keyboard focus must not leave a portalled task-group menu below chat."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        app.mobileTab = 'chat';
        app.activity.show = true;
        app.activity.moveMenu = {
          show: true, eventId: 'stale-menu',
          style: 'position:fixed;left:8px;top:500px;width:220px;',
        };
        return true;
        """,
    )

    input_box = page.locator(".chat-input-textarea")
    input_box.evaluate("el => { el.disabled = false; el.focus(); }")
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const menu = document.querySelector('.activity-move-menu');
          const backdrop = document.querySelector(
            '.modal-backdrop .activity-modal')?.parentElement;
          return !app.activity.show && !app.activity.moveMenu.show
            && getComputedStyle(menu).display === 'none'
            && getComputedStyle(backdrop).display === 'none';
        }""",
        timeout=2000,
    )
    expect(input_box).to_be_focused()
    _assert_no_browser_errors(page, errors)


def test_mobile_keyboard_close_without_viewport_event_restores_full_layout(
    page: Page, backend_url, auth_token,
):
    """A dropped iOS visualViewport close event must not leave a bottom band."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.mobileTab = 'chat'; return true;")

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const input = document.querySelector('.chat-input-textarea');
          input.disabled = false;
          input.focus();
          const vv = window.visualViewport;
          window.__testVvHeight = innerHeight - 140;
          window.__composerScrollIntoViewCalls = 0;
          input.scrollIntoView = () => { window.__composerScrollIntoViewCalls += 1; };
          Object.defineProperty(vv, 'height', {
            configurable: true,
            get: () => window.__testVvHeight,
          });
          window.__nativeScrollTo = window.scrollTo;
          window.__rootResetCalls = 0;
          window.scrollTo = (...args) => {
            window.__rootResetCalls += 1;
            return window.__nativeScrollTo.apply(window, args);
          };
          app._syncMobileKeyboardViewport();
        }"""
    )
    page.wait_for_function(
        """() => {
          const layout = document.querySelector('.layout').getBoundingClientRect();
          return document.activeElement === document.querySelector('.chat-input-textarea')
            && document.body.classList.contains('kb-open')
            && layout.bottom <= innerHeight - 130;
        }""",
        timeout=2000,
    )

    # Geometry becomes current, but Safari drops resize/scroll notification.
    page.evaluate("() => { window.__testVvHeight = innerHeight; }")
    page.wait_for_function(
        """() => {
          const layout = document.querySelector('.layout').getBoundingClientRect();
          const tab = document.querySelector('.mobile-tab-bar');
          const tabRect = tab.getBoundingClientRect();
          return document.activeElement === document.querySelector('.chat-input-textarea')
            && !document.body.classList.contains('kb-open')
            && getComputedStyle(document.documentElement)
                 .getPropertyValue('--kb-inset').trim() === '0px'
            && Math.abs(layout.top) < 2
            && Math.abs(layout.bottom - innerHeight) < 2
            && getComputedStyle(tab).display === 'flex'
            && Math.abs(tabRect.bottom - innerHeight) < 2
            && window.__rootResetCalls > 0;
        }""",
        timeout=3000,
    )
    assert page.evaluate("() => window.__composerScrollIntoViewCalls") == 0
    page.evaluate(
        """() => {
          delete window.visualViewport.height;
          window.scrollTo = window.__nativeScrollTo;
          delete window.__nativeScrollTo;
          delete window.__testVvHeight;
        }"""
    )
    _assert_no_browser_errors(page, errors)


def test_mobile_composer_footer_is_compact_and_never_overflows(
    browser: Browser, backend_url: str, auth_token: str,
):
    """Real touch layout keeps the composer/footer usable at 390px and 320px."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    errors = _capture_browser_errors(page)
    try:
        _login(page, backend_url, auth_token)
        _app_eval(
            page,
            """
            app.mobileTab = "chat";
            app.availableModels = [{
              model: "e2e-model-with-a-long-label",
              label: "E2E model with a long label",
              group: "e2e",
              supports_thinking: true,
            }];
            app.model = "e2e-model-with-a-long-label";
            const st = app._ensureTabState(app.currentId);
            st.streaming = false;
            app.streaming = false;
            return true;
            """,
        )
        page.wait_for_function(
            """() => {
              const input = document.querySelector(".chat-input-textarea");
              return input && !input.disabled
                && getComputedStyle(document.querySelector(".pane.chat")).display !== "none";
            }"""
        )
        page.locator(".chat-input-textarea").fill("footer layout check")

        def composer_metrics():
            return page.evaluate(
                """() => {
                  const pick = selector => document.querySelector(selector);
                  const box = selector => {
                    const r = pick(selector).getBoundingClientRect();
                    return {top: r.top, bottom: r.bottom, width: r.width, height: r.height};
                  };
                  const toolbar = pick(".chat-toolbar");
                  const send = pick(".chat-toolbar-queue");
                  const stop = pick(".chat-toolbar-stop");
                  const textLabelCount = button => Array.from(button.children)
                    .filter(child => !child.classList.contains("chat-toolbar-queue-badge")
                      && getComputedStyle(child).display !== "none"
                      && child.textContent.trim())
                    .length;
                  return {
                    composer: box(".chat-input"),
                    wrapPadding: getComputedStyle(pick(".chat-input-wrap")).paddingTop,
                    flexShrink: getComputedStyle(pick(".chat-input")).flexShrink,
                    toolbarOverflow: toolbar.scrollWidth - toolbar.clientWidth,
                    nav: box(".mobile-tab-bar"),
                    sendWidth: send.getBoundingClientRect().width,
                    sendLabelCount: textLabelCount(send),
                    sendAria: send.getAttribute("aria-label") || "",
                    stopWidth: stop.getBoundingClientRect().width,
                    stopLabelCount: textLabelCount(stop),
                    stopAria: stop.getAttribute("aria-label") || "",
                  };
                }"""
            )

        idle = composer_metrics()
        assert idle["wrapPadding"] == "0px"
        assert idle["flexShrink"] == "0"
        assert idle["toolbarOverflow"] <= 1
        assert idle["sendWidth"] == 44
        assert idle["sendLabelCount"] == 0
        assert idle["sendAria"]
        assert idle["composer"]["height"] <= 120
        assert idle["composer"]["bottom"] <= idle["nav"]["top"] + 1

        _app_eval(
            page,
            """
            const st = app._ensureTabState(app.currentId);
            st.streaming = true;
            app.streaming = true;
            return true;
            """,
        )
        page.wait_for_function(
            """() => {
              const toolbar = document.querySelector(".chat-toolbar");
              const stop = document.querySelector(".chat-toolbar-stop");
              return toolbar?.classList.contains("has-stop")
                && stop?.getBoundingClientRect().width > 0;
            }"""
        )
        busy = composer_metrics()
        assert busy["toolbarOverflow"] <= 1
        assert busy["stopWidth"] == 44
        assert busy["stopLabelCount"] == 0
        assert busy["stopAria"]
        assert busy["composer"]["bottom"] <= busy["nav"]["top"] + 1

        page.set_viewport_size({"width": 320, "height": 700})
        page.wait_for_timeout(100)
        compact = composer_metrics()
        assert compact["toolbarOverflow"] <= 1
        assert compact["composer"]["bottom"] <= compact["nav"]["top"] + 1
        _assert_no_browser_errors(page, errors)
    finally:
        context.close()


@pytest.mark.parametrize(
    ("viewport", "expects_plain"),
    [
        ({"width": 390, "height": 844}, True),
        ({"width": 1440, "height": 900}, True),
    ],
    ids=["mobile", "desktop"],
)
def test_120kb_mixed_sse_stream_renders_final_assistant_html(
    page: Page, backend_url, auth_token, viewport, expects_plain,
):
    """Drive the real send()/SSE handlers with a long mixed event stream."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size(viewport)
    _install_fake_event_source(page)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"e2e-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)

    _app_eval(
        page,
        """
        const sid = "perf-stream";
        const now = Date.now();
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.sessions = [{ id: sid, name: "Perf stream", updated_at: now / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        app.currentId = sid;
        app._activateTabState(sid);
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app.input = "stream a long deterministic answer";
        app.atBottom = true;
        return true;
        """,
    )
    page.evaluate(
        """() => {
          window.__longTasks = [];
          if (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes?.includes('longtask')) {
            window.__longTaskObserver = new PerformanceObserver(list => {
              for (const e of list.getEntries()) window.__longTasks.push(e.duration);
            });
            window.__longTaskObserver.observe({ type: 'longtask', buffered: true });
          }
        }"""
    )
    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )

    page.evaluate(
        """() => {
          window.__emitSse("thinking", { text: "planning ".repeat(80) });
          window.__emitSse("tool_use", {
            id: "toolu_perf_1", name: "Bash", summary: "generate fixture",
            input: { command: "printf long-stream" },
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_1", tool_name: "Bash", preview: "ok",
            text: "result ".repeat(300), truncated: false, is_error: false,
            bash: { stdout: "ok", stderr: "", exit_code: 0 },
          });
          window.__emitSse("text", { text: "MID_STREAM_VISIBLE_1 " + "alpha ".repeat(80) });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && last && last.role === "assistant"
            && last._streamPlain === true
            && last._streamText.includes("MID_STREAM_VISIBLE_1")
            && body.includes("MID_STREAM_VISIBLE_1");
        }""",
        timeout=10000,
    )
    mid_1 = _app_eval(
        page,
        """
        const last = app.messages[app.messages.length - 1];
        return {
          streaming: app.streaming,
          textLength: last.text.length,
          streamTextLength: last._streamText.length,
        };
        """,
    )
    assert mid_1["streaming"] is True

    page.evaluate(
        """() => {
          window.__emitSse("text", { text: "MID_STREAM_VISIBLE_2 " + "beta ".repeat(120) });
        }"""
    )
    page.wait_for_function(
        """prev => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true
            && last && last.role === "assistant"
            && last.text.length > prev.textLength
            && last._streamText.length > prev.streamTextLength
            && last._streamText.includes("MID_STREAM_VISIBLE_2")
            && body.includes("MID_STREAM_VISIBLE_2");
        }""",
        arg=mid_1,
        timeout=10000,
    )

    page.evaluate(
        """() => {
          const finalText = "FINAL_ASSISTANT_HTML_COMPLETE " + "long-stream-token ".repeat(7200);
          window.__emitSse("thinking", { text: "checking ".repeat(60) });
          window.__emitSse("tool_use", {
            id: "toolu_perf_2", name: "Read", summary: "inspect file",
            input: { file_path: "fixture.txt" },
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_2", tool_name: "Read", preview: "line 1",
            text: "1: fixture\\n".repeat(1000), truncated: false, is_error: false,
          });
          window.__emitSse("text", { text: "second assistant segment before todos. " });
          window.__emitSse("tool_use", {
            id: "toolu_perf_3", name: "TodoWrite", summary: "update plan",
            todos: [
              { content: "stream", status: "completed" },
              { content: "render", status: "in_progress" },
            ],
          });
          window.__emitSse("tool_result", {
            id: "toolu_perf_3", tool_name: "TodoWrite", preview: "updated",
            text: "todos updated", truncated: false, is_error: false,
          });
          window.__emitSse("text", { text: finalText });
        }"""
    )
    page.wait_for_function(
        """expectsPlain => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const last = app.messages[app.messages.length - 1];
          return app.streaming === true && last
            && last._streamPlain === expectsPlain
            && last.text.includes('FINAL_ASSISTANT_HTML_COMPLETE')
            && (expectsPlain || last.html.includes('FINAL_ASSISTANT_HTML_COMPLETE'));
        }""",
        arg=expects_plain,
        timeout=10000,
    )
    page.evaluate(
        """() => window.__emitSse("done", {
          total_cost_usd: 0.001,
          session_usage: { context_used_pct: 10, context_used: 1000, context_limit: 100000 },
        })"""
    )

    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const last = app.messages[app.messages.length - 1];
          return app.streaming === false
            && last && last.role === "assistant"
            && last.text.length >= 120000
            && last.text.includes("FINAL_ASSISTANT_HTML_COMPLETE")
            && last.html.includes("FINAL_ASSISTANT_HTML_COMPLETE");
        }""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant").last).to_contain_text(
        "FINAL_ASSISTANT_HTML_COMPLETE", timeout=5000
    )
    assert page.locator(".msg-pane:visible .msg").count() <= 50
    assert _app_eval(page, "return app.messages.length;") <= 50
    assert _app_eval(
        page,
        """
        const roles = app.messages.map(m => m.role);
        const last = app.messages[app.messages.length - 1];
        return roles.includes("thinking")
          && roles.includes("tool_use")
          && roles.includes("tool_result")
          && last.role === "assistant"
          && last.text.length >= 120000
          && last.html.length > 0
          && !last._streamPlain;
        """,
    )
    render_stats = _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        return {
          rich: st._streamRichRenderCount,
          plain: st._streamPlainRenderCount,
          mounted: Array.from(document.querySelectorAll('.msg-pane'))
            .filter(p => getComputedStyle(p).display !== 'none')
            .reduce((n, p) => n + p.querySelectorAll('.msg').length, 0),
          cached: st.messages.length,
        };
        """,
    )
    if expects_plain:
        assert render_stats["plain"] >= 1
        assert render_stats["rich"] <= 8
    else:
        assert render_stats["plain"] == 0
        assert render_stats["rich"] >= 1
    assert render_stats["mounted"] <= 60
    assert render_stats["cached"] >= render_stats["mounted"]
    long_tasks = page.evaluate("() => window.__longTasks || []")
    assert max(long_tasks or [0]) < 2000, long_tasks

    _assert_no_browser_errors(page, errors)
