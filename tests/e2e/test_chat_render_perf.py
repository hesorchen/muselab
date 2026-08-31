"""Browser stress checks for long chat rendering.

These tests deliberately run against the real frontend bundle and Alpine DOM,
but keep the model/provider path deterministic by injecting controlled session
state or a fake EventSource stream. They cover the long-history and long-stream
regression classes that static lint cannot see.
"""
from __future__ import annotations

import json
import re
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
          return app && app.authed === true && app.appReady && app._modelsLoaded
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
            const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
            return (new AsyncFunction("app", "arg", body))(app, arg);
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


def _route_windowed_session(
    page: Page,
    sid: str,
    messages: list[dict],
    *,
    updated_at: float | None = None,
) -> list[dict]:
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
            "full": "full" in qs,
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
                "history_order": "full" if "full" in qs else "normal",
                "history_generation": "gen-e2e-1",
                **({"updated_at": updated_at} if updated_at is not None else {}),
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", handle)
    return requests


def _install_fake_event_source(page: Page) -> None:
    page.add_init_script(
        """
        (() => {
          const streams = [];
          const originalFetch = window.fetch.bind(window);
          window.fetch = (url, init) => {
            if (String(url).includes("/api/chat/stream/mux/start")) {
              return Promise.resolve(new Response("{}", {
                status: 404, headers: {"Content-Type": "application/json"},
              }));
            }
            return originalFetch(url, init);
          };
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
          window.__emitSseAt = (index, type, payload) => {
            const es = window.__fakeChatStreams()[index];
            if (!es) throw new Error("no fake chat EventSource at index " + index);
            es.dispatchEvent(new MessageEvent(type, {
              data: typeof payload === "string" ? payload : JSON.stringify(payload || {}),
            }));
          };
          window.__emitSse = (type, payload) => {
            const chatStreams = window.__fakeChatStreams();
            window.__emitSseAt(chatStreams.length - 1, type, payload);
          };
        })();
        """
    )


def _install_fake_mux_event_source(page: Page) -> None:
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
                if (this.readyState === 2) return;
                this.readyState = 1;
                if (this.onopen) this.onopen(new Event("open"));
                this.dispatchEvent(new Event("open"));
              }, 0);
            }
            close() { this.readyState = 2; this.closed = true; }
          }
          window.EventSource = FakeEventSource;
          window.__fakeMuxStreams = () => streams.filter(
            es => String(es.url || "").includes("/api/chat/stream/mux?")
          );
          window.__emitMux = (type, payload, index = -1) => {
            const mux = window.__fakeMuxStreams();
            const es = mux[index < 0 ? mux.length - 1 : index];
            if (!es) throw new Error("no fake mux EventSource");
            es.dispatchEvent(new MessageEvent(type, {
              data: typeof payload === "string" ? payload : JSON.stringify(payload || {}),
            }));
          };
          window.__disconnectMux = () => {
            const mux = window.__fakeMuxStreams();
            const es = mux[mux.length - 1];
            if (!es) throw new Error("no fake mux EventSource");
            es.dispatchEvent(new Event("error"));
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


def test_mux_routes_two_sessions_reconnects_with_checkpoints_and_defers_watcher_runtime(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _install_fake_mux_event_source(page)
    mux_starts: list[dict] = []
    turn_starts: list[dict] = []

    def handle_mux_start(route) -> None:
        mux_starts.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": f"mux-{len(mux_starts)}"}),
        )

    def handle_turn_start(route) -> None:
        turn_starts.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "accepted": True,
                "session_id": route.request.post_data_json["session_id"],
                "turn_id": "turn-local",
                "started_at": int(time.time()),
            }),
        )

    page.route("**/api/chat/stream/mux/start", handle_mux_start)
    page.route("**/api/chat/turns/start", handle_turn_start)
    _login(page, backend_url, auth_token)
    page.wait_for_function("window.__fakeMuxStreams().length === 1")

    watcher_sid = "mux-watcher-session"
    initial = page.evaluate(
        """async watcherSid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const localSid = app.currentId;
          app._confirmSessionBusy = async () => false;
          app._awaitRuntimeSettingPatches = async () => true;
          app.availableModels = [{
            model: 'mux-e2e-model', label: 'Mux E2E', group: 'e2e',
            supports_thinking: true,
          }];
          app.model = 'mux-e2e-model';
          app.permission = 'default';
          app.sessions = app.sessions.map(session => session.id === localSid
            ? {...session, model: 'mux-e2e-model', permission: 'default'} : session);
          app._ensureTabState(localSid).permission = 'default';
          app.sessions.push({
            id: watcherSid, name: 'Watcher session', model: 'mux-e2e-model',
            permission: 'default', cwd: app.currentWorkspacePath(), active: true,
          });
          window.__emitMux('session_state', {
            session_id: watcherSid, active: true, attachable: false,
            background: true, continuation: false, turn_id: 'watcher-gap',
            started_at: Math.floor(Date.now() / 1000),
            background_tasks_pending: 1,
          });
          await new Promise(resolve => setTimeout(resolve, 30));
          const watcherCreatedDuringGap = !!app.tabState[watcherSid];
          const watcher = app._ensureTabState(watcherSid);
          watcher._loaded = true;
          if (!app.openTabIds.includes(watcherSid)) app.openTabIds.push(watcherSid);
          window.__emitMux('session_state', {
            session_id: watcherSid, active: true, attachable: true,
            background: false, continuation: true, turn_id: 'turn-watcher',
            parent_turn_id: 'watcher-gap', started_at: Math.floor(Date.now() / 1000),
          });
          for (let i = 0; i < 100 && !watcher.es; i++) {
            await new Promise(resolve => setTimeout(resolve, 10));
          }
          const local = app._ensureTabState(localSid);
          local.draft.input = 'MUX_LOCAL_PROMPT';
          app._activateComposerState(localSid);
          await app.send();
          return {
            localSid,
            watcherCreatedDuringGap,
            watcherAttached: !!watcher.es,
            nativeStreamCount: window.__fakeMuxStreams().length,
          };
        }""",
        watcher_sid,
    )
    assert initial == {
        "localSid": initial["localSid"],
        "watcherCreatedDuringGap": False,
        "watcherAttached": True,
        "nativeStreamCount": 1,
    }

    page.evaluate(
        """({localSid, watcherSid}) => {
          window.__emitMux('text', {
            session_id: localSid, turn_id: 'turn-local', event_seq: 1,
            text: 'MUX_LOCAL_REPLY',
          });
          window.__emitMux('text', {
            session_id: watcherSid, turn_id: 'turn-watcher', event_seq: 1,
            text: 'MUX_BACKGROUND_REPLY',
          });
        }""",
        {"localSid": initial["localSid"], "watcherSid": watcher_sid},
    )
    page.wait_for_function(
        """sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[sid]?.lastEventSeq === 1;
        }""",
        arg=initial["localSid"],
    )
    page.evaluate(
        """async sid => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.activateTab(sid);
        }""",
        watcher_sid,
    )
    page.wait_for_function(
        """sid => document.querySelector('#app')._x_dataStack[0]
          .tabState[sid].messages.some(m => m.text === 'MUX_BACKGROUND_REPLY')""",
        arg=watcher_sid,
    )

    page.evaluate("window.__disconnectMux()")
    page.wait_for_function("window.__fakeMuxStreams().length === 2", timeout=5000)
    state = page.evaluate(
        """({localSid, watcherSid}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            localStreaming: app.tabState[localSid].streaming,
            watcherStreaming: app.tabState[watcherSid].streaming,
            localSeq: app.tabState[localSid].lastEventSeq,
            watcherSeq: app.tabState[watcherSid].lastEventSeq,
            nativeStreamCount: window.__fakeMuxStreams().length,
          };
        }""",
        {"localSid": initial["localSid"], "watcherSid": watcher_sid},
    )
    assert state == {
        "localStreaming": True,
        "watcherStreaming": True,
        "localSeq": 1,
        "watcherSeq": 1,
        "nativeStreamCount": 2,
    }
    assert len(turn_starts) == 1
    assert turn_starts[0] == {
        "prompt": "MUX_LOCAL_PROMPT",
        "session_id": initial["localSid"],
        "model": "mux-e2e-model",
        "permission": "default",
        "image_ids": "",
        "mobile": False,
    }
    assert len(mux_starts) == 2
    checkpoints = {
        (row["session_id"], row["turn_id"]): row["last_event_seq"]
        for row in mux_starts[1]["checkpoints"]
    }
    assert checkpoints[(initial["localSid"], "turn-local")] == 1
    assert checkpoints[(watcher_sid, "turn-watcher")] == 1
    _assert_no_browser_errors(page, errors)


def test_mux_inactive_retires_matching_turn_without_harming_successor(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _install_fake_mux_event_source(page)
    page.route(
        "**/api/chat/stream/mux/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "mux-inactive-settlement"}),
        ),
    )
    _login(page, backend_url, auth_token)
    page.wait_for_function("window.__fakeMuxStreams().length === 1")

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          const meta = app.sessions.find(session => session.id === sid);
          const originalReload = app._scheduleCanonicalStreamReload;
          let reloads = 0;
          app._scheduleCanonicalStreamReload = () => { reloads += 1; return true; };
          try {
            st._loaded = true;
            st.messages = [{ role: 'assistant', text: 'VISIBLE_COMPLETED_REPLY' }];
            st.streaming = false;
            st.streamPhase = '';
            st.activeTurnId = '';
            st.es = null;
            if (meta) meta.active = true;

            // A fast server-drained queue turn can start and finish before this
            // browser ever owns its mux channel. Its inactive aggregate frame is
            // then the only live hint that canonical history has a missing suffix.
            window.__emitMux('session_state', {
              session_id: sid, turn_id: 'turn-headless-queued', active: false,
              stopping: false, attachable: false, activity_source: 'queued',
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const headless = {
              streaming: st.streaming,
              activeTurnId: st.activeTurnId,
              reloads,
              text: st.messages[0] && st.messages[0].text,
              metaActive: meta && meta.active,
            };

            st.streaming = true;
            st.streamPhase = 'runtime';
            st.activeTurnId = 'turn-a';
            st._stoppingTurnId = 'turn-a';
            st.es = app._chatMuxChannel(sid, 'turn-a');
            app._activateChatMuxChannel(st.es);
            st._streamStartedAt = Date.now() - 5000;
            st.streamElapsed = 5;
            st._streamTimer = setInterval(() => {}, 1000);
            st._stallWatch = setInterval(() => {}, 1000);
            if (meta) meta.active = true;

            window.__emitMux('session_state', {
              session_id: sid, turn_id: 'turn-a', active: false,
              stopping: false, attachable: false,
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const matching = {
              streaming: st.streaming,
              phase: st.streamPhase,
              es: st.es,
              timer: st._streamTimer,
              stall: st._stallWatch,
              stoppingTurn: st._stoppingTurnId,
              elapsed: st.streamElapsed,
              reloads,
              text: st.messages[0] && st.messages[0].text,
              metaActive: meta && meta.active,
            };

            st.streaming = true;
            st.streamPhase = 'runtime';
            st.activeTurnId = 'turn-b';
            st._stoppingTurnId = '';
            st.es = app._chatMuxChannel(sid, 'turn-b');
            app._activateChatMuxChannel(st.es);
            st._streamTimer = setInterval(() => {}, 1000);
            st._stallWatch = setInterval(() => {}, 1000);
            if (meta) meta.active = true;
            const successorEs = st.es;
            const successorTimer = st._streamTimer;

            window.__emitMux('session_state', {
              session_id: sid, turn_id: 'turn-a', active: false,
              stopping: false, attachable: false,
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const successor = {
              streaming: st.streaming,
              phase: st.streamPhase,
              sameEs: st.es === successorEs,
              sameTimer: st._streamTimer === successorTimer,
              activeTurnId: st.activeTurnId,
              reloads,
              metaActive: meta && meta.active,
            };
            clearInterval(st._streamTimer);
            clearInterval(st._stallWatch);
            if (st.es) st.es.close();
            return { headless, matching, successor };
          } finally {
            app._scheduleCanonicalStreamReload = originalReload;
          }
        }"""
    )
    assert result["headless"] == {
        "streaming": False,
        "activeTurnId": "",
        "reloads": 1,
        "text": "VISIBLE_COMPLETED_REPLY",
        "metaActive": False,
    }
    assert result["matching"] == {
        "streaming": False,
        "phase": "",
        "es": None,
        "timer": None,
        "stall": None,
        "stoppingTurn": "",
        "elapsed": 0,
        "reloads": 2,
        "text": "VISIBLE_COMPLETED_REPLY",
        "metaActive": False,
    }
    assert result["successor"] == {
        "streaming": True,
        "phase": "runtime",
        "sameEs": True,
        "sameTimer": True,
        "activeTurnId": "turn-b",
        "reloads": 2,
        "metaActive": True,
    }
    _assert_no_browser_errors(page, errors)


def test_terminal_turn_cannot_be_reattached_by_stale_active_state(
    page: Page, backend_url, auth_token,
):
    """A completed immutable turn stays completed while postlude state lags."""
    errors = _capture_browser_errors(page)
    _install_fake_mux_event_source(page)
    page.route(
        "**/api/chat/stream/mux/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ticket": "mux-stale-terminal"}),
        ),
    )
    _login(page, backend_url, auth_token)
    page.wait_for_function("window.__fakeMuxStreams().length === 1")

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          const originals = {
            send: app.send,
            fetch: app._fetchWithDeadline,
            queue: app._syncQueueFromServer,
            resume: app._resumePendingCanonicalSync,
            bgPoller: app._ensureBgContPoller,
          };
          const sends = [];
          let resumes = 0;
          let probePayload = {
            active: true, attachable: true, background: false,
            continuation: false, turn_id: 'turn-completed',
            started_at: Math.floor(Date.now() / 1000),
          };
          try {
            st._loaded = true;
            st.messages = [{ role: 'assistant', text: 'VISIBLE_DONE_REPLY' }];
            st.streaming = false;
            st.es = null;
            st.activeTurnId = '';
            st._lastTerminalTurnId = 'turn-completed';
            app.sessions = app.sessions.map(session => session.id === sid
              ? {...session, active: false, turn_active: false,
                  background_active: false}
              : session);
            app.send = async options => { sends.push(options); return true; };
            app._syncQueueFromServer = () => Promise.resolve(true);
            app._resumePendingCanonicalSync = () => { resumes += 1; };
            app._ensureBgContPoller = () => {};
            app._fetchWithDeadline = async () => new Response(
              JSON.stringify(probePayload),
              {status: 200, headers: {'Content-Type': 'application/json'}},
            );

            const probeResult = await app._probeActiveTurn(sid, st);
            window.__emitMux('session_state', {
              session_id: sid, active: true, attachable: true,
              background: false, continuation: false,
              turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const afterStale = {
              probeResult,
              sends: sends.length,
              resumes,
              streaming: st.streaming,
              hasEs: !!st.es,
              activeTurnId: st.activeTurnId,
              text: st.messages[0] && st.messages[0].text,
              metaActive: !!app.sessions.find(session => session.id === sid)?.active,
            };

            // The main Result can legitimately leave SDK background tasks
            // attached to the same origin turn id. That background-only busy
            // state remains visible and must not be mistaken for stale foreground.
            probePayload = {
              active: true, attachable: false, background: true,
              continuation: false, turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
              background_tasks_pending: 1,
            };
            await app._probeActiveTurn(sid, st);
            window.__emitMux('session_state', {
              session_id: sid, active: true, attachable: false,
              background: true, continuation: false,
              turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
              background_tasks_pending: 1,
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const backgroundAccepted = {
              sends: sends.length,
              backgroundActive: st.backgroundActive,
              backgroundTaskCount: st.backgroundTaskCount,
              activeTurnId: st.activeTurnId,
              metaActive: !!app.sessions.find(session => session.id === sid)?.active,
            };

            // The foreground broadcast can linger during its postlude while
            // background tasks already exist. Suppress only its stale reattach;
            // retain the legitimate background busy state and task count.
            probePayload = {
              active: true, attachable: true, background: false,
              continuation: false, turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
              background_tasks_pending: 1,
            };
            await app._probeActiveTurn(sid, st);
            window.__emitMux('session_state', probePayload);
            await new Promise(resolve => setTimeout(resolve, 30));
            const backgroundSurvivesPostlude = {
              sends: sends.length,
              backgroundActive: st.backgroundActive,
              backgroundTaskCount: st.backgroundTaskCount,
              metaBackground: !!app.sessions.find(
                session => session.id === sid)?.background_active,
            };

            window.__emitMux('session_state', {
              session_id: sid, active: true, attachable: true,
              background: false, continuation: false, turn_id: 'turn-successor',
              started_at: Math.floor(Date.now() / 1000),
            });
            for (let i = 0; i < 50 && sends.length < 1; i++) {
              await new Promise(resolve => setTimeout(resolve, 10));
            }
            st.streaming = true;
            st.activeTurnId = 'turn-successor';
            st.es = app._chatMuxChannel(sid, 'turn-successor');
            app._activateChatMuxChannel(st.es);
            const successorEs = st.es;
            probePayload = {
              active: true, attachable: true, background: false,
              continuation: false, turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
            };
            await app._probeActiveTurn(sid, st);
            window.__emitMux('session_state', {
              session_id: sid, active: true, attachable: true,
              background: false, continuation: false,
              turn_id: 'turn-completed',
              started_at: Math.floor(Date.now() / 1000),
            });
            await new Promise(resolve => setTimeout(resolve, 30));
            const successorPreserved = {
              streaming: st.streaming,
              sameEs: st.es === successorEs,
              activeTurnId: st.activeTurnId,
              sends: sends.length,
            };
            if (st.es) st.es.close();
            st.es = null;
            st.streaming = false;
            return {
              afterStale,
              backgroundAccepted,
              backgroundSurvivesPostlude,
              successorPreserved,
              successorSends: sends.length,
              successorTurnId: sends[0] && sends[0].turnId,
              successorMuxAttach: !!(sends[0] && sends[0]._muxAttach),
            };
          } finally {
            app.send = originals.send;
            app._fetchWithDeadline = originals.fetch;
            app._syncQueueFromServer = originals.queue;
            app._resumePendingCanonicalSync = originals.resume;
            app._ensureBgContPoller = originals.bgPoller;
          }
        }"""
    )
    assert result == {
        "afterStale": {
            "probeResult": False,
            "sends": 0,
            "resumes": 2,
            "streaming": False,
            "hasEs": False,
            "activeTurnId": "",
            "text": "VISIBLE_DONE_REPLY",
            "metaActive": False,
        },
        "backgroundAccepted": {
            "sends": 0,
            "backgroundActive": True,
            "backgroundTaskCount": 1,
            "activeTurnId": "turn-completed",
            "metaActive": True,
        },
        "backgroundSurvivesPostlude": {
            "sends": 0,
            "backgroundActive": True,
            "backgroundTaskCount": 1,
            "metaBackground": True,
        },
        "successorPreserved": {
            "streaming": True,
            "sameEs": True,
            "activeTurnId": "turn-successor",
            "sends": 1,
        },
        "successorSends": 1,
        "successorTurnId": "turn-successor",
        "successorMuxAttach": True,
    }
    _assert_no_browser_errors(page, errors)


def test_mux_coordinator_connects_before_background_history_warmup(
    page: Page, backend_url, auth_token,
):
    """The root live transport owns events before background history starts."""
    _login(page, backend_url, auth_token)
    order = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          if (app._chatMuxCoordinatorPromise) {
            try { await app._chatMuxCoordinatorPromise; } catch (_) {}
          }
          if (app._chatMuxStartPromise) {
            try { await app._chatMuxStartPromise; } catch (_) {}
          }
          app._setChatMuxUnsupported();
          const originalEnsure = app._ensureChatMux;
          const originalHistory = app._bootstrapChatMuxHistory;
          const order = [];
          app._chatMuxSupported = true;
          app._ensureChatMux = async () => {
            order.push('mux:start');
            await Promise.resolve();
            order.push('mux:connected');
            return true;
          };
          app._bootstrapChatMuxHistory = async () => order.push('history:start');
          try {
            await app._startChatMuxCoordinator();
            return order;
          } finally {
            app._ensureChatMux = originalEnsure;
            app._bootstrapChatMuxHistory = originalHistory;
          }
        }"""
    )
    assert order == ["mux:start", "mux:connected", "history:start"]


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
        app._ensureTabState(app.currentId).messagesReady = true;
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
    expect(page.locator(".thinking-head + pre").filter(has_text="THINKING_FULL_BODY_MARKER")).to_be_visible()
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
        app._modelsLoaded = true;
        app.sessions = [{ id: arg.sid, name: arg.name, updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg.sid];
        app.tabState = {};
        app.currentId = arg.sid;
        app.mobileTab = "chat";
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
        "popover": True,
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
    keyboard_steps = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const staleItem = app.sessionTodoItems()
            .find(item => item.id === 'todo-medium');
          const snapshot = () => app.sessionTodoItems()
            .map(item => item.id + ':' + item.priority);
          const eventFor = key => ({
            key, altKey: false, ctrlKey: false, metaKey: false,
            preventDefault() {}, stopPropagation() {},
          });
          app.onSessionTodoGripKeydown(eventFor('ArrowLeft'), staleItem);
          const afterLeft = snapshot();
          app.onSessionTodoGripKeydown(eventFor('ArrowUp'), staleItem);
          return {afterLeft, afterUp: snapshot()};
        }"""
    )
    assert keyboard_steps == {
        "afterLeft": ["todo-high:high", "todo-low:low", "todo-medium:high"],
        "afterUp": ["todo-medium:high", "todo-high:high", "todo-low:low"],
    }
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app._todoPushPromise === null && !app._todoPushPending;
        }"""
    )

    expect(modal.locator('.session-todo-lane.is-high [data-todo-id="todo-medium"]')).to_be_visible()
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
            app._ensureTabState(modelSid);
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
          return rendered === historySize;
        }""",
        arg=history_size,
        timeout=5000,
    )

    for sid in [f"perf-history-{i}" for i in [1, 2, 3, 4, 5, 0, 5]]:
        _app_eval(
            page,
            """
            app.currentId = arg;
            app._ensureTabState(app.currentId).messagesReady = true;
            app._ensureTabState(app.currentId).messagesLoading = false;
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
                    && p.querySelectorAll(".msg").length === historySize);
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
                    messagesLength: app._ensureTabState(app.currentId).messages.length,
                    visiblePanes: Array.from(document.querySelectorAll(".msg-pane"))
                      .filter(p => getComputedStyle(p).display !== "none")
                      .map(p => ({ count: p.querySelectorAll(".msg").length,
                                   text: p.textContent.slice(0, 400) })),
                  };
                }"""
            )
            raise AssertionError(f"target tail not visible: {expected_tail}; diag={diag}") from exc
        snap = _visible_pane_with_text_snapshot(page, expected_tail)
        assert snap["msgCount"] == history_size
        assert expected_tail in snap["text"]
        assert page.locator(".msg-pane").count() <= 1
        assert page.locator(".msg-pane").count() <= 1

    _assert_no_browser_errors(page, errors)


def test_desktop_session_switch_keeps_bounded_warm_panes_and_composer_stable(
    page: Page, backend_url: str, auth_token: str,
):
    """Desktop bounds warm panes even when every inactive session is streaming."""
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
          st.streaming = true;
          app.tabState[id] = st;
          app._ensureTabState(id);
        }
        app.currentId = arg.ids[0];
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
            && visible[0].querySelectorAll(".msg").length === 40
            && getComputedStyle(document.querySelector(".chat-transcript-loading-overlay")).display === "none";
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
            const settleDeadline = performance.now() + 1000;
            while ((app.transcriptLoadingVisible()
                || getComputedStyle(document.querySelector(
                  ".chat-transcript-loading-overlay")).display !== "none")
                && performance.now() < settleDeadline) {
              await new Promise(resolve => requestAnimationFrame(resolve));
            }
            const panes = Array.from(document.querySelectorAll(".msg-pane"));
            const visible = panes.filter(
              pane => getComputedStyle(pane).display !== "none");
            out.push({
              elapsed: performance.now() - started,
              expectedPanes: Math.min(ids.indexOf(id) + 1, app.WARM_TRANSCRIPT_LIMIT),
              panes: panes.length,
              visible: visible.length,
              visibleMessages: visible[0]?.querySelectorAll(".msg").length || 0,
              ready: app._ensureTabState(app.currentId).messagesReady,
              skeleton: getComputedStyle(
                document.querySelector(".chat-transcript-loading-overlay")).display,
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
    assert all(row["panes"] == row["expectedPanes"] and row["visible"] == 1 for row in switches), switches
    assert all(row["visibleMessages"] == 40 for row in switches)
    assert all(row["ready"] for row in switches), switches
    assert switches[-1]["skeleton"] == "none", switches
    assert abs(after["y"] - before["y"]) < 1
    assert abs(after["height"] - before["height"]) < 1
    _assert_no_browser_errors(page, errors)


def test_cold_session_switch_shields_every_frame_until_transcript_paints(
    page: Page, backend_url: str, auth_token: str,
):
    """A gated cold switch never exposes onboarding or the previous transcript."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1280, "height": 800})
    _login(page, backend_url, auth_token)
    sid_a = "loading-shield-a"
    sid_b = "loading-shield-b"
    marker_a = "OLD_SESSION_CONTENT_MUST_NEVER_FLASH"
    marker_b = "NEW_SESSION_CONTENT_AFTER_RELEASE"
    payload = {
        "sidA": sid_a,
        "sidB": sid_b,
        "messagesA": _make_mixed_messages(6, marker_a),
        "messagesB": _make_mixed_messages(6, marker_b),
    }
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.sessions = [
          {id: arg.sidA, name: "Loaded A", message_count: arg.messagesA.length,
           updated_at: 1, model: "e2e-model"},
          {id: arg.sidB, name: "Cold B", message_count: arg.messagesB.length,
           updated_at: 2, model: "e2e-model"},
        ];
        app.openTabIds = [arg.sidA, arg.sidB];
        app.tabState = {};
        const a = app._ensureTabState(arg.sidA);
        a.messages = app._historyEnvelopes(arg.sidA, arg.messagesA);
        a.messageRange.visibleEnd = a.messages.length;
        a.messageRange.total = a.messages.length;
        a._installedCanonicalCount = a.messages.length;
        a._loaded = true;
        a.messagesReady = true;
        const b = app._ensureTabState(arg.sidB);
        b._loaded = false;
        b.messagesReady = true;
        app.currentId = arg.sidA;
        app._touchTranscriptPane(arg.sidA);
        app._activateTabState(arg.sidA);
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        app.__releaseTranscriptGate = release;
        app.__slowTranscriptStarted = false;
        app._reloadSessionCoalesced = async sid => {
          if (sid !== arg.sidB) return true;
          app.__slowTranscriptStarted = true;
          await gate;
          const st = app._ensureTabState(sid);
          st.messages = app._historyEnvelopes(sid, arg.messagesB);
          st.messageRange.visibleStart = 0;
          st.messageRange.visibleEnd = st.messages.length;
          st.messageRange.offset = 0;
          st.messageRange.total = st.messages.length;
          st._installedCanonicalCount = st.messages.length;
          st.messagesReady = true;
          st.messagesLoading = false;
          st._loaded = true;
          return true;
        };
        return true;
        """,
        payload,
    )
    page.wait_for_function(
        """marker => Array.from(document.querySelectorAll('.msg-pane')).some(
          node => getComputedStyle(node).display !== 'none'
            && node.getClientRects().length > 0
            && node.textContent.includes(marker))""",
        arg=marker_a,
    )

    page.evaluate(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          app.currentId = sid;
          app.__switchPromise = app.switchSession();
        }""",
        sid_b,
    )
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].__slowTranscriptStarted"
    )
    frames = page.evaluate(
        """async oldMarker => {
          const out = [];
          for (let i = 0; i < 12; i += 1) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const overlay = document.querySelector('.chat-transcript-loading-overlay');
            const empty = document.querySelector('.chat-empty');
            const visiblePanes = Array.from(document.querySelectorAll('.msg-pane'))
              .filter(node => getComputedStyle(node).display !== 'none'
                && node.getClientRects().length > 0);
            const app = document.querySelector('#app')._x_dataStack[0];
            out.push({
              currentId: app.currentId,
              phase: app._ensureTabState(app.currentId).transcriptLoadPhase,
              generation: app._ensureTabState(app.currentId).transcriptLoadGeneration,
              overlayVisible: !!overlay
                && getComputedStyle(overlay).display !== 'none'
                && overlay.getClientRects().length > 0,
              overlayBackground: overlay ? getComputedStyle(overlay).backgroundColor : '',
              emptyVisible: !!empty
                && getComputedStyle(empty).display !== 'none'
                && empty.getClientRects().length > 0,
              oldVisible: visiblePanes.some(node => node.textContent.includes(oldMarker)),
            });
          }
          return out;
        }""",
        marker_a,
    )
    hidden_frames = [row for row in frames if not row["overlayVisible"]]
    assert not hidden_frames, hidden_frames
    assert all(row["overlayBackground"] not in ("", "rgba(0, 0, 0, 0)") for row in frames)
    assert not any(row["emptyVisible"] for row in frames), frames
    assert not any(row["oldVisible"] for row in frames), frames

    page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          app.__releaseTranscriptGate();
          await app.__switchPromise;
        }"""
    )
    expect(page.locator(".chat-transcript-loading-overlay")).to_be_hidden(timeout=5000)
    expect(page.locator(".msg-pane:visible")).to_contain_text(marker_b, timeout=5000)
    expect(page.locator(".chat-empty")).to_be_hidden()
    _assert_no_browser_errors(page, errors)


def test_mobile_transcript_loader_reuses_workspace_switch_status(
    page: Page, backend_url: str, auth_token: str,
):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    geometry = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId || 'mobile-muse-loader';
          if (!app.sessions.some(row => row.id === sid)) {
            app.sessions = [{id: sid, name: 'Muse loader', message_count: 1}];
          }
          if (!app.openTabIds.includes(sid)) app.openTabIds = [sid];
          const st = app._ensureTabState(sid);
          st._loaded = false;
          st.messagesReady = true;
          st.messagesLoading = false;
          st.transcriptLoadPhase = 'fetching';
          app.currentId = sid;
          await new Promise(resolve => app.$nextTick(resolve));
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const overlay = document.querySelector('.chat-transcript-loading-overlay');
          const loader = overlay.querySelector('.chat-transcript-loading-status');
          const spinner = loader.querySelector('.spinner-sm');
          const copy = loader.querySelector('span:last-child');
          const overlayRect = overlay.getBoundingClientRect();
          const loaderRect = loader.getBoundingClientRect();
          const result = {
            overlayVisible: getComputedStyle(overlay).display !== 'none',
            centerDeltaX: Math.abs(
              loaderRect.left + loaderRect.width / 2
              - (overlayRect.left + overlayRect.width / 2)),
            centerDeltaY: Math.abs(
              loaderRect.top + loaderRect.height / 2
              - (overlayRect.top + overlayRect.height / 2)),
            noOverflow: overlay.scrollWidth <= overlay.clientWidth,
            spinnerVisible: spinner.getClientRects().length > 0,
            reusesWorkspaceStatus: loader.classList.contains('workspace-switch-status'),
            copy: copy.textContent.trim(),
            expectedCopy: app.t('chat.loading_session'),
            skeletonCount: overlay.querySelectorAll('.chat-skeleton').length,
            srOnlyCount: overlay.querySelectorAll('.sr-only').length,
            heavyLoaderCount: overlay.querySelectorAll(
              '.chat-muse-loader, .chat-muse-loader-emblem, .chat-muse-loader-dots'
            ).length,
          };
          st.transcriptLoadPhase = 'idle';
          return result;
        }"""
    )
    assert geometry["overlayVisible"] is True
    assert geometry["centerDeltaX"] < 2
    assert geometry["centerDeltaY"] < 2
    assert geometry["noOverflow"] is True
    assert geometry["spinnerVisible"] is True
    assert geometry["reusesWorkspaceStatus"] is True
    assert geometry["copy"] == geometry["expectedCopy"]
    assert geometry["skeletonCount"] == 0
    assert geometry["srOnlyCount"] == 0
    assert geometry["heavyLoaderCount"] == 0
    expect(page.locator(".chat-transcript-loading-overlay")).to_be_hidden()
    _assert_no_browser_errors(page, errors)


def test_transcript_failure_keeps_resident_content_and_live_owner_cancels_shield(
    page: Page, backend_url: str, auth_token: str,
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "loading-shield-failure"
    marker = "RESIDENT_CONTENT_SURVIVES_LOAD_FAILURE"
    payload = {"sid": sid, "messages": _make_mixed_messages(4, marker)}
    result = page.evaluate(
        """async arg => {
        const app = document.querySelector("#app")._x_dataStack[0];
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app._scheduleIdlePreload = () => {};
        app.sessions = [{
          id: arg.sid, name: "Failure fixture",
          message_count: arg.messages.length, updated_at: 2,
        }];
        app.openTabIds = [arg.sid];
        app.tabState = {};
        const st = app._ensureTabState(arg.sid);
        st.messages = app._historyEnvelopes(arg.sid, arg.messages);
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        st._loaded = false;
        app.currentId = arg.sid;
        app._touchTranscriptPane(arg.sid);
        app._activateTabState(arg.sid);
        app._reloadSessionCoalesced = async () => false;
        const loaded = await app._ensureSessionLoaded(arg.sid);
        return {loaded, phase: st.transcriptLoadPhase};
        }""",
        payload,
    )
    assert result == {"loaded": False, "phase": "error"}
    expect(page.locator(".chat-load-error")).to_be_visible()
    expect(page.locator(".chat-load-error")).to_contain_text("Conversation failed to load")
    expect(page.locator(".msg-pane:visible")).to_contain_text(marker)
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0].transcriptLoadingVisible()"
    )
    expect(page.locator(".chat-transcript-loading-overlay")).to_be_hidden()

    live_takeover = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const token = app._beginTranscriptLoad(arg, st, "fetching");
        app._releaseTranscriptLoadForLive(st);
        const staleFailed = app._failTranscriptLoad(token);
        return {
          phase: st.transcriptLoadPhase,
          staleFailed,
          generation: st.transcriptLoadGeneration,
        };
        """,
        sid,
    )
    assert live_takeover["phase"] == "idle"
    assert live_takeover["staleFailed"] is False
    expect(page.locator(".chat-transcript-loading-overlay")).to_be_hidden()
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
          return app._ensureTabState(app.currentId).messagesReady === true
            && app._ensureTabState(app.currentId).messagesLoading === false
            && app._ensureTabState(app.currentId).messages.some(m => (m.text || "").includes("WINDOW_MSG_179"));
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
          ready: app._ensureTabState(app.currentId).messagesReady,
          bodyText: document.querySelector(".chat-body")?.textContent || "",
        };
        """,
        sid,
    )
    assert requests and requests[0]["tail"] == 20
    assert state["messages"] == 20
    assert state["visible"] <= 60
    assert state["loadedOffset"] == 160
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
            const body = app._chatBodyElement();
            app._ensureTabState(app.currentId).atBottom = false;
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
            """return app._ensureTabState(app.currentId).messages.some(m => (m.text || "").includes("WINDOW_MSG_000"));""",
        ):
            break

    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app._ensureTabState(app.currentId).messagesReady === true
            && app._ensureTabState(app.currentId).messages.some(m => (m.text || "").includes("WINDOW_MSG_000"));
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
          hasLater: app.hasLaterMessages(arg),
          cached: st.messages.length,
          ready: app._ensureTabState(app.currentId).messagesReady,
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
    assert final_state["hasLater"] is False
    latest_after_load_earlier = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          latestInMessages: st.messages.some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInLater: st.messages.slice(st.messageRange.visibleEnd)
            .some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInDom: document.querySelector(".chat-body")?.textContent.includes("WINDOW_MSG_179"),
          hasLater: app.hasLaterMessages(arg),
          ready: st.messagesReady,
        };
        """,
        sid,
    )
    assert latest_after_load_earlier == {
        "latestInMessages": True,
        "latestInLater": False,
        "latestInDom": True,
        "hasLater": False,
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
          return app._ensureTabState(app.currentId).messagesReady && !app._ensureTabState(app.currentId).messagesLoading;
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
        const mounted = st.messages.find(m => m.uuid === "CROSS_PAGE_KEY-tu-170");
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
        const mounted = all.find(m => m.uuid === "CROSS_PAGE_KEY-tu-170");
        const olderUser = all.find(m => m.uuid === "CROSS_PAGE_KEY-u-144");
        const olderAssistant = all.find(m => m.uuid === "CROSS_PAGE_KEY-a-145");
        const olderTool = all.find(m => m.uuid === "CROSS_PAGE_KEY-tr-147");
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

    assert any(request["offset"] < 160 for request in requests[1:]), requests
    assert after["sameMountedObject"] is True
    assert after["mountedKey"] == before["key"] == f"{sid}:uuid:CROSS_PAGE_KEY-tu-170"
    assert after["olderUserKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-u-144"
    assert after["olderAssistantKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-a-145"
    assert after["olderToolKey"] == f"{sid}:uuid:CROSS_PAGE_KEY-tr-147"
    assert after["allNonempty"] is True
    assert after["unique"] is True
    _assert_no_browser_errors(page, errors)


def test_message_outline_traps_focus_and_supports_keyboard_selection(
    page: Page, backend_url, auth_token,
):
    """Outline stays lazy, shows its local fallback, and preserves keyboard UX."""
    errors = _capture_browser_errors(page)
    requests: list[str] = []

    page.add_init_script(
        """
        (() => {
          const nativeFetch = window.fetch;
          window.__outlineFetchCalls = 0;
          window.fetch = function(input, options) {
            const url = String(typeof input === "string" ? input : input?.url || "");
            if (!url.includes("/outline")) {
              return nativeFetch.apply(this, arguments);
            }
            window.__outlineFetchCalls += 1;
            const receiver = this;
            const args = arguments;
            return new Promise(resolve => {
              window.__releaseOutlineFetch = () => {
                resolve(nativeFetch.apply(receiver, args));
              };
            });
          };
        })();
        """
    )

    def serve_outline(route):
        requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "outline": [
                    {
                        "uuid": "outline-first",
                        "preview": "First keyboard prompt",
                    },
                    {
                        "uuid": "outline-second",
                        "preview": "Second keyboard prompt",
                    },
                ],
            }),
        )

    page.route(
        "**/api/chat/sessions/*/outline",
        serve_outline,
    )
    _login(page, backend_url, auth_token)
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.__outlineFetchCalls") == 0
    assert requests == []

    _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        st.atBottom = false;
        st._backendOutline = [];
        st._outlineFetchedAt = 0;
        st._outlineFetching = false;
        st.messages.splice(0, st.messages.length,
          {
            role: "user", uuid: "outline-first",
            text: "Immediate local first",
          },
          {
            role: "user", uuid: "outline-second",
            text: "Immediate local second",
          },
        );
        app._scrollToUserMsg = message => {
          window.__outlineKeyboardSelection = message.uuid;
        };
        """,
    )

    opener = page.locator(".chat-outline-fab:visible")
    expect(opener).to_be_visible()
    opener.focus()
    opener.click()

    dialog = page.locator(".msg-outline-panel")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("role", "dialog")
    expect(dialog).to_have_attribute("aria-modal", "true")
    expect(dialog).to_have_attribute("aria-labelledby", "msg-outline-title")
    expect(dialog.locator("#msg-outline-title")).to_contain_text("(2)")
    items = dialog.locator(".msg-outline-item")
    expect(items).to_have_count(2)
    expect(items.nth(0)).to_contain_text("Immediate local first")
    expect(items.nth(0)).to_be_focused()
    assert page.evaluate("() => window.__outlineFetchCalls") == 1
    assert requests == []

    page.evaluate("() => window.__releaseOutlineFetch()")
    expect(items.nth(0)).to_contain_text("First keyboard prompt")
    page.wait_for_function(
        """() => !document.querySelector("#app")._x_dataStack[0]
          ._ensureTabState(document.querySelector("#app")._x_dataStack[0].currentId)
          ._outlineFetching"""
    )
    assert len(requests) == 1

    # The last outline item wraps forward to the close button, while reverse
    # traversal from the first DOM control wraps back to the last item.
    page.keyboard.press("Tab")
    expect(items.nth(1)).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.locator(".msg-outline-close")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(items.nth(1)).to_be_focused()

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()

    # Native button activation covers Enter/Space without custom key handlers.
    opener.press("Enter")
    expect(items.nth(0)).to_be_focused()
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.__outlineFetchCalls") == 1
    assert len(requests) == 1
    items.nth(1).focus()
    page.keyboard.press("Space")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    assert page.evaluate("() => window.__outlineKeyboardSelection") == "outline-second"
    assert len(requests) == 1
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize("viewport", [
    {"width": 738, "height": 828},
    {"width": 390, "height": 844},
])
def test_queued_message_avoids_visible_tail_navigation_fabs(
    page: Page, backend_url, auth_token, viewport,
):
    """Queue edit/remove controls stay clear of the three tail FABs."""
    page.set_viewport_size(viewport)
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        st.pendingQueue.splice(0, st.pendingQueue.length, {
          id: "q-tail-nav-overlap",
          text: "queued message",
          displayText: "queued message",
          pendingQuotes: [], images: [], docs: [],
        });
        st.atBottom = false;
        """,
    )

    queued = page.locator(".queued-row:visible")
    expect(queued).to_be_visible()
    expect(queued).to_have_class(re.compile(r"\bavoids-tail-nav\b"))
    for selector in (
        ".jump-bottom:visible",
        ".chat-outline-fab:visible",
        ".chat-prevuser-fab:visible",
    ):
        expect(page.locator(selector)).to_be_visible()

    geometry = queued.locator(".queued-bubble").evaluate(
        """bubble => {
          const bubbleRect = bubble.getBoundingClientRect();
          const selectors = ['.jump-bottom', '.chat-outline-fab', '.chat-prevuser-fab'];
          return selectors.map(selector => {
            const node = Array.from(document.querySelectorAll(selector))
              .find(el => el.getClientRects().length);
            const rect = node.getBoundingClientRect();
            const overlaps = !(
              bubbleRect.right <= rect.left || bubbleRect.left >= rect.right
              || bubbleRect.bottom <= rect.top || bubbleRect.top >= rect.bottom
            );
            return {selector, overlaps};
          });
        }"""
    )
    assert not any(item["overlaps"] for item in geometry), geometry

    _app_eval(
        page,
        """
        const st = app._ensureTabState(app.currentId);
        st.atBottom = true;
        if (st.messageRange) {
          st.messageRange.total = st.messageRange.offset + st.messages.length;
        }
        """,
    )
    expect(queued).not_to_have_class(re.compile(r"\bavoids-tail-nav\b"))
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
          hasLater: app.hasLaterMessages(arg),
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
    assert around_state["hasLater"] is True
    assert len([call for call in calls if "around_uuid" in call]) == 2
    assert len([call for call in calls if "tail" in call]) == 1

    returned = _app_eval(page, "return app.returnToLatest(arg);", sid)
    assert returned is True
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return st.messageRange.order === 'normal' && !app.hasLaterMessages(app.currentId)
            && st.messages.some(m => (m.text || '').includes('CANONICAL_LATEST_VISIBLE'));
        }""",
        timeout=10000,
    )
    assert len([call for call in calls if "tail" in call]) == 2
    tail_state = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {resident: st.messages.length, cap: app._liveMessageDomCap()};
        """,
        sid,
    )
    assert tail_state["resident"] == len(latest_messages)
    assert page.locator(".msg-pane:visible .msg").count() == min(
        len(latest_messages), tail_state["cap"]
    )
    _assert_no_browser_errors(page, errors)


def test_resident_history_uses_exact_layout_and_native_scroll_anchor(
    page: Page, backend_url, auth_token,
):
    """Resident rows use exact browser layout and native scroll anchoring."""
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
        // Later-history availability is derived from resident server coordinates.
        st.messagesReady = true;
        st.messagesLoading = false;
        st.atBottom = true;
        app.currentId = arg;
        app._activateTabState(arg);
        app.mobileTab = "chat";
        return true;
        """,
        sid,
    )
    page.wait_for_function(
        """() => {
          const pane = document.querySelector('.msg-pane');
          return pane && pane.textContent.includes('VIRTUAL_MESSAGE_599')
            && pane.querySelectorAll('.msg').length === 600;
        }""",
        timeout=10000,
    )
    initial = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const pane = document.querySelector('.msg-pane');
        return { canonical: st.messages.length, normalized: st.messages.length,
          mounted: pane.querySelectorAll('.msg').length,
          spacers: pane.querySelectorAll('.msg-virtual-spacer').length };
        """,
        sid,
    )
    assert initial["canonical"] == initial["normalized"] == 600
    assert initial["mounted"] == 600
    assert initial["spacers"] == 0

    _app_eval(
        page,
        """
        const body = app._chatBodyElement();
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
          const body = app._chatBodyElement();
          const rows = Array.from(document.querySelectorAll('.msg-pane .msg'));
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
          app._chatBodyElement().scrollTop += 500;
          app._syncMessageViewport(app.currentId);
        }"""
    )
    page.wait_for_timeout(100)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._chatBodyElement().scrollTop -= 500;
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
            '.msg-pane .msg').length, top: row?.getBoundingClientRect().top || 0};
        }""",
        anchor["key"],
    )
    assert shifted["canonical"] == 600
    assert shifted["mounted"] == 600
    assert abs(shifted["top"] - anchor["top"]) < 3

    streaming = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        st.streaming = true;
        st.atBottom = false;
        app._ensureTabState(app.currentId).atBottom = false;
        const body = app._chatBodyElement();
        body.scrollTop = 0;
        app._syncMessageViewport(arg);
        return new Promise(resolve => app.$nextTick(() => resolve({
          tailMounted: !!document.querySelector(
            '.msg[data-message-key="virtual-key-599"]'),
          mounted: document.querySelectorAll('.msg-pane .msg').length,
          canonical: st.messages.length,
        })));
        """,
        sid,
    )
    # Exact resident layout keeps every loaded row mounted while native scrolling
    # preserves the reader position; network paging bounds how many rows are resident.
    assert streaming["tailMounted"] is True
    assert streaming["mounted"] == 600
    assert streaming["canonical"] == 600

    # Legacy estimated-height virtual-window bookkeeping no longer controls DOM rows.
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
          return app._ensureTabState(app.currentId).streaming === true && app._ensureTabState(app.currentId).messagesReady === true
            && app._ensureTabState(app.currentId).messages.some(m => (m.text || "").includes("ACTIVE_RECONNECT_USER"));
        }""",
        timeout=10000,
    )
    assert active_requests, "loadSession did not call /active"
    assert ticket_requests and ticket_requests[-1]["prompt"] == ""
    assert ticket_requests[-1]["session_id"] == sid
    assert ticket_requests[-1]["turn_id"] == "active-turn-1"
    assert ticket_requests[-1]["mobile"] is True

    transport_open = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return {
            phase: st.streamPhase,
            assistantCount: st.messages.filter(m => m.role === "assistant").length,
            footer: document.querySelector(".turn-pending-footer")?.textContent || "",
            expectedRuntime: app.t("chat.startup_runtime"),
            streaming: st.streaming,
          };
        }"""
    )
    assert transport_open["phase"] == "connecting"
    assert transport_open["assistantCount"] == 0
    assert transport_open["streaming"] is True
    assert transport_open["expectedRuntime"] in transport_open["footer"]

    for event_seq, phase, label_key in [
        (1, "accepted", "chat.startup_runtime"),
        (2, "runtime", "chat.startup_runtime"),
        (3, "tools", "chat.startup_tools"),
        (4, "context", "chat.startup_context"),
    ]:
        page.evaluate(
            """({ phase, eventSeq }) => window.__emitSse("startup", {
              phase, turn_id: "active-turn-1", event_seq: eventSeq,
            })""",
            {"phase": phase, "eventSeq": event_seq},
        )
        page.wait_for_function(
            """({ phase, labelKey }) => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const st = app._ensureTabState(app.currentId);
              const footer = document.querySelector(".turn-pending-footer")?.textContent || "";
              return st.streamPhase === phase && footer.includes(app.t(labelKey))
                && st.messages.filter(m => m.role === "assistant").length === 0;
            }""",
            arg={"phase": phase, "labelKey": label_key},
            timeout=5000,
        )

    page.evaluate(
        """() => {
          window.__emitSse("text", {
            text: "ACTIVE_RECONNECT_LIVE_VISIBLE",
            turn_id: "active-turn-1", event_seq: 5,
          });
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const body = document.querySelector(".chat-body")?.textContent || "";
          const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming === true
            && app._ensureTabState(app.currentId).streamPhase === "running"
            && app._ensureTabState(app.currentId).messagesReady === true
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
            turn_id: "active-turn-1", event_seq: 6,
          });
        }"""
    )
    page.wait_for_function(
        """() => document.querySelector("#app")._x_dataStack[0].activeSessionPane().streaming === false""",
        timeout=10000,
    )
    expect(page.locator(".msg-pane:visible .msg.assistant").last).to_contain_text(
        "ACTIVE_RECONNECT_LIVE_VISIBLE", timeout=5000
    )
    assert _app_eval(page, "return app._ensureTabState(app.currentId).messagesReady === true && !app._ensureTabState(app.currentId).messagesLoading;") is True
    _assert_no_browser_errors(page, errors)


def test_active_turn_adoption_uses_tail_or_bounded_running_suffix(
    page: Page, backend_url, auth_token,
):
    """Older repeats stay unclaimed; steering can follow the active root."""
    _login(page, backend_url, auth_token)
    result = _app_eval(
        page,
        """
        const makeState = (sid, messages) => {
          const st = app._blankTabState();
          st._sid = sid;
          st.messages = messages;
          st.messageRange.visibleStart = 0;
          st.messageRange.visibleEnd = messages.length;
          st.messageRange.total = messages.length;
          app.tabState[sid] = st;
          return st;
        };

        const repeated = makeState("active-repeat", [
          { role: "user", text: "继续" },
          { role: "assistant", text: "旧回复" },
        ]);
        const repeatedResult = app._installActiveTurnUser(
          repeated, "turn-repeat", "继续", [], [],
        );

        const attachmentOnly = makeState("active-attachment", [
          { role: "user", text: "", images: [
            { mime: "image/png", url: "/old.png" },
          ], docs: [] },
        ]);
        const attachmentResult = app._installActiveTurnUser(
          attachmentOnly, "turn-attachment", "", [
            { mime: "image/png", url: "/new.png" },
          ], [],
        );

        const exactTail = makeState("active-exact-tail", [
          { role: "user", text: "", images: [
            { url: "/same.png", mime: "image/png" },
          ], docs: [{ kind: "text", name: "notes.md" }] },
        ]);
        const exactResult = app._installActiveTurnUser(
          exactTail, "turn-exact", "", [
            { mime: "image/png", url: "/same.png" },
          ], [{ name: "notes.md", kind: "text" }],
        );

        const midturn = makeState("active-midturn", [
          { role: "assistant", text: "previous reply", turn_status: "completed" },
          { role: "user", text: "original active prompt" },
          { role: "tool_result", text: "tool result" },
          { role: "user", text: "mid-turn adjustment", uuid: "steering-command" },
          { role: "assistant", text: "active reply", turn_status: "running" },
        ]);
        const midturnResult = app._installActiveTurnUser(
          midturn, "turn-midturn", "original active prompt", [], [],
        );

        return {
          repeated: {
            appended: repeatedResult.appended,
            length: repeated.messages.length,
            oldTurnId: repeated.messages[0]._turnId || "",
            tailTurnId: repeated.messages.at(-1)._turnId || "",
          },
          attachment: {
            appended: attachmentResult.appended,
            length: attachmentOnly.messages.length,
            oldTurnId: attachmentOnly.messages[0]._turnId || "",
            tailTurnId: attachmentOnly.messages.at(-1)._turnId || "",
          },
          exact: {
            appended: exactResult.appended,
            length: exactTail.messages.length,
            tailTurnId: exactTail.messages.at(-1)._turnId || "",
          },
          midturn: {
            appended: midturnResult.appended,
            length: midturn.messages.length,
            rootTurnId: midturn.messages[1]._turnId || "",
            rootMarked: midturn.messages[1]._turnRoot === true,
            adjustmentTurnId: midturn.messages[3]._turnId || "",
          },
        };
        """,
    )
    assert result["repeated"] == {
        "appended": True,
        "length": 3,
        "oldTurnId": "",
        "tailTurnId": "turn-repeat",
    }
    assert result["attachment"] == {
        "appended": True,
        "length": 2,
        "oldTurnId": "",
        "tailTurnId": "turn-attachment",
    }
    assert result["exact"] == {
        "appended": False,
        "length": 1,
        "tailTurnId": "turn-exact",
    }
    assert result["midturn"] == {
        "appended": False,
        "length": 5,
        "rootTurnId": "turn-midturn",
        "rootMarked": True,
        "adjustmentTurnId": "",
    }


def test_session_sync_deadline_dispose_and_hidden_resume(
    page: Page, backend_url, auth_token,
):
    """A stuck request releases the coordinator; hidden polling resumes promptly."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    result = _app_eval(
        page,
        """
        return (async () => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const makeState = (sid) => {
            const st = app._blankTabState();
            st._sid = sid;
            app.tabState[sid] = st;
            return st;
          };

          app._abortActivityFetches();
          await sleep(0);
          const originalFetch = window.fetch;
          const originalRequestDeadline = app.REQUEST_DEADLINE_MS;
          app.REQUEST_DEADLINE_MS = 35;
          window.fetch = () => new Promise(() => {});
          const activityStarted = performance.now();
          const activityResult = await app.fetchActivity();
          const activityElapsed = performance.now() - activityStarted;
          const activityReleased = activityResult === false
            && !app._activityFetchPromises.events
            && !app._activityFetchControllers.events;
          window.fetch = originalFetch;
          app.REQUEST_DEADLINE_MS = originalRequestDeadline;

          const deadlineState = makeState("sync-never-resolving");
          const deadlineResult = await app._requestSessionSync(
            "sync-never-resolving", "transport_retry", {
              deadlineMs: 40,
              run: () => new Promise(() => {}),
            },
          );
          const coordinatorReleased = deadlineResult === false
            && deadlineState.sessionSync.inFlight === null;

          const disposeState = makeState("sync-dispose");
          let abortObserved = false;
          const disposePromise = app._requestSessionSync(
            "sync-dispose", "transport_retry", {
              deadlineMs: 1000,
              run: signal => new Promise((resolve) => {
                const onAbort = () => {
                  abortObserved = true;
                  resolve("aborted");
                };
                if (signal.aborted) onAbort();
                else signal.addEventListener("abort", onAbort, { once: true });
              }),
            },
          );
          for (let i = 0; i < 20 && !disposeState.sessionSync.inFlight; i += 1) {
            await sleep(5);
          }
          const disposeWasInFlight = !!disposeState.sessionSync.inFlight;
          app._disposeSessionSync(disposeState);
          const disposeResult = await Promise.race([
            disposePromise,
            sleep(250).then(() => "stuck"),
          ]);

          const descriptor = Object.getOwnPropertyDescriptor(
            document, "visibilityState",
          );
          let visibility = "hidden";
          Object.defineProperty(document, "visibilityState", {
            configurable: true,
            get: () => visibility,
          });
          const hiddenState = makeState("sync-hidden");
          let hiddenRuns = 0;
          const hiddenPromise = app._requestSessionSync(
            "sync-hidden", "transport_retry", {
              deadlineMs: 500,
              run: () => { hiddenRuns += 1; return true; },
            },
          );
          await sleep(80);
          const hiddenHeld = hiddenRuns === 0
            && !!hiddenState.sessionSync.pending.transport_retry;
          visibility = "visible";
          app._resumeVisibleSessionSync();
          const resumed = await Promise.race([
            hiddenPromise,
            sleep(500).then(() => "stuck"),
          ]);
          app._disposeSessionSync(hiddenState);
          if (descriptor) {
            Object.defineProperty(document, "visibilityState", descriptor);
          } else {
            delete document.visibilityState;
          }

          const originalRandom = Math.random;
          Math.random = () => 0;
          const retryDelays = [1, 2, 3].map(attempt => app._retryDelay(attempt));
          const previousActivityFailures = app._activityLiveFailures;
          app._activityLiveFailures = 0;
          const activityDelays = [
            app._activityReconnectDelay(),
            app._activityReconnectDelay(),
            app._activityReconnectDelay(),
          ];
          app._activityLiveFailures = previousActivityFailures;
          Math.random = originalRandom;

          return {
            activityReleased,
            activityElapsed,
            coordinatorReleased,
            disposeWasInFlight,
            abortObserved,
            disposeSettled: disposeResult !== "stuck",
            hiddenHeld,
            hiddenRuns,
            resumed,
            retryDelays,
            activityDelays,
          };
        })();
        """,
    )
    assert result["activityReleased"] is True
    assert 25 <= result["activityElapsed"] < 500
    assert result["coordinatorReleased"] is True
    assert result["disposeWasInFlight"] is True
    assert result["abortObserved"] is True
    assert result["disposeSettled"] is True
    assert result["hiddenHeld"] is True
    assert result["hiddenRuns"] == 1
    assert result["resumed"] is True
    assert result["retryDelays"] == [800, 1600, 3200]
    assert result["activityDelays"] == [1000, 2000, 4000]
    _assert_no_browser_errors(page, errors)


def test_existing_fifo_queue_renders_pending_send_as_disabled_tail_card(
    page: Page, backend_url, auth_token,
):
    """A known FIFO send stays at the visual queue tail while POST is pending."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "perf-optimistic-queue-tail"
    prompt = "THIRD_QUEUE_ITEM_PENDING_POST"

    def queue_item(item_id: str, text: str, enqueued_at: int) -> dict:
        return {
            "id": item_id,
            "text": text,
            "display_text": text,
            "selection_quotes": [],
            "image_ids": "",
            "attachments": [],
            "delivery": "queue",
            "steering_state": "queued",
            "command_uuid": f"{item_id}-command",
            "target_turn_id": "existing-running-turn",
            "enqueued_at": enqueued_at,
        }

    first = queue_item("queue-tail-first", "FIRST_QUEUE_ITEM", 1)
    second = queue_item("queue-tail-second", "SECOND_QUEUE_ITEM", 2)
    accepted = queue_item("queue-tail-third", prompt, 3)
    authoritative = [first, second, accepted]
    held_post: dict[str, object] = {}
    post_payloads: list[dict] = []

    def queue_route(route):
        if route.request.method == "POST":
            post_payloads.append(route.request.post_data_json)
            held_post["route"] = route
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": authoritative, "paused": False, "revision": 3}),
        )

    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        const queueView = item => ({
          id: item.id,
          text: item.text,
          displayText: item.display_text,
          pendingQuotes: [], image_ids: "", hasAttach: false,
          images: [], docs: [], expiredCount: 0,
          pendingImages: [], pendingDocs: [],
          delivery: "queue", deliveryStatus: "queued",
          commandUuid: item.command_uuid,
          targetTurnId: item.target_turn_id,
          enqueuedAt: item.enqueued_at,
        });
        app.refreshSessions = async () => {};
        app._pullSessionList = async () => false;
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._syncQueueFromServer = async () => {};
        app.appReady = true;
        app._modelsLoaded = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.lang = "zh";
        app.busySendMode = "adjust";
        app.sessions = [{
          id: sid, name: "Optimistic queue tail", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
          active: true, turn_active: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.messages = [{
          role: "assistant", text: "RUNNING_ASSISTANT_BEFORE_QUEUE",
          html: "<p>RUNNING_ASSISTANT_BEFORE_QUEUE</p>",
          uuid: "queue-tail-running-assistant",
          _k: `${sid}:uuid:queue-tail-running-assistant`, _noAnim: true,
        }];
        Object.assign(st.messageRange, {
          visibleStart: 0, visibleEnd: 1, offset: 0, total: 1,
          preTotal: 0, order: "full", generation: "queue-tail-e2e",
        });
        st.messagesReady = true;
        st.messagesLoading = false;
        st.streaming = true;
        st.activeTurnId = "existing-running-turn";
        st._streamOwnerToken = "existing-running-owner";
        st.pendingQueue = arg.initial.map(queueView);
        app.currentId = sid;
        app.mobileTab = "chat";
        app._activateTabState(sid);
        st.atBottom = true;
        app.input = arg.prompt;
        window.__optimisticQueueTailSend = app.send();
        return true;
        """,
        {"sid": sid, "prompt": prompt, "initial": [first, second]},
    )

    page.wait_for_function(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          return st._queueAdmission?.displayText === arg.prompt
            && document.querySelectorAll(".msg.user.queued").length === 3;
        }""",
        arg={"sid": sid, "prompt": prompt},
        timeout=10000,
    )
    assert "route" in held_post, "send() did not reach the delayed queue POST"
    assert len(post_payloads) == 1
    assert post_payloads[0]["text"] == prompt
    assert post_payloads[0]["delivery"] == "queue"

    queued = page.locator(".msg.user.queued")
    expect(queued).to_have_count(3)
    assert queued.locator(".queued-text").all_text_contents() == [
        "FIRST_QUEUE_ITEM", "SECOND_QUEUE_ITEM", prompt,
    ]
    assert queued.locator(".queued-label").all_text_contents() == [
        "排队中 1 / 3", "排队中 2 / 3", "排队中 3 / 3",
    ]
    expect(
        page.locator(f'.msg-pane[data-tid="{sid}"] .msg.user').filter(
            has_text=prompt
        )
    ).to_have_count(0)
    pending_actions = queued.nth(2).locator("button.queued-act")
    expect(pending_actions).to_have_count(2)
    expect(pending_actions.nth(0)).to_be_disabled()
    expect(pending_actions.nth(1)).to_be_disabled()

    held_post["route"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "ok": True,
            "item": accepted,
            "effective_delivery": "queue",
            "delivery_status": "queued",
            "queue": {"items": authoritative, "revision": 3},
        }),
    )
    send_result = _app_eval(
        page, "return await window.__optimisticQueueTailSend;"
    )
    assert send_result is True
    page.wait_for_function(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          return st._queueAdmission === null
            && st.pendingQueue.length === 3
            && document.querySelectorAll(".msg.user.queued").length === 3;
        }""",
        arg={"sid": sid},
        timeout=10000,
    )
    settled = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg.sid);
        return {
          admission: st._queueAdmission,
          ids: st.pendingQueue.map(item => item.id),
          texts: st.pendingQueue.map(item => item.displayText),
          transcriptPromptCount: st.messages.filter(
            item => item.role === "user" && item.displayText === arg.prompt).length,
        };
        """,
        {"sid": sid, "prompt": prompt},
    )
    assert settled == {
        "admission": None,
        "ids": [first["id"], second["id"], accepted["id"]],
        "texts": [first["display_text"], second["display_text"], prompt],
        "transcriptPromptCount": 0,
    }
    expect(queued).to_have_count(3)
    assert queued.locator(".queued-text").all_text_contents() == [
        "FIRST_QUEUE_ITEM", "SECOND_QUEUE_ITEM", prompt,
    ]
    assert queued.locator(".queued-label").all_text_contents() == [
        "排队中 1 / 3", "排队中 2 / 3", "排队中 3 / 3",
    ]
    _assert_no_browser_errors(page, errors)


def test_existing_same_turn_adjust_keeps_next_real_send_as_adjust(
    page: Page, backend_url, auth_token,
):
    """A waiting adjustment must not downgrade the next same-turn send to FIFO."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "perf-repeated-midturn-adjust"
    turn_id = "repeated-midturn-active-turn"
    first_prompt = "FIRST_ADJUSTMENT_WAITING_FOR_TOOL"
    second_prompt = "SECOND_ADJUSTMENT_MUST_STAY_NATIVE"
    first = {
        "id": "repeated-adjust-first",
        "text": first_prompt,
        "display_text": first_prompt,
        "selection_quotes": [],
        "image_ids": "",
        "attachments": [],
        "delivery": "adjust",
        "steering_state": "waiting_tool",
        "command_uuid": "repeated-adjust-first-command",
        "target_turn_id": turn_id,
        "enqueued_at": 1,
    }
    accepted = {
        "id": "repeated-adjust-second",
        "text": second_prompt,
        "display_text": second_prompt,
        "selection_quotes": [],
        "image_ids": "",
        "attachments": [],
        "delivery": "adjust",
        "steering_state": "waiting_tool",
        "command_uuid": "repeated-adjust-second-command",
        "target_turn_id": turn_id,
        "enqueued_at": 2,
    }
    post_payloads: list[dict] = []

    def queue_route(route):
        if route.request.method == "POST":
            post_payloads.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "item": accepted,
                    "effective_delivery": "adjust",
                    "delivery_status": "waiting_tool",
                    "queue": {
                        "items": [first, accepted],
                        "paused": False,
                        "revision": 2,
                    },
                }),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "items": [first, accepted], "paused": False, "revision": 2,
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    result = _app_eval(
        page,
        """
        const sid = arg.sid;
        const turnId = arg.turnId;
        app.refreshSessions = async () => {};
        app._pullSessionList = async () => false;
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._syncQueueFromServer = async () => {};
        app.appReady = true;
        app._modelsLoaded = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.permission = "bypassPermissions";
        app.busySendMode = "adjust";
        app.sessions = [{
          id: sid, name: "Repeated mid-turn adjust", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
          active: true, turn_active: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.messagesReady = true;
        st.messagesLoading = false;
        st.streaming = true;
        st.activeTurnId = turnId;
        st._streamOwnerToken = "repeated-midturn-owner";
        st.pendingQueue = [{
          id: arg.first.id,
          text: arg.first.text,
          displayText: arg.first.display_text,
          pendingQuotes: [], image_ids: "", hasAttach: false,
          images: [], docs: [], expiredCount: 0,
          pendingImages: [], pendingDocs: [],
          delivery: "adjust", deliveryStatus: "waiting_tool",
          commandUuid: arg.first.command_uuid,
          targetTurnId: turnId,
          enqueuedAt: arg.first.enqueued_at,
        }];
        app.currentId = sid;
        app.mobileTab = "chat";
        app._activateTabState(sid);
        st.atBottom = true;

        // A native adjustment from another immutable turn is still an ordering
        // barrier; only the exact current-turn row may admit another adjustment.
        st.pendingQueue[0].targetTurnId = "different-active-turn";
        const differentTurnDelivery = app._busySendDelivery(sid, turnId, false);
        st.pendingQueue[0].targetTurnId = turnId;

        app.input = arg.secondPrompt;
        const sent = await app.send();
        return {
          sent,
          differentTurnDelivery,
          pending: st.pendingQueue.map(item => ({
            id: item.id,
            delivery: item.delivery,
            deliveryStatus: item.deliveryStatus,
            targetTurnId: item.targetTurnId,
          })),
        };
        """,
        {
            "sid": sid,
            "turnId": turn_id,
            "first": first,
            "secondPrompt": second_prompt,
        },
    )

    assert result["sent"] is True
    assert result["differentTurnDelivery"] == "queue"
    assert len(post_payloads) == 1
    assert post_payloads[0]["text"] == second_prompt
    assert post_payloads[0]["delivery"] == "adjust"
    assert post_payloads[0]["active_turn_id"] == turn_id
    assert result["pending"] == [
        {
            "id": first["id"],
            "delivery": "adjust",
            "deliveryStatus": "waiting_tool",
            "targetTurnId": turn_id,
        },
        {
            "id": accepted["id"],
            "delivery": "adjust",
            "deliveryStatus": "waiting_tool",
            "targetTurnId": turn_id,
        },
    ]
    _assert_no_browser_errors(page, errors)


def test_mux_pending_turn_busy_keeps_queue_admission_until_post_ack(
    page: Page, backend_url, auth_token,
):
    """A synchronously replayed mux busy frame must not lose its pending card."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    sid = "mux-pending-turn-busy-admission"
    prompt = "MUX_BUSY_QUEUE_POST_PENDING"
    attempted_turn_id = "mux-attempted-turn"
    busy_turn_id = "mux-existing-busy-turn"
    accepted = {
        "id": "mux-busy-queued-item",
        "text": prompt,
        "display_text": prompt,
        "selection_quotes": [],
        "image_ids": "",
        "attachments": [],
        "delivery": "queue",
        "steering_state": "queued",
        "command_uuid": "mux-busy-command",
        "target_turn_id": busy_turn_id,
        "enqueued_at": 1,
    }
    held_post: dict[str, object] = {}
    post_payloads: list[dict] = []
    start_payloads: list[dict] = []

    def start_route(route):
        start_payloads.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "accepted": True,
                "turn_id": attempted_turn_id,
                "started_at": 1,
            }),
        )

    def queue_route(route):
        if route.request.method == "POST":
            post_payloads.append(route.request.post_data_json)
            held_post["route"] = route
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": [accepted], "paused": False, "revision": 1}),
        )

    page.route("**/api/chat/turns/start", start_route)
    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._pullSessionList = async () => false;
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app._syncQueueFromServer = async () => {};
        app._awaitRuntimeSettingPatches = async () => true;
        app._confirmSessionBusy = async () => false;
        app._ensureChatMux = async () => {
          app._chatMuxConnected = true;
          return true;
        };
        app.appReady = true;
        app._modelsLoaded = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = "e2e-model";
        app.defaultModel = "e2e-model";
        app.permission = "bypassPermissions";
        app.lang = "zh";
        app.busySendMode = "queue";
        app.sessions = [{
          id: sid, name: "Mux pending busy", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
          active: false, turn_active: false,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.messagesReady = true;
        st.messagesLoading = false;
        st.pendingQueue = [];
        st.atBottom = true;
        app.currentId = sid;
        app._touchTranscriptPane(sid);
        app._activateTabState(sid);
        app.input = arg.prompt;

        // The aggregate mux received this terminal admission race before the
        // per-turn adapter installed its listeners. Activation replays it via
        // dispatchEvent synchronously; its async handler then waits on the
        // deliberately-held queue POST while send() reaches outer finally.
        app._queueChatMuxEvent(sid, "error", JSON.stringify({
          session_id: sid,
          turn_id: arg.attemptedTurnId,
          active_turn_id: arg.busyTurnId,
          error: "session already has an active turn",
          kind: "turn_busy",
          retryable: true,
          cta: "retry",
        }));
        window.__muxPendingBusySendSettled = false;
        window.__muxPendingBusySendResult = "pending";
        window.__muxPendingBusySend = app.send().then(result => {
          window.__muxPendingBusySendSettled = true;
          window.__muxPendingBusySendResult = result;
          return result;
        });
        return true;
        """,
        {
            "sid": sid,
            "prompt": prompt,
            "attemptedTurnId": attempted_turn_id,
            "busyTurnId": busy_turn_id,
        },
    )

    page.wait_for_function(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          return window.__muxPendingBusySendSettled === true
            && st._composerSubmitToken === null
            && st._queueAdmission?.displayText === arg.prompt
            && st.pendingQueue.length === 0
            && document.querySelectorAll(".msg.user.queued").length === 1;
        }""",
        arg={"sid": sid, "prompt": prompt},
        timeout=10000,
    )
    assert len(start_payloads) == 1
    assert "route" in held_post, "turn_busy handoff did not reach the delayed queue POST"
    assert len(post_payloads) == 1
    assert post_payloads[0]["text"] == prompt
    assert post_payloads[0]["delivery"] == "queue"
    assert post_payloads[0]["active_turn_id"] == busy_turn_id

    queued = page.locator(".msg.user.queued")
    expect(queued).to_have_count(1)
    expect(queued.locator(".queued-text")).to_have_text(prompt)
    expect(queued.locator(".queued-label")).to_have_text("排队中 1 / 1")
    expect(queued.locator("button.queued-act").nth(0)).to_be_disabled()
    expect(queued.locator("button.queued-act").nth(1)).to_be_disabled()
    expect(
        page.locator(f'.msg-pane[data-tid="{sid}"] .msg.user').filter(
            has_text=prompt
        )
    ).to_have_count(0)

    held_post["route"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({
            "ok": True,
            "item": accepted,
            "effective_delivery": "queue",
            "delivery_status": "queued",
            "queue": {"items": [accepted], "revision": 1},
        }),
    )
    page.wait_for_function(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          return st._queueAdmission === null
            && st.pendingQueue.length === 1
            && st.pendingQueue[0].id === arg.itemId
            && st.streaming === false
            && !st._busyQueueHandoff
            && document.querySelectorAll(".msg.user.queued").length === 1;
        }""",
        arg={"sid": sid, "itemId": accepted["id"]},
        timeout=10000,
    )
    settled = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg.sid);
        return {
          admission: st._queueAdmission,
          claim: st._composerSubmitToken,
          ids: st.pendingQueue.map(item => item.id),
          texts: st.pendingQueue.map(item => item.displayText),
          displayIds: app.queueDisplayItems(st).map(item => item.id),
          transcriptPromptCount: st.messages.filter(
            item => item.role === "user" && item.displayText === arg.prompt).length,
        };
        """,
        {"sid": sid, "prompt": prompt},
    )
    assert settled == {
        "admission": None,
        "claim": None,
        "ids": [accepted["id"]],
        "texts": [prompt],
        "displayIds": [accepted["id"]],
        "transcriptPromptCount": 0,
    }
    expect(queued).to_have_count(1)
    expect(queued.locator(".queued-text")).to_have_text(prompt)
    expect(queued.locator(".queued-label")).to_have_text("排队中 1 / 1")
    _assert_no_browser_errors(page, errors)


def test_admission_gap_resolves_exact_turn_before_busy_adjust_enqueue(
    page: Page, backend_url, auth_token,
):
    """A second send during first-turn admission must not freeze as FIFO."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "perf-busy-admission-gap"
    turn_id = "admitted-root-turn"
    item_id = "admission-gap-item"
    command_uuid = "admission-gap-command"
    active_calls: list[str] = []
    queue_payloads: list[dict] = []

    def active_route(route):
        active_calls.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "active": True,
                "background": False,
                "turn_id": turn_id,
            }),
        )

    def queue_route(route):
        payload = route.request.post_data_json
        queue_payloads.append(payload)
        item = {
            "id": item_id,
            "text": payload["text"],
            "display_text": payload["display_text"],
            "selection_quotes": [],
            "image_ids": "",
            "delivery": "adjust",
            "steering_state": "waiting_tool",
            "command_uuid": command_uuid,
            "target_turn_id": turn_id,
            "enqueued_at": int(time.time() * 1000),
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "item": item,
                "effective_delivery": "adjust",
                "delivery_status": "waiting_tool",
                "queue": {"items": [item], "revision": 1},
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}/active", active_route)
    page.route(f"**/api/chat/sessions/{sid}/queue", queue_route)
    result = _app_eval(
        page,
        """
        const sid = arg.sid;
        app.busySendMode = "adjust";
        app.sessions = [{
          id: sid, name: "Admission gap", model: "e2e-model",
          permission: "bypassPermissions", updated_at: 1,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.streaming = true;
        st._streamOwnerToken = "admitting-root-owner";
        st.streamPhase = "connecting";
        st.activeTurnId = "";
        st.pendingQueue = [];
        app.currentId = sid;
        app._activateTabState(sid);
        app._syncQueueFromServer = async () => {};
        const queued = await app._enqueueMessage(sid, {
          text: "ADJUST_DURING_ADMISSION",
          displayText: "ADJUST_DURING_ADMISSION",
          pendingImages: [], pendingDocs: [], pendingQuotes: [],
          permission: "bypassPermissions",
          delivery: "queue",
          active_turn_id: "",
          stream_owner_token: "admitting-root-owner",
        });
        return {
          queued,
          pending: st.pendingQueue.map(item => ({
            id: item.id,
            delivery: item.delivery,
            deliveryStatus: item.deliveryStatus,
            targetTurnId: item.targetTurnId,
          })),
        };
        """,
        {"sid": sid},
    )

    assert active_calls == ["GET"]
    assert len(queue_payloads) == 1
    assert queue_payloads[0]["delivery"] == "adjust"
    assert queue_payloads[0]["active_turn_id"] == turn_id
    assert result == {
        "queued": True,
        "pending": [{
            "id": item_id,
            "delivery": "adjust",
            "deliveryStatus": "waiting_tool",
            "targetTurnId": turn_id,
        }],
    }
    _assert_no_browser_errors(page, errors)


def test_admission_gap_probe_cannot_steer_successor_turn(
    page: Page, backend_url, auth_token,
):
    """A delayed admission probe is bound to its original local stream."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    sid = "perf-busy-admission-successor"
    result = _app_eval(
        page,
        """
        const sid = arg.sid;
        app.busySendMode = "adjust";
        app.sessions = [{
          id: sid, name: "Admission successor", model: "e2e-model",
          permission: "bypassPermissions", updated_at: 1,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st.streaming = true;
        st._streamOwnerToken = "root-a-owner";
        st.streamPhase = "connecting";
        st.activeTurnId = "";
        st.pendingQueue = [];
        app.currentId = sid;
        app._activateTabState(sid);
        app._syncQueueFromServer = async () => {};

        const originalFetch = window.fetch;
        let activeCalls = 0;
        const queuePayloads = [];
        window.fetch = async (url, options = {}) => {
          const path = String(url);
          if (path.endsWith("/active")) {
            activeCalls += 1;
            await new Promise(resolve => setTimeout(resolve, 40));
            return new Response(JSON.stringify({
              active: true,
              background: false,
              turn_id: "successor-turn-b",
            }), { status: 200, headers: { "Content-Type": "application/json" } });
          }
          if (path.endsWith("/queue")) {
            const queuePayload = JSON.parse(options.body || "{}");
            queuePayloads.push(queuePayload);
            const item = {
              id: `successor-safe-queue-item-${queuePayloads.length}`,
              text: queuePayload.text,
              display_text: queuePayload.display_text,
              selection_quotes: [],
              image_ids: "",
              delivery: "queue",
              steering_state: "",
              command_uuid: "",
              target_turn_id: "",
              enqueued_at: Date.now(),
            };
            return new Response(JSON.stringify({
              ok: true,
              item,
              effective_delivery: "queue",
              delivery_status: "queued",
              queue: { items: [item], revision: 1 },
            }), { status: 200, headers: { "Content-Type": "application/json" } });
          }
          return originalFetch(url, options);
        };
        try {
          const enqueue = app._enqueueMessage(sid, {
            text: "MUST_NOT_STEER_SUCCESSOR",
            displayText: "MUST_NOT_STEER_SUCCESSOR",
            pendingImages: [], pendingDocs: [], pendingQuotes: [],
            permission: "bypassPermissions",
            delivery: "queue",
            active_turn_id: "",
            stream_owner_token: "root-a-owner",
          });
          await new Promise(resolve => setTimeout(resolve, 10));
          st.streaming = false;
          st._streamOwnerToken = "";
          st.activeTurnId = "";
          st.streaming = true;
          st._streamOwnerToken = "root-b-owner";
          st.activeTurnId = "successor-turn-b";
          const firstQueued = await enqueue;
          const activeCallsAfterFirst = activeCalls;

          // Also cover the earlier race: A can become B while send() awaits
          // its busy probe, before _enqueueMessage even enters the resolver.
          st.pendingQueue = [];
          const secondQueued = await app._enqueueMessage(sid, {
            text: "SNAPSHOT_FROM_ROOT_A",
            displayText: "SNAPSHOT_FROM_ROOT_A",
            pendingImages: [], pendingDocs: [], pendingQuotes: [],
            permission: "bypassPermissions",
            delivery: "queue",
            active_turn_id: "",
            stream_owner_token: "root-a-owner",
          });
          return {
            firstQueued,
            secondQueued,
            queuePayloads,
            activeCalls,
            activeCallsAfterFirst,
          };
        } finally {
          window.fetch = originalFetch;
        }
        """,
        {"sid": sid},
    )

    assert result["firstQueued"] is True
    assert result["secondQueued"] is True
    assert result["activeCallsAfterFirst"] == 1
    assert result["activeCalls"] == 1
    assert len(result["queuePayloads"]) == 2
    assert all(payload["delivery"] == "queue"
               for payload in result["queuePayloads"])
    assert all(payload["active_turn_id"] == ""
               for payload in result["queuePayloads"])
    _assert_no_browser_errors(page, errors)


def test_started_queue_steering_becomes_user_bubble_at_stream_boundary(
    page: Page, backend_url, auth_token,
):
    """A native mid-turn adjustment replaces its queue row in event order."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-midturn-steering-bubble"
    command_uuid = "midturn-command-uuid"
    item_id = "midturn-queue-item"
    adjustment = "MIDTURN_ADJUSTMENT_VISIBLE"
    second_command_uuid = "midturn-second-command-uuid"
    second_item_id = "midturn-second-queue-item"
    second_adjustment = "MIDTURN_SECOND_ADJUSTMENT_VISIBLE"
    history_marker = "MIDTURN_HISTORY_MARKER"
    canonical_thinking_uuid = "midturn-canonical-thinking"
    canonical_messages: list[dict] = []
    requests = _route_windowed_session(
        page, sid, canonical_messages, updated_at=2,
    )
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"midturn-steering-ticket"}',
        ),
    )
    page.route(
        f"**/api/chat/sessions/{sid}/active",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            # A terminal frame may beat the backend's inactive snapshot. The
            # exact assistant UUID in canonical history must still settle the
            # live transcript without waiting for this endpoint to catch up.
            body=json.dumps({
                "active": True,
                "background": False,
                "turn_id": "midturn-turn",
            }),
        ),
    )
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = arg.sid;
        app.refreshSessions = async () => {};
        app._pullSessionList = async () => false;
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
          id: sid, name: "Mid-turn steering bubble", updated_at: 1,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st._loaded = true;
        st._seenUpdated = 1;
        st.messages.push({
          role: "assistant", text: arg.historyMarker,
          html: `<p>${arg.historyMarker}</p>`, uuid: "midturn-history-uuid",
          _k: `${sid}:uuid:midturn-history-uuid`, _noAnim: true,
        });
        Object.assign(st.messageRange, {
          visibleStart: 0, visibleEnd: 1, offset: 0, total: 1,
          preTotal: 0, order: "full", generation: "midturn-e2e",
        });
        app.currentId = sid;
        app._activateTabState(sid);
        st.messagesReady = true;
        st.messagesLoading = false;
        st.atBottom = true;
        app.mobileTab = "chat";
        app.input = "MIDTURN_ORIGINAL_PROMPT";
        return true;
        """,
        {"sid": sid, "historyMarker": history_marker},
    )

    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    _app_eval(
        page,
        """
        const st = app._ensureTabState(arg.sid);
        st.pendingQueue = [{
          id: arg.itemId,
          text: arg.adjustment,
          displayText: arg.adjustment,
          pendingQuotes: [], images: [], docs: [],
          delivery: "adjust", deliveryStatus: "waiting_tool",
          commandUuid: arg.commandUuid,
          enqueuedAt: Date.now(),
        }];
        return true;
        """,
        {
            "sid": sid,
            "itemId": item_id,
            "commandUuid": command_uuid,
            "adjustment": adjustment,
        },
    )
    expect(page.locator(".msg.user.queued")).to_contain_text(
        adjustment, timeout=5000
    )

    page.evaluate(
        """arg => {
          const common = { turn_id: "midturn-turn", session_id: arg.sid };
          window.__emitSse("text", {
            ...common, event_seq: 1, text: "ASSISTANT_BEFORE_ADJUSTMENT",
          });
          window.__emitSse("tool_use", {
            ...common, event_seq: 2, id: "midturn-tool-use",
            name: "Read", summary: "inspect before adjustment", input: {},
          });
          window.__emitSse("tool_result", {
            ...common, event_seq: 3, id: "midturn-tool-use",
            tool_name: "Read", preview: "TOOL_RESULT_BEFORE_ADJUSTMENT",
            text: "TOOL_RESULT_BEFORE_ADJUSTMENT", is_error: false,
          });
          const steering = {
            ...common,
            item_id: arg.itemId,
            command_uuid: arg.commandUuid,
            state: "started",
            effective_delivery: "adjust",
            message: {
              id: arg.itemId, uuid: arg.commandUuid,
              text: arg.adjustment, display_text: arg.adjustment,
              selection_quotes: [],
            },
          };
          window.__emitSse("queue_steering", steering);
          window.__emitSse("text", {
            ...common, event_seq: 4, text: "ASSISTANT_AFTER_PART_A",
          });
          // The terminal lifecycle event is a duplicate transcript boundary,
          // not a second user message and not a reason to split assistant text.
          window.__emitSse("queue_steering", {...steering, state: "completed"});
          window.__emitSse("tool_use", {
            ...common, event_seq: 5, id: "midturn-tool-use-2",
            name: "Read", summary: "inspect after first adjustment", input: {},
          });
          window.__emitSse("tool_result", {
            ...common, event_seq: 6, id: "midturn-tool-use-2",
            tool_name: "Read", preview: "TOOL_RESULT_AFTER_ADJUSTMENT",
            text: "TOOL_RESULT_AFTER_ADJUSTMENT", is_error: false,
          });
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          st.pendingQueue.push({
            id: arg.secondItemId,
            text: arg.secondAdjustment,
            displayText: arg.secondAdjustment,
            pendingQuotes: [], images: [], docs: [],
            delivery: "adjust", deliveryStatus: "waiting_tool",
            commandUuid: arg.secondCommandUuid,
            enqueuedAt: Date.now(),
          });
          const secondSteering = {
            ...common,
            item_id: arg.secondItemId,
            command_uuid: arg.secondCommandUuid,
            state: "started",
            effective_delivery: "adjust",
            message: {
              id: arg.secondItemId, uuid: arg.secondCommandUuid,
              text: arg.secondAdjustment, display_text: arg.secondAdjustment,
              selection_quotes: [],
            },
          };
          window.__emitSse("queue_steering", secondSteering);
          window.__emitSse("text", {
            ...common, event_seq: 7, text: "ASSISTANT_AFTER_PART_B",
          });
          window.__emitSse("queue_steering", {
            ...secondSteering, state: "completed",
          });
        }""",
        {
            "sid": sid,
            "itemId": item_id,
            "commandUuid": command_uuid,
            "adjustment": adjustment,
            "secondItemId": second_item_id,
            "secondCommandUuid": second_command_uuid,
            "secondAdjustment": second_adjustment,
        },
    )
    page.wait_for_function(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          const adjustmentIndex = st.messages.findIndex(
            m => m.role === "user" && m.uuid === arg.commandUuid);
          const secondIndex = st.messages.findIndex(
            m => m.role === "user" && m.uuid === arg.secondCommandUuid);
          const after = secondIndex >= 0 ? st.messages[secondIndex + 1] : null;
          return adjustmentIndex >= 0 && secondIndex > adjustmentIndex
            && st.pendingQueue.length === 0
            && after?.role === "assistant"
            && after.text === "ASSISTANT_AFTER_PART_B";
        }""",
        arg={
            "sid": sid,
            "commandUuid": command_uuid,
            "secondCommandUuid": second_command_uuid,
        },
        timeout=10000,
    )

    result = page.evaluate(
        """arg => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          const indexOf = predicate => st.messages.findIndex(predicate);
          const before = indexOf(m => (m.text || "").includes(
            "ASSISTANT_BEFORE_ADJUSTMENT"));
          const toolUse = indexOf(m => m.id === "midturn-tool-use"
            && m.role === "tool_use");
          const toolResult = indexOf(m => m.id === "midturn-tool-use"
            && m.role === "tool_result");
          const adjustment = indexOf(m => m.role === "user"
            && m.uuid === arg.commandUuid);
          const afterFirst = st.messages.findIndex((m, i) => i > adjustment
            && m.role === "assistant"
            && (m.text || "").includes("ASSISTANT_AFTER_PART_A"));
          const toolUse2 = indexOf(m => m.id === "midturn-tool-use-2"
            && m.role === "tool_use");
          const toolResult2 = indexOf(m => m.id === "midturn-tool-use-2"
            && m.role === "tool_result");
          const secondAdjustment = indexOf(m => m.role === "user"
            && m.uuid === arg.secondCommandUuid);
          const afterSecond = st.messages.findIndex((m, i) => i > secondAdjustment
            && m.role === "assistant"
            && (m.text || "").includes("ASSISTANT_AFTER_PART_B"));
          const user = st.messages[adjustment];
          const secondUser = st.messages[secondAdjustment];
          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(arg.sid)}"]`);
          const node = pane?.querySelector(
            `.msg.user[data-uuid="${CSS.escape(arg.commandUuid)}"]`);
          const secondNode = pane?.querySelector(
            `.msg.user[data-uuid="${CSS.escape(arg.secondCommandUuid)}"]`);
          const canonical = app._preserveCanonicalMessageIdentity(st, [{
            role: "user", text: arg.adjustment,
            displayText: arg.adjustment, uuid: arg.commandUuid,
          }])[0];
          const secondCanonical = app._preserveCanonicalMessageIdentity(st, [{
            role: "user", text: arg.secondAdjustment,
            displayText: arg.secondAdjustment, uuid: arg.secondCommandUuid,
          }])[0];
          window.__midturnLiveUser = user;
          window.__midturnLiveKey = user?._k || "";
          window.__midturnSecondLiveUser = secondUser;
          window.__midturnSecondLiveKey = secondUser?._k || "";
          const visibleRunningFooters = [...document.querySelectorAll(
            ".turn-footer .turn-status.running")].filter(node => {
              const footer = node.closest(".turn-footer");
              return footer && getComputedStyle(footer).display !== "none"
                && getComputedStyle(node).display !== "none";
            });
          return {
            before, toolUse, toolResult, adjustment, afterFirst,
            toolUse2, toolResult2, secondAdjustment, afterSecond,
            pendingCount: st.pendingQueue.length,
            adjustmentCount: st.messages.filter(m => m.role === "user"
              && m.uuid === arg.commandUuid).length,
            secondAdjustmentCount: st.messages.filter(m => m.role === "user"
              && m.uuid === arg.secondCommandUuid).length,
            afterCount: st.messages.filter((m, i) => i > adjustment
              && m.role === "assistant"
              && (m.text || "").includes("ASSISTANT_AFTER_PART_A")).length,
            afterText: st.messages[afterFirst]?.text || "",
            afterSecondText: st.messages[afterSecond]?.text || "",
            turnId: user?._turnId || "",
            noAnim: user?._noAnim === true,
            normalBubble: !!node,
            normalBubbleText: node?.textContent || "",
            secondNormalBubble: !!secondNode,
            secondNormalBubbleText: secondNode?.textContent || "",
            queuedBubbleCount: document.querySelectorAll(".msg.user.queued").length,
            sameCanonicalObject: canonical === user,
            sameCanonicalKey: canonical?._k === user?._k,
            sameSecondCanonicalObject: secondCanonical === secondUser,
            sameSecondCanonicalKey: secondCanonical?._k === secondUser?._k,
            beforeStatus: app.turnFooterStatus(st.messages[toolResult], st),
            afterStatus: app.turnFooterStatus(st.messages[afterSecond], st),
            visibleRunningFooterCount: visibleRunningFooters.length,
          };
        }""",
        {
            "sid": sid,
            "commandUuid": command_uuid,
            "adjustment": adjustment,
            "secondCommandUuid": second_command_uuid,
            "secondAdjustment": second_adjustment,
        },
    )
    assert 0 <= result["before"] < result["toolUse"] < result["toolResult"]
    assert result["toolResult"] < result["adjustment"] < result["afterFirst"]
    assert result["afterFirst"] < result["toolUse2"] < result["toolResult2"]
    assert result["toolResult2"] < result["secondAdjustment"] < result["afterSecond"]
    assert result["pendingCount"] == 0
    assert result["adjustmentCount"] == 1
    assert result["secondAdjustmentCount"] == 1
    assert result["afterCount"] == 1
    assert result["afterText"] == "ASSISTANT_AFTER_PART_A"
    assert result["afterSecondText"] == "ASSISTANT_AFTER_PART_B"
    assert result["turnId"] == "midturn-turn"
    assert result["noAnim"] is True
    assert result["normalBubble"] is True
    assert adjustment in result["normalBubbleText"]
    assert result["secondNormalBubble"] is True
    assert second_adjustment in result["secondNormalBubbleText"]
    assert result["queuedBubbleCount"] == 0
    assert result["sameCanonicalObject"] is True
    assert result["sameCanonicalKey"] is True
    assert result["sameSecondCanonicalObject"] is True
    assert result["sameSecondCanonicalKey"] is True
    assert result["beforeStatus"] == ""
    assert result["afterStatus"] == "running"
    assert result["visibleRunningFooterCount"] == 1

    canonical_messages.extend([
        {
            "role": "assistant", "text": history_marker,
            "html": f"<p>{history_marker}</p>",
            "uuid": "midturn-history-uuid", "ts": 1_700_030_000,
            "turn_status": "completed",
            "block_id": "midturn-history-uuid:0:assistant",
            "_key": "midturn-history-uuid:0:assistant",
        },
        {
            "role": "user", "text": "MIDTURN_ORIGINAL_PROMPT",
            "uuid": "midturn-root-user", "_turnRoot": True,
            "block_id": "midturn-root-user:0:user",
            "_key": "midturn-root-user:0:user",
        },
        {
            "role": "assistant", "text": "ASSISTANT_BEFORE_ADJUSTMENT",
            "uuid": "midturn-before-assistant",
            "block_id": "midturn-before-assistant:0:assistant",
            "_key": "midturn-before-assistant:0:assistant",
        },
        {
            "role": "tool_use", "id": "midturn-tool-use", "name": "Read",
            "summary": "inspect before adjustment", "input": {},
            "uuid": "midturn-before-assistant",
            "block_id": "midturn-before-assistant:1:tool_use",
            "_key": "midturn-before-assistant:1:tool_use",
        },
        {
            "role": "tool_result", "id": "midturn-tool-use",
            "tool_name": "Read", "preview": "TOOL_RESULT_BEFORE_ADJUSTMENT",
            "text": "TOOL_RESULT_BEFORE_ADJUSTMENT", "is_error": False,
            "uuid": "midturn-before-result",
            "block_id": "midturn-before-result:0:tool_result",
            "_key": "midturn-before-result:0:tool_result",
        },
        {
            "role": "user", "text": adjustment, "displayText": adjustment,
            "selectionQuotes": [], "uuid": command_uuid,
            "_steeringAdjustment": True, "_turnRoot": False,
            "block_id": f"{command_uuid}:0:user",
            "_key": f"{command_uuid}:0:user",
        },
        {
            "role": "thinking", "text": "[encrypted thinking]",
            "uuid": canonical_thinking_uuid,
            "block_id": f"{canonical_thinking_uuid}:0:thinking",
            "_key": f"{canonical_thinking_uuid}:0:thinking",
        },
        {
            "role": "assistant", "text": "ASSISTANT_AFTER_PART_A",
            "uuid": "midturn-after-first-assistant",
            "block_id": "midturn-after-first-assistant:0:assistant",
            "_key": "midturn-after-first-assistant:0:assistant",
        },
        {
            "role": "tool_use", "id": "midturn-tool-use-2", "name": "Read",
            "summary": "inspect after first adjustment", "input": {},
            "uuid": "midturn-after-first-assistant",
            "block_id": "midturn-after-first-assistant:1:tool_use",
            "_key": "midturn-after-first-assistant:1:tool_use",
        },
        {
            "role": "tool_result", "id": "midturn-tool-use-2",
            "tool_name": "Read", "preview": "TOOL_RESULT_AFTER_ADJUSTMENT",
            "text": "TOOL_RESULT_AFTER_ADJUSTMENT", "is_error": False,
            "uuid": "midturn-after-first-result",
            "block_id": "midturn-after-first-result:0:tool_result",
            "_key": "midturn-after-first-result:0:tool_result",
        },
        {
            "role": "user", "text": second_adjustment,
            "displayText": second_adjustment, "selectionQuotes": [],
            "uuid": second_command_uuid,
            "_steeringAdjustment": True, "_turnRoot": False,
            "block_id": f"{second_command_uuid}:0:user",
            "_key": f"{second_command_uuid}:0:user",
        },
        {
            "role": "assistant",
            "text": "ASSISTANT_AFTER_PART_B",
            "uuid": "midturn-final-assistant", "ts": 1_700_030_010,
            "turn_status": "completed",
            "block_id": "midturn-final-assistant:0:assistant",
            "_key": "midturn-final-assistant:0:assistant",
        },
    ])
    page.evaluate(
        """() => window.__emitSse("done", {
          total_cost_usd: 0.001, turn_id: "midturn-turn", event_seq: 8,
          assistant_uuid: "midturn-final-assistant",
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
    page.wait_for_function(
        """arg => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(arg.sid);
          return st.messages.some(m => m.uuid === "midturn-final-assistant")
            && st.messages.some(m => m.uuid === arg.commandUuid)
            && st.messages.some(m => m.uuid === arg.secondCommandUuid)
            && st.messages.some(m => m.uuid === arg.thinkingUuid);
        }""",
        arg={
            "sid": sid,
            "commandUuid": command_uuid,
            "secondCommandUuid": second_command_uuid,
            "thinkingUuid": canonical_thinking_uuid,
        },
        timeout=10000,
    )
    thinking_row = page.locator(
        f'.msg-pane[data-tid="{sid}"] '
        f'.msg.thinking[data-uuid="{canonical_thinking_uuid}"]'
    )
    expect(thinking_row).to_be_visible(timeout=10000)
    completed_footer = page.locator(
        f'.msg-pane[data-tid="{sid}"] '
        '.msg[data-uuid="midturn-final-assistant"] '
        '.turn-footer .turn-status.completed'
    )
    expect(completed_footer).to_be_visible(timeout=10000)
    expect(
        page.locator(
            f'.msg-pane[data-tid="{sid}"] '
            '.turn-footer .turn-status.running:visible'
        )
    ).to_have_count(0)
    completed = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg.sid);
        const root = st.messages.findIndex(m => m.uuid === "midturn-root-user");
        const steering = st.messages.find(m => m.uuid === arg.commandUuid);
        const secondSteering = st.messages.find(
          m => m.uuid === arg.secondCommandUuid);
        const firstTail = st.messages.find(m => m.uuid === "midturn-before-result");
        const secondToolTail = st.messages.find(
          m => m.uuid === "midturn-after-first-result");
        const final = st.messages.find(m => m.uuid === "midturn-final-assistant");
        const indexOfUuid = uuid => st.messages.findIndex(m => m.uuid === uuid);
        const statuses = st.messages.slice(root + 1)
          .map(m => app.turnFooterStatus(m, st)).filter(Boolean);
        return {
          steeringCount: st.messages.filter(
            m => m.uuid === arg.commandUuid).length,
          secondSteeringCount: st.messages.filter(
            m => m.uuid === arg.secondCommandUuid).length,
          sameSteeringObject: steering === window.__midturnLiveUser,
          sameSteeringKey: steering?._k === window.__midturnLiveKey,
          liveSteeringKey: window.__midturnLiveKey,
          canonicalSteeringKey: steering?._k || "",
          sameSecondSteeringObject:
            secondSteering === window.__midturnSecondLiveUser,
          sameSecondSteeringKey:
            secondSteering?._k === window.__midturnSecondLiveKey,
          firstStatus: app.turnFooterStatus(firstTail, st),
          secondToolStatus: app.turnFooterStatus(secondToolTail, st),
          finalStatus: app.turnFooterStatus(final, st),
          currentTurnStatuses: statuses,
          hasPostResult: st.messages.some(m => m.uuid === "midturn-final-assistant"
            && m.text === "ASSISTANT_AFTER_PART_B"),
          hasCanonicalThinking: st.messages.some(
            m => m.uuid === arg.thinkingUuid && m.role === "thinking"),
          steeringFlag: steering?._steeringAdjustment === true,
          secondSteeringFlag: secondSteering?._steeringAdjustment === true,
          canonicalOrder: [
            indexOfUuid(arg.commandUuid),
            indexOfUuid(arg.thinkingUuid),
            indexOfUuid("midturn-after-first-assistant"),
            indexOfUuid("midturn-after-first-result"),
            indexOfUuid(arg.secondCommandUuid),
            indexOfUuid("midturn-final-assistant"),
          ],
        };
        """,
        {
            "sid": sid,
            "commandUuid": command_uuid,
            "secondCommandUuid": second_command_uuid,
            "thinkingUuid": canonical_thinking_uuid,
        },
    )
    assert requests, "done reconciliation did not request canonical history"
    assert completed["steeringCount"] == 1
    assert completed["secondSteeringCount"] == 1
    assert completed["sameSteeringObject"] is True
    assert completed["sameSteeringKey"] is True, (
        completed["liveSteeringKey"], completed["canonicalSteeringKey"])
    assert completed["sameSecondSteeringObject"] is True
    assert completed["sameSecondSteeringKey"] is True
    assert completed["firstStatus"] == ""
    assert completed["secondToolStatus"] == ""
    assert completed["finalStatus"] == "completed"
    assert completed["currentTurnStatuses"] == ["completed"]
    assert completed["hasPostResult"] is True
    assert completed["hasCanonicalThinking"] is True
    assert completed["steeringFlag"] is True
    assert completed["secondSteeringFlag"] is True
    assert completed["canonicalOrder"] == sorted(completed["canonicalOrder"])
    assert all(index >= 0 for index in completed["canonicalOrder"])
    _assert_no_browser_errors(page, errors)


def test_desktop_done_reconcile_preserves_live_message_dom_identity(
    page: Page, backend_url, auth_token,
):
    """Done keeps its node while canonical history replaces stale live HTML."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-done-canonical-identity"
    prompt = "DOM_IDENTITY_USER_PROMPT"
    history_marker = "FULL_ORDER_HISTORY_SURVIVES_LRU_REMOUNT"
    live_text = "DOM_IDENTITY_PARTIAL_REPLY"
    canonical_marker = "CANONICAL_ONLY_SUFFIX_VISIBLE"
    final_text = (
        live_text + " "
        + ("stable canonical text " * 40)
        + canonical_marker
    )
    canonical_messages: list[dict] = [{
        "role": "assistant",
        "text": history_marker,
        "uuid": "done-full-history-assistant",
        "ts": 1_700_019_999,
    }]
    requests = _route_windowed_session(
        page, sid, canonical_messages, updated_at=2,
    )
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
        // done uses the quiet list path directly now. Keep this synthetic
        // session resident so the test observes canonical message morphing,
        // not an unrelated real session-list pull removing its fake sid.
        app._pullSessionList = async () => false;
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
        st.messages.push({
          role: "assistant", text: arg.historyMarker,
          html: `<p>${arg.historyMarker}</p>`,
          uuid: "done-full-history-assistant",
          _k: `${sid}:uuid:done-full-history-assistant`, _noAnim: true,
        });
        Object.assign(st.messageRange, {
          visibleStart: 0, visibleEnd: 1, offset: 0, total: 1,
          preTotal: 0, order: "full", generation: "gen-e2e-1",
        });
        app.currentId = sid;
        app._activateTabState(sid);
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
        app.mobileTab = "chat";
        app.input = arg.prompt;
        app._ensureTabState(app.currentId).atBottom = true;
        return true;
        """,
        {"sid": sid, "prompt": prompt, "historyMarker": history_marker},
    )

    _app_eval(page, "app.send(); return true;")
    page.wait_for_function(
        "() => window.__fakeChatStreams && window.__fakeChatStreams().length === 1"
    )
    page.evaluate(
        """text => window.__emitSse("text", {
          text, turn_id: "done-reconcile-turn", event_seq: 1,
        })""",
        live_text,
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
        arg=live_text,
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
          memory_recall: { count: 2, query: "private-query" },
          session_usage: { context_used_pct: 5, context_used: 500, context_limit: 100000 },
          turn_id: "done-reconcile-turn",
          assistant_uuid: "done-canonical-assistant", event_seq: 2,
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
        """async ({ sid, marker }) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          st._pendingExternalUpdate = true;
          app._reconcileOpenSession([{
            ...app.sessions[0], id: sid, updated_at: 2, active: false,
          }]);
          const frames = [];
          const canonicalSyncBusy = () => {
            const sync = st.sessionSync || {};
            const reasons = new Set(["completed_turn", "history_revision"]);
            return reasons.has(sync.inFlight?.reason)
              || Object.keys(sync.pending || {}).some(reason => reasons.has(reason));
          };
          // Reconciliation is owned by sessionSync now. The old
          // `_reconcilePromise` field no longer exists, so checking it made
          // this test race the coordinator on fast runners and assert against
          // the earlier tail-only completion probe. Observe every frame while
          // the real coordinator drains instead.
          for (let i = 0; i < 240; i++) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const pane = document.querySelector(
              `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
            const canonicalReady = st.messages.some(
              message => message?.uuid === "done-canonical-assistant");
            frames.push({
              ready: st.messagesReady,
              loading: st.messagesLoading,
              canonicalReady,
              visible: !!pane && pane.innerText.includes(marker),
              count: pane ? pane.querySelectorAll(".msg").length : 0,
            });
            if (canonicalReady && !canonicalSyncBusy() && i >= 2) break;
          }
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
            cost: last.cost || "",
            memoryCount: Number(last.memoryRecall?.count) || 0,
            historyOrder: st.messageRange.order,
            visible: !!canonicalNode
              && canonicalNode.innerText.includes(marker),
          };
        }""",
        {"sid": sid, "marker": canonical_marker},
    )

    assert requests, "canonical reconciliation did not request session history"
    assert any(req["full"] and req["tail"] for req in requests), requests
    assert result["sameNode"] is True, result
    assert result["key"] == result["oldKey"]
    assert result["uuid"] == "done-canonical-assistant"
    assert result["text"] == final_text
    assert result["cost"] == "$0.0010"
    assert result["memoryCount"] == 2
    assert result["historyOrder"] == "full"
    assert result["frames"]
    assert all(frame["ready"] and not frame["loading"] for frame in result["frames"]), result
    assert all(frame["count"] > 0 for frame in result["frames"]), result
    assert any(
        frame["canonicalReady"] and frame["visible"] for frame in result["frames"]
    ), result
    assert result["visible"] is True, result

    remount = _app_eval(
        page,
        """
        const sid = arg.sid;
        const dummyIds = ["done-lru-a", "done-lru-b", "done-lru-c"];
        app.sessions = [app.sessions.find(s => s.id === sid), ...dummyIds.map((id, i) => ({
          id, name: `LRU ${i}`, updated_at: 10 + i,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }))];
        app.openTabIds = [sid, ...dummyIds];
        app.currentId = sid;
        app._touchTranscriptPane(sid);
        await new Promise(resolve => app.$nextTick(resolve));
        for (const id of dummyIds) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messagesReady = true;
          st.messagesLoading = false;
          st.messages.push({
            role: "assistant", text: id, html: `<p>${id}</p>`,
            uuid: `${id}-assistant`, _k: `${id}:uuid:${id}-assistant`, _noAnim: true,
          });
          Object.assign(st.messageRange, {
            visibleStart: 0, visibleEnd: 1, offset: 0, total: 1,
            preTotal: 0, order: "normal", generation: "gen-e2e-1",
          });
          app.tabState[id] = st;
          app.currentId = id;
          app._touchTranscriptPane(id);
          app._activateTabState(id);
          await new Promise(resolve => app.$nextTick(resolve));
        }
        const evicted = !app.warmTranscriptTabIds().includes(sid);
        app.currentId = sid;
        app._touchTranscriptPane(sid);
        app._activateTabState(sid);
        await new Promise(resolve => app.$nextTick(
          () => requestAnimationFrame(resolve)));
        const pane = document.querySelector(
          `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
        return {
          evicted,
          warmCount: app.warmTranscriptTabIds().length,
          markerVisible: !!pane && pane.textContent.includes(arg.historyMarker),
          finalVisible: !!pane && pane.textContent.includes(arg.finalText.trim()),
          historyOrder: app._ensureTabState(sid).messageRange.order,
        };
        """,
        {"sid": sid, "historyMarker": history_marker, "finalText": final_text},
    )
    assert remount["evicted"] is True, remount
    assert remount["warmCount"] <= 3, remount
    assert remount["markerVisible"] is True, remount
    assert remount["finalVisible"] is True, remount
    assert remount["historyOrder"] == "full"
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
        app.mobileTab = "chat";
        app.input = arg.prompt;
        app._ensureTabState(app.currentId).atBottom = true;
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
          return app._ensureTabState(app.currentId).streaming && app._ensureTabState(app.currentId).messages.length === 6
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
          window.__cancelledSnapshotKeys = app._ensureTabState(app.currentId).messages.map(m => m._k);
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
          return !app._ensureTabState(app.currentId).streaming && !st.streaming && st._loaded
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
          const tail = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          const footer = nodes[nodes.length - 1]?.querySelector('.turn-footer');
          return {
            minCount: window.__cancelledSnapshotMinCount,
            count: nodes.length,
            sameNodes: nodes.every(
              (node, index) => node === window.__cancelledSnapshotNodes[index]),
            keys: app._ensureTabState(app.currentId).messages.map(message => message._k),
            ready: app._ensureTabState(app.currentId).messagesReady,
            loading: app._ensureTabState(app.currentId).messagesLoading,
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
    """Done completes the live tail while canonical history is still retrying."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _install_fake_event_source(page)
    sid = "perf-tool-tail-done-metadata"
    assistant_uuid = "tool-tail-assistant-boundary"
    completed_at_ms = int(time.time() * 1000)
    duration_ms = 125_000
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
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"active":true}',
        )

    def incomplete_history(route):
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
    page.route(f"**/api/chat/sessions/{sid}?*", incomplete_history)
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
        app.mobileTab = 'chat';
        app.input = 'Finish on a tool result';
        app._ensureTabState(app.currentId).atBottom = true;
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
          const tail = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming && tail?.role === 'tool_result';
        }"""
    )
    before_done = _app_eval(
        page,
        """
        const tail = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
        const assistant = [...app._ensureTabState(app.currentId).messages].reverse()
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
          const tail = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(arg.sid)}"]`);
          const tailNode = pane?.querySelector(
            `.msg[data-message-key="${CSS.escape(arg.tailKey)}"]`);
          const footer = tailNode?.querySelector('.turn-footer');
          return !app._ensureTabState(app.currentId).streaming
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
    expect(footer.locator(".turn-status > span:visible")).to_have_text(
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
        const tail = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
        const assistant = [...app._ensureTabState(app.currentId).messages].reverse()
          .find(message => message.role === 'assistant');
        return {
          role: tail.role,
          tailUuid: tail.uuid,
          tailForkUuid: tail.forkUuid,
          assistantUuid: assistant?.uuid || '',
          forkBoundary: app.turnForkMessageId(
            app._ensureTabState(app.currentId).messages, app._ensureTabState(app.currentId).messages.length - 1),
          ts: tail.ts,
          elapsed: tail.elapsed,
          model: tail.model,
          turnStatus: tail.turn_status,
          memoryRecallId: tail.memoryRecall?.id || '',
          liveKey: tail._k,
          streaming: app._ensureTabState(app.currentId).streaming,
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
    assert history_requests, "done did not probe canonical history immediately"
    assert all("tail=800" in url for url in history_requests)
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
          app._ensureTabState(app.currentId).messagesReady = true;
          app._ensureTabState(app.currentId).messagesLoading = false;
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


def test_live_turn_keeps_resident_messages_but_bounds_mounted_rows(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = "live-dom-budget";
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.appReady = true;
          app.sessions = [{id: sid, name: "Live budget", model: "e2e-model"}];
          app.openTabIds = [sid];
          app.tabState = {};
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st.streaming = true;
          st.atBottom = true;
          app.currentId = sid;
          app._activateTabState(sid);
          app._ensureTabState(app.currentId).messagesReady = true;
          app._ensureTabState(app.currentId).messagesLoading = false;
          for (let i = 0; i < 250; i++) {
            app._appendLiveMessage(st, {role: "thinking", text: `live ${i}`});
          }
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const followed = {
            resident: st.messages.length,
            start: st.messageRange.visibleStart,
            end: st.messageRange.visibleEnd,
            mounted: Array.from(document.querySelectorAll('.msg-pane')).filter(p => getComputedStyle(p).display !== 'none').reduce((n, p) => n + p.querySelectorAll('.msg').length, 0),
          };
          st.atBottom = false;
          const frozen = {start: st.messageRange.visibleStart, end: st.messageRange.visibleEnd};
          for (let i = 250; i < 300; i++) {
            app._appendLiveMessage(st, {role: "thinking", text: `live ${i}`});
          }
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const reading = {
            start: st.messageRange.visibleStart,
            end: st.messageRange.visibleEnd,
            resident: st.messages.length,
            hasLater: app.hasLaterMessages(sid),
          };
          const body = document.querySelector('.chat-body');
          body.scrollTop = body.scrollHeight;
          app.onChatScroll();
          const physicalBottomAtBottom = st.atBottom;
          const emojiStatus = app._normalizeTaskStatusPreview({
            summary: '😀'.repeat(1500), summary_length: 1500,
            summary_truncated: false,
          });
          // Reproduce the completed-middle-window regression: explicit selection
          // must acquire the logical tail, not merely scroll the bounded DOM.
          st.streaming = false;
          st.es = null;
          await app.activateTab(sid);
          await new Promise(resolve => app.$nextTick(() => requestAnimationFrame(resolve)));
          const latest = {
            start: st.messageRange.visibleStart,
            end: st.messageRange.visibleEnd,
            mounted: Array.from(document.querySelectorAll('.msg-pane')).filter(p => getComputedStyle(p).display !== 'none').reduce((n, p) => n + p.querySelectorAll('.msg').length, 0),
          };
          return {
            followed, frozen, reading, latest, physicalBottomAtBottom,
            emojiLength: Array.from(emojiStatus.summary).length,
            emojiTruncated: emojiStatus.summary_truncated,
          };
        }"""
    )

    assert result["followed"] == {
        "resident": 250, "start": 150, "end": 250, "mounted": 100,
    }
    assert result["reading"]["start"] == result["frozen"]["start"]
    assert result["reading"]["end"] == result["frozen"]["end"]
    assert result["reading"]["resident"] == 300
    assert result["reading"]["hasLater"] is True
    assert result["physicalBottomAtBottom"] is False
    assert result["emojiLength"] == 1500
    assert result["emojiTruncated"] is False
    assert result["latest"] == {"start": 200, "end": 300, "mounted": 100}
    _assert_no_browser_errors(page, errors)


def test_repeated_tool_turn_then_fast_canonical_turns_keep_exact_dom_order(
    page: Page, backend_url, auth_token,
):
    """Successive quiet installs must converge the keyed DOM without reload."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = "canonical-dom-fast-turns";
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.sessions = [{id: sid, name: "Canonical DOM", model: "e2e-model"}];
          app.openTabIds = [sid];
          app.tabState = {};
          app.currentId = sid;
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st.messagesReady = true;
          st.messagesLoading = false;
          st.atBottom = true;
          const canonical = [];
          const addCanonical = message => {
            canonical.push({...message, block_id: `${message.uuid}:0:${message.role}`});
          };
          const settle = async () => {
            let next = app._historyEnvelopes(
              sid, canonical.map(message => ({...message, _noAnim: true})));
            next = app._preserveCanonicalMessageIdentity(st, next);
            st.messageRange.visibleStart = Math.max(0, next.length - 100);
            st.messageRange.visibleEnd = next.length;
            st.messageRange.total = next.length;
            st.messages = next;
            app._syncSessionMessageStore(st);
            await new Promise(resolve => app.$nextTick(
              () => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          };
          const live = message => app._appendLiveMessage(st, message);

          for (let i = 0; i < 70; i++) {
            addCanonical({role: "assistant", text: `OLDER ${i}`,
                          uuid: `older-${i}`});
          }
          addCanonical({role: "user", text: "RUN TEN LS",
                        uuid: "prior-turn-user", _turnRoot: true});
          let midturnSteering = null;
          for (let i = 0; i < 10; i++) {
            const id = `prior-tool-${i}`;
            addCanonical({role: "tool_use", name: "Bash", text: "ls", id,
                          uuid: `prior-tool-use-${i}`});
            addCanonical({role: "tool_result", tool_name: "Bash",
                          text: "same output", preview: "same output", id,
                          tool_use_id: id, uuid: `prior-tool-result-${i}`});
          }
          addCanonical({role: "assistant", text: "TEN LS DONE",
                        uuid: "prior-turn-final", turn_status: "completed"});
          await settle();

          live({role: "user", text: "RUN TEN LS", _turnRoot: true});
          addCanonical({role: "user", text: "RUN TEN LS", uuid: "turn-a-user",
                        _turnRoot: true});
          for (let i = 0; i < 10; i++) {
            const id = `tool-${i}`;
            live({role: "tool_use", name: "Bash", text: "ls", id});
            addCanonical({role: "tool_use", name: "Bash", text: "ls", id,
                          uuid: `turn-a-tool-use-${i}`});
            live({role: "tool_result", tool_name: "Bash", text: "same output",
                  preview: "same output", id, tool_use_id: id});
            addCanonical({role: "tool_result", tool_name: "Bash",
                          text: "same output", preview: "same output", id,
                          tool_use_id: id, uuid: `turn-a-tool-result-${i}`});
            if (i === 0) {
              midturnSteering = live({
                role: "user", text: "test", uuid: "midturn-user",
                _steeringAdjustment: true, _turnRoot: false,
              });
              addCanonical({role: "user", text: "test",
                            uuid: "midturn-user", _steeringAdjustment: true,
                            _turnRoot: false});
            }
          }
          live({role: "assistant", text: "TEN LS DONE", forkUuid: "turn-a-final"});
          addCanonical({role: "assistant", text: "TEN LS DONE",
                        uuid: "turn-a-final", turn_status: "completed"});

          let firstQueuedLiveUser = null;
          let firstQueuedLiveAssistant = null;
          for (const [index, prompt, reply] of [
            [1, "test", "TEST REPLY"],
            [2, "test", "TEST REPLY"],
            [3, "stop", "STOP REPLY"],
          ]) {
            // The browser attached only to the first short queued turn. The
            // second identical turn and the final stop both completed between
            // /active probes and therefore exist only in canonical history.
            // This is the real refresh-only failure: weak newest-first text
            // matching must not move the first live `test` node onto turn 2.
            if (index === 1) {
              firstQueuedLiveUser = live({
                role: "user", text: prompt, _turnRoot: true,
                _turnId: `turn-${index}`,
              });
              firstQueuedLiveAssistant = live({
                role: "assistant", text: reply,
                forkUuid: `turn-${index}-assistant`,
                ts: "13:49", elapsed: 7, model: "first-live-model",
                turn_status: "completed",
              });
            }
            addCanonical({role: "user", text: prompt,
                          uuid: `turn-${index}-user`, _turnRoot: true});
            addCanonical({role: "assistant", text: reply,
                          uuid: `turn-${index}-assistant`,
                          turn_status: "completed"});
          }
          await settle();

          const firstCanonicalUser = st.messages.find(
            message => message.uuid === "turn-1-user");
          const secondCanonicalUser = st.messages.find(
            message => message.uuid === "turn-2-user");
          const firstCanonicalAssistant = st.messages.find(
            message => message.uuid === "turn-1-assistant");
          const finalCanonicalAssistant = st.messages.find(
            message => message.uuid === "turn-3-assistant");

          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
          const visible = st.messages.slice(
            st.messageRange.visibleStart, st.messageRange.visibleEnd);
          const stateKeys = visible.map(message => message._k);
          const stateRows = visible.map(message =>
            `${message.role}:${message.text || message.preview || ""}`);
          const dom = Array.from(pane.querySelectorAll(":scope > .msg"));
          const initial = {
            stateKeys,
            stateRows,
            domKeys: dom.map(node => node.dataset.messageKey),
            domRows: dom.map(node => {
              const message = (node._x_dataStack || [])
                .map(scope => scope && scope.m).find(Boolean);
                return `${message?.role || ""}:${message?.text || message?.preview || ""}`;
              }),
          };
          const epochBeforeRepair = st._transcriptRenderEpoch;
          dom[Math.floor(dom.length / 2)].remove();
          const repaired = await app._ensureTranscriptDomConverged(
            sid, st, {followTail: true});
          await new Promise(resolve => app.$nextTick(
            () => requestAnimationFrame(resolve)));
          const repairedPane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
          const repairedDomKeys = Array.from(repairedPane.querySelectorAll(
            ":scope > .msg[data-message-key]"
          )).map(node => node.dataset.messageKey);
          const epochBeforeSwitch = st._transcriptRenderEpoch;
          const switchedCheck = app._ensureTranscriptDomConverged(sid, st);
          app.currentId = "another-open-tab";
          const switchedAwayAccepted = await switchedCheck;
          app.currentId = sid;
          app._activateTabState(sid);
          return {
            initial,
            identity: {
              firstUserPreserved: firstCanonicalUser === firstQueuedLiveUser,
              firstAssistantPreserved:
                firstCanonicalAssistant === firstQueuedLiveAssistant,
              firstLiveUserOwnerUuid: st.messages.find(
                message => message === firstQueuedLiveUser)?.uuid || "",
              steeringOwnerUuid: st.messages.find(
                message => message === midturnSteering)?.uuid || "",
              firstLiveUserKey: firstQueuedLiveUser?._k || "",
              firstUserKey: firstCanonicalUser?._k || "",
              secondUserKey: secondCanonicalUser?._k || "",
              firstAssistantFooter: {
                ts: firstCanonicalAssistant?.ts || "",
                elapsed: firstCanonicalAssistant?.elapsed || 0,
                model: firstCanonicalAssistant?.model || "",
              },
              finalAssistantFooter: {
                ts: finalCanonicalAssistant?.ts || "",
                elapsed: finalCanonicalAssistant?.elapsed || 0,
                model: finalCanonicalAssistant?.model || "",
              },
            },
            repaired,
            epochBeforeRepair,
            epochAfterRepair: st._transcriptRenderEpoch,
            repairedDomKeys,
            switchedAwayAccepted,
            epochBeforeSwitch,
            epochAfterSwitch: st._transcriptRenderEpoch,
          };
        }"""
    )

    initial = result["initial"]
    assert len(initial["stateKeys"]) == len(set(initial["stateKeys"])), result
    assert len(initial["domKeys"]) == len(set(initial["domKeys"])), result
    assert initial["domKeys"] == initial["stateKeys"], result
    assert initial["domRows"] == initial["stateRows"], result
    assert result["identity"]["firstAssistantPreserved"] is True, result
    assert result["identity"]["firstLiveUserOwnerUuid"] != "turn-2-user", result
    assert result["identity"]["steeringOwnerUuid"] == "midturn-user", result
    assert result["identity"]["secondUserKey"] != result["identity"]["firstLiveUserKey"], result
    assert result["identity"]["firstUserKey"] != result["identity"]["secondUserKey"], result
    assert result["identity"]["firstAssistantFooter"] == {
        "ts": "13:49", "elapsed": 7, "model": "first-live-model",
    }, result
    assert result["identity"]["finalAssistantFooter"] == {
        "ts": "", "elapsed": 0, "model": "",
    }, result
    assert result["repaired"] is True, result
    assert result["epochAfterRepair"] == result["epochBeforeRepair"] + 1, result
    assert result["repairedDomKeys"] == initial["stateKeys"], result
    assert result["switchedAwayAccepted"] is True, result
    assert result["epochAfterSwitch"] == result["epochBeforeSwitch"], result
    _assert_no_browser_errors(page, errors)


def test_queue_attach_final_canonical_load_converges_duplicate_fast_successors(
    page: Page, backend_url, auth_token,
):
    """The real queue-attach fallback must install every fast successor once."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    sid = "queue-attach-duplicate-successors"

    canonical_messages: list[dict] = []

    def add_message(role: str, text: str, uuid: str, **extra) -> None:
        canonical_messages.append({
            "role": role,
            "text": text,
            "uuid": uuid,
            "block_id": f"{uuid}:0:{role}",
            **extra,
        })

    # 99 durable rows precede the first queued turn. Together with the three
    # queued prompt/reply pairs below, the final canonical snapshot has 105
    # blocks, matching the long tool-heavy transcript that exposed the bug.
    for index in range(77):
        add_message("assistant", f"OLDER {index}", f"older-{index}")
    add_message(
        "user", "RUN TEN LS", "direct-turn-user", _turnRoot=True,
    )
    for index in range(10):
        tool_id = f"direct-tool-{index}"
        add_message(
            "tool_use", "ls", f"direct-tool-use-{index}",
            id=tool_id, name="Bash",
        )
        add_message(
            "tool_result", "same output", f"direct-tool-result-{index}",
            id=tool_id, tool_use_id=tool_id, tool_name="Bash",
            preview="same output",
        )
    add_message(
        "assistant", "TEN LS DONE", "direct-turn-final",
        turn_status="completed", model="e2e-model", ts=69_000, elapsed=9,
    )
    assert len(canonical_messages) == 99

    add_message("user", "test", "turn-1-user", _turnRoot=True)
    add_message(
        "assistant", "TEST REPLY", "turn-1-assistant",
        turn_status="completed", model="e2e-model", ts=70_000, elapsed=1,
    )
    live_canonical_count = len(canonical_messages)
    add_message("user", "test", "turn-2-user", _turnRoot=True)
    add_message(
        "assistant", "TEST REPLY", "turn-2-assistant",
        turn_status="completed", model="e2e-model", ts=71_000, elapsed=1,
    )
    add_message("user", "stop", "turn-3-user", _turnRoot=True)
    add_message(
        "assistant", "STOP REPLY", "turn-3-assistant",
        turn_status="completed", model="e2e-model", ts=72_000, elapsed=1,
    )
    assert len(canonical_messages) == 105

    queue_requests: list[str] = []
    active_requests: list[str] = []
    history_requests: list[str] = []

    def empty_queue(route):
        queue_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": [], "paused": False, "revision": 72}),
        )

    def inactive_queued(route):
        active_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "active": False,
                "activity_source": "queued",
                "background_tasks_pending": 0,
            }),
        )

    def final_canonical_history(route):
        history_requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Queue attach duplicate successors",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "updated_at": 72,
                "messages": canonical_messages,
                "offset": 0,
                "total": len(canonical_messages),
                "message_count": len(canonical_messages),
                "pre_total": 0,
                "history_order": "normal",
                "history_generation": "queue-generation-72",
                "runtime_ui_revision": "queue-ui-revision-72",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}/queue", empty_queue)
    page.route(f"**/api/chat/sessions/{sid}/active", inactive_queued)
    page.route(
        re.compile(
            rf".*/api/chat/sessions/{re.escape(sid)}\?tail=\d+(?:&.*)?$"
        ),
        final_canonical_history,
    )
    _login(page, backend_url, auth_token)

    result = _app_eval(
        page,
        """
        const sid = arg.sid;
        if (app._sessionsSyncTimer) clearInterval(app._sessionsSyncTimer);
        app._sessionsSyncTimer = null;
        app.refreshSessions = async () => {};
        app._pullSessionList = async () => false;
        app._syncSessionListQuiet = async () => false;
        app._fetchTabUsage = async () => {};
        app._checkActiveTurn = () => {};
        app._scheduleIdlePreload = () => {};
        app.appReady = true;
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.sessions = [{
          id: sid, name: "Queue attach duplicate successors", updated_at: 70,
          message_count: arg.baseMessages.length,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        st.messages = app._historyEnvelopes(
          sid, arg.baseMessages.map(message => ({...message, _noAnim: true})));
        Object.assign(st.messageRange, {
          visibleStart: 0,
          visibleEnd: st.messages.length,
          offset: 0,
          total: st.messages.length,
          preTotal: 0,
          order: "normal",
          generation: "queue-generation-70",
        });
        app._syncSessionMessageStore(st);
        st._loaded = true;
        st._installedCanonicalCount = st.messages.length;
        st._seenUpdated = 70;
        st.runtimeUiRevision = "queue-ui-revision-70";
        st._queueRevision = 71;
        st.messagesReady = true;
        st.messagesLoading = false;
        st.atBottom = true;

        // Only queue turn 1 reached the browser live. Turn 2 has identical
        // prompt/reply prose, so a weak newest-first matcher used to steal
        // turn 1's mounted key and make Alpine's keyed mover throw `.after`.
        const firstLiveUser = app._appendLiveMessage(st, {
          role: "user", text: "test", _turnRoot: true, _turnId: "turn-1",
        });
        const firstLiveAssistant = app._appendLiveMessage(st, {
          role: "assistant", text: "TEST REPLY",
          forkUuid: "turn-1-assistant", turn_status: "completed",
          model: "e2e-model", ts: 70_000, elapsed: 1,
        });
        st.pendingQueue = arg.pending.map(item => ({
          id: item.id,
          text: item.text,
          displayText: item.text,
          pendingQuotes: [],
          image_ids: "",
          hasAttach: false,
          images: [], docs: [], expiredCount: 0,
          pendingImages: [], pendingDocs: [],
          delivery: "queue",
          deliveryStatus: "queued",
          commandUuid: `${item.id}-command`,
          targetTurnId: "turn-1",
          enqueuedAt: item.enqueuedAt,
        }));
        st._queuePaused = false;
        app.currentId = sid;
        app.mobileTab = "chat";
        app._activateTabState(sid);

        const reports = [];
        const originalReport = app._reportHistoryLoadPerf;
        app._reportHistoryLoadPerf = fields => reports.push({...fields});
        try {
          await new Promise(resolve => app.$nextTick(
            () => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const before = {
            messageCount: st.messages.length,
            tail: st.messages.slice(-2).map(message => ({
              role: message.role,
              text: message.text,
              uuid: message.uuid || "",
            })),
            pending: st.pendingQueue.map(item => item.text),
            queueCards: document.querySelectorAll(".msg.user.queued").length,
          };

          const loaded = await app._runQueueAttach(sid, st, {tries: 2});
          for (let index = 0; index < 100; index++) {
            const sync = st.sessionSync;
            if (!st._draining && !sync.inFlight && !sync.timer
                && !Object.keys(sync.pending || {}).length) break;
            await new Promise(resolve => setTimeout(resolve, 10));
          }
          await new Promise(resolve => app.$nextTick(
            () => requestAnimationFrame(() => requestAnimationFrame(resolve))));

          const pane = document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(sid)}"]`);
          const rendered = app.paneMessages(sid);
          const nodes = Array.from(pane.querySelectorAll(
            ":scope > .msg[data-message-key]"));
          const firstCanonicalAssistant = st.messages.find(
            message => message.uuid === "turn-1-assistant");
          const secondCanonicalAssistant = st.messages.find(
            message => message.uuid === "turn-2-assistant");
          return {
            before,
            loaded,
            reports,
            watermarks: {
              seenUpdated: st._seenUpdated,
              installedCanonicalCount: st._installedCanonicalCount,
              runtimeUiRevision: st.runtimeUiRevision,
              queueRevision: st._queueRevision,
              total: st.messageRange.total,
              generation: st.messageRange.generation,
            },
            canonicalUuids: st.messages.map(message => message.uuid || ""),
            stateKeys: rendered.map(message => message._k),
            stateUuids: rendered.map(message => message.uuid || ""),
            domKeys: nodes.map(node => node.dataset.messageKey),
            domUuids: nodes.map(node => node.dataset.uuid || ""),
            identity: {
              firstAssistantPreserved:
                firstCanonicalAssistant === firstLiveAssistant,
              firstAssistantKey: firstCanonicalAssistant?._k || "",
              firstLiveAssistantKey: firstLiveAssistant?._k || "",
              secondAssistantKey: secondCanonicalAssistant?._k || "",
              firstLiveUserOwnerUuid: st.messages.find(
                message => message === firstLiveUser)?.uuid || "",
            },
            queue: {
              pendingCount: st.pendingQueue.length,
              displayCount: app.queueDisplayItems(st).length,
              cardCount: document.querySelectorAll(".msg.user.queued").length,
              paused: st._queuePaused,
            },
            settled: {
              draining: st._draining,
              syncInFlight: !!st.sessionSync.inFlight,
              syncTimer: !!st.sessionSync.timer,
              syncPending: Object.keys(st.sessionSync.pending || {}).length,
              ready: st.messagesReady,
              loading: st.messagesLoading,
            },
          };
        } finally {
          app._reportHistoryLoadPerf = originalReport;
          app._disposeSessionSync(st);
        }
        """,
        {
            "sid": sid,
            "baseMessages": canonical_messages[:99],
            "pending": [
                {"id": "queued-turn-2", "text": "test", "enqueuedAt": 71},
                {"id": "queued-turn-3", "text": "stop", "enqueuedAt": 72},
            ],
        },
    )

    assert result["before"] == {
        "messageCount": live_canonical_count,
        "tail": [
            {"role": "user", "text": "test", "uuid": ""},
            {"role": "assistant", "text": "TEST REPLY", "uuid": ""},
        ],
        "pending": ["test", "stop"],
        "queueCards": 2,
    }, result
    assert result["loaded"] is True, result
    assert len(history_requests) == 1, history_requests
    assert len(queue_requests) >= 2, queue_requests
    assert len(active_requests) >= 2, active_requests
    assert result["watermarks"] == {
        "seenUpdated": 72,
        "installedCanonicalCount": 105,
        "runtimeUiRevision": "queue-ui-revision-72",
        "queueRevision": 72,
        "total": 105,
        "generation": "queue-generation-72",
    }, result
    assert result["canonicalUuids"] == [
        message["uuid"] for message in canonical_messages
    ], result
    assert len(result["stateKeys"]) == len(set(result["stateKeys"])), result
    assert len(result["domKeys"]) == len(set(result["domKeys"])), result
    assert result["domKeys"] == result["stateKeys"], result
    assert result["domUuids"] == result["stateUuids"], result
    assert result["identity"]["firstAssistantPreserved"] is True, result
    assert (
        result["identity"]["firstAssistantKey"]
        == result["identity"]["firstLiveAssistantKey"]
    ), result
    assert (
        result["identity"]["secondAssistantKey"]
        != result["identity"]["firstLiveAssistantKey"]
    ), result
    assert result["identity"]["firstLiveUserOwnerUuid"] != "turn-2-user", result
    assert result["queue"] == {
        "pendingCount": 0,
        "displayCount": 0,
        "cardCount": 0,
        "paused": False,
    }, result
    assert result["settled"] == {
        "draining": False,
        "syncInFlight": False,
        "syncTimer": False,
        "syncPending": 0,
        "ready": True,
        "loading": False,
    }, result
    assert result["reports"] and result["reports"][0]["status"] == "ok", result
    _assert_no_browser_errors(page, errors)


def test_stale_older_response_cannot_overwrite_new_canonical_tail(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = "history-owner-race";
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.sessions = [{id: sid, name: "History owner race", message_count: 60}];
          app.openTabIds = [sid];
          app.tabState = {};
          app.currentId = sid;
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st.atBottom = false;
          st.messages.push(...Array.from({length: 20}, (_, i) => ({
            role: "user", text: `old ${i + 20}`, uuid: `old-${i + 20}`,
            _k: `${sid}:uuid:old-${i + 20}`,
          })));
          Object.assign(st.messageRange, {
            visibleStart: 0, visibleEnd: 20, offset: 20, total: 40,
            preTotal: 0, order: "normal", generation: "G1",
          });
          app._activateTabState(sid);

          const originalFetch = window.fetch;
          let releaseOlder;
          window.fetch = async url => {
            const value = String(url);
            if (value.includes("offset=0") && value.includes("history_generation=G1")) {
              return await new Promise(resolve => {
                releaseOlder = () => resolve(new Response(JSON.stringify({
                  messages: Array.from({length: 20}, (_, i) => ({
                    role: "user", text: `stale ${i}`, uuid: `stale-${i}`,
                  })),
                  offset: 0, total: 40, pre_total: 0,
                  history_order: "normal", history_generation: "G1",
                }), {status: 200, headers: {"content-type": "application/json"}}));
              });
            }
            if (value.includes(`/api/chat/sessions/${sid}?tail=`)) {
              return new Response(JSON.stringify({
                id: sid, name: "History owner race", model: "e2e-model",
                permission: "bypassPermissions", thinking: true,
                messages: Array.from({length: 20}, (_, i) => ({
                  role: i === 19 ? "assistant" : "user",
                  text: `fresh ${i + 40}`, uuid: `fresh-${i + 40}`,
                  turn_status: i === 19 ? "completed" : "",
                })),
                offset: 40, total: 60, message_count: 60,
                pre_total: 0, history_order: "normal",
                history_generation: "G2", runtime_ui_revision: "rev-2",
                updated_at: 2,
              }), {status: 200, headers: {"content-type": "application/json"}});
            }
            return originalFetch(url);
          };

          try {
            const older = app._fetchOlderWindow(sid);
            while (!releaseOlder) await new Promise(resolve => setTimeout(resolve, 0));
            const canonical = await app.loadSession(sid, {
              quiet: true, followTail: true, probeActive: false,
            });
            releaseOlder();
            const olderCount = await older;
            return {
              canonical,
              olderCount,
              generation: st.messageRange.generation,
              offset: st.messageRange.offset,
              total: st.messageRange.total,
              first: st.messages[0]?.uuid,
              last: st.messages[st.messages.length - 1]?.uuid,
              atBottom: st.atBottom,
              hasLater: app.hasLaterMessages(sid),
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )

    assert result == {
        "canonical": True,
        "olderCount": 0,
        "generation": "G2",
        "offset": 40,
        "total": 60,
        "first": "fresh-40",
        "last": "fresh-59",
        "atBottom": True,
        "hasLater": False,
    }
    _assert_no_browser_errors(page, errors)


def test_completed_history_defers_to_optimistic_and_remote_successors(
    page: Page, backend_url, auth_token,
):
    """A's late canonical sync never removes a newly admitted B prompt."""
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = 'completed-history-successor-race';
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.sessions = [{id: sid, name: 'Successor race', message_count: 2}];
          app.openTabIds = [sid];
          app.tabState = {};
          app.currentId = sid;
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st.atBottom = true;
          st.messages.push(
            {role: 'user', text: 'TURN_A_PROMPT', uuid: 'turn-a-user',
             _turnRoot: true, _k: `${sid}:uuid:turn-a-user`},
            {role: 'assistant', text: 'TURN_A_REPLY', uuid: 'turn-a-final',
             turn_status: 'completed', _k: `${sid}:uuid:turn-a-final`},
          );
          Object.assign(st.messageRange, {
            visibleStart: 0, visibleEnd: 2, offset: 0, total: 2,
            preTotal: 0, order: 'normal', generation: 'race-g1',
          });
          app._activateTabState(sid);

          const originalFetch = window.fetch;
          const originalDeadline = app._fetchWithDeadline;
          const originalLoad = app.loadSession;
          let releaseHistory = null;
          let guardedFetches = 0;
          try {
            window.fetch = async input => {
              const url = String(input);
              if (!url.includes(`/api/chat/sessions/${sid}?tail=`)) {
                return originalFetch(input);
              }
              return await new Promise(resolve => {
                releaseHistory = () => resolve(new Response(JSON.stringify({
                  id: sid, name: 'Successor race', model: 'e2e-model',
                  permission: 'bypassPermissions', thinking: true,
                  messages: [
                    {role: 'user', text: 'TURN_A_PROMPT', uuid: 'turn-a-user',
                     _turnRoot: true},
                    {role: 'assistant', text: 'TURN_A_REPLY', uuid: 'turn-a-final',
                     turn_status: 'completed'},
                  ],
                  offset: 0, total: 2, message_count: 2,
                  pre_total: 0, history_order: 'normal',
                  history_generation: 'race-g2', updated_at: 2,
                }), {status: 200, headers: {'content-type': 'application/json'}}));
              });
            };
            const pendingLoad = app.loadSession(sid, {
              quiet: true, probeActive: false, followTail: true,
            });
            while (!releaseHistory) await new Promise(resolve => setTimeout(resolve, 0));

            st._composerSubmitToken = 'turn-b-composer-claim';
            const optimistic = app._appendLiveMessage(st, {
              role: 'user', text: 'TURN_B_OPTIMISTIC',
              _turnRoot: true, _admissionPending: true,
            });
            releaseHistory();
            const staleLoadResult = await pendingLoad;
            await new Promise(resolve => app.$nextTick(resolve));
            const pane = document.querySelector(
              `.msg-pane[data-tid="${CSS.escape(sid)}"]`);

            // A completion retry that starts after the claim is visible must
            // not even issue a canonical read.
            app._fetchWithDeadline = async () => {
              guardedFetches += 1;
              throw new Error('guarded completion fetched unexpectedly');
            };
            st._pendingCompletedTurnSync = {
              expectedAssistantUuid: 'turn-a-final',
              completedTurnId: 'turn-a', attempt: 30,
            };
            const guardedCompletion = await app._runCompletedTurnSync(sid, st, {
              expectedAssistantUuid: 'turn-a-final',
              completedTurnId: 'turn-a', attempt: 30,
            });

            // A remote successor can win before its mux channel reaches this
            // browser. Identity-aware /active must defer A's replacement too.
            const remoteSid = 'completed-history-remote-successor';
            const remote = app._ensureTabState(remoteSid);
            remote._loaded = true;
            let remoteLoads = 0;
            app._fetchWithDeadline = async url => new Response(JSON.stringify(
              String(url).endsWith('/active')
                ? {active: true, background: false, turn_id: 'turn-b'}
                : {messages: [
                    {role: 'user', text: 'A', uuid: 'remote-a-user',
                     _turnRoot: true},
                    {role: 'assistant', text: 'A done', uuid: 'remote-a-final'},
                  ]},
            ), {status: 200, headers: {'content-type': 'application/json'}});
            app.loadSession = async () => { remoteLoads += 1; return true; };
            const remoteCompletion = await app._runCompletedTurnSync(
              remoteSid, remote, {
                expectedAssistantUuid: 'remote-a-final',
                completedTurnId: 'turn-a', attempt: 30,
              },
            );

            // Non-zero visible windows use absolute coordinates without
            // allocating a fresh slice for every Alpine binding.
            const indexSid = 'pane-index-window';
            const indexState = app._ensureTabState(indexSid);
            indexState.messages = Array.from({length: 800}, (_, index) => ({
              role: 'assistant', text: String(index),
              _k: `${indexSid}:${index}`,
            }));
            Object.assign(indexState.messageRange, {
              visibleStart: 700, visibleEnd: 800, offset: 0, total: 800,
              preTotal: 0, order: 'normal', generation: 'index-g1',
            });
            const indexedMessage = indexState.messages[750];
            const firstIndex = app.paneMessageIndex(indexSid, indexedMessage);
            indexState.messages.splice(720, 0, {
              role: 'thinking', text: 'inserted', _k: `${indexSid}:inserted`,
            });
            indexState.messageRange.visibleEnd = 801;
            const shiftedIndex = app.paneMessageIndex(indexSid, indexedMessage);
            indexState.messageRange.visibleStart = 710;
            const movedWindowIndex = app.paneMessageIndex(indexSid, indexedMessage);

            const optimisticKey = optimistic._k;
            return {
              staleLoadResult,
              optimisticRetained: st.messages.includes(optimistic),
              optimisticKey,
              optimisticVisible: !!pane?.querySelector(
                `.msg[data-message-key="${CSS.escape(optimisticKey)}"]`),
              guardedCompletion,
              guardedFetches,
              pendingCompletionRetained: !!st._pendingCompletedTurnSync,
              remoteCompletion,
              remoteLoads,
              firstIndex,
              shiftedIndex,
              movedWindowIndex,
            };
          } finally {
            window.fetch = originalFetch;
            app._fetchWithDeadline = originalDeadline;
            app.loadSession = originalLoad;
            st._composerSubmitToken = null;
          }
        }"""
    )

    assert result == {
        "staleLoadResult": False,
        "optimisticRetained": True,
        "optimisticKey": "completed-history-successor-race:live:1",
        "optimisticVisible": True,
        "guardedCompletion": False,
        "guardedFetches": 0,
        "pendingCompletionRetained": True,
        "remoteCompletion": False,
        "remoteLoads": 0,
        "firstIndex": 50,
        "shiftedIndex": 51,
        "movedWindowIndex": 41,
    }
    _assert_no_browser_errors(page, errors)


def test_send_from_older_window_returns_to_latest_with_composer_claim(
    page: Page, backend_url, auth_token,
):
    """Send's own composer claim must not block its return-to-tail load."""
    errors = _capture_browser_errors(page)
    _install_fake_event_source(page)
    sid = "send-from-older-window"
    prompt = "SEND_FROM_OLDER_WINDOW"
    canonical_messages = [
        {
            "role": "user", "text": "OLDER_PROMPT", "uuid": "older-user",
            "_turnRoot": True,
        },
        {
            "role": "assistant", "text": "OLDER_REPLY", "uuid": "older-reply",
            "turn_status": "completed",
        },
        {
            "role": "user", "text": "LATEST_PROMPT", "uuid": "latest-user",
            "_turnRoot": True,
        },
        {
            "role": "assistant", "text": "LATEST_REPLY", "uuid": "latest-reply",
            "turn_status": "completed",
        },
    ]
    requests = _route_windowed_session(page, sid, canonical_messages)
    page.route(
        "**/api/chat/stream/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ticket":"older-window-send-ticket"}',
        ),
    )
    _login(page, backend_url, auth_token)
    _bootstrap_session_for_real_load(page, sid, "Send from older window")
    _app_eval(
        page,
        """
        app._ensureSessionRegistered = async () => true;
        app._awaitRuntimeSettingPatches = async () => true;
        app._confirmSessionBusy = async () => false;
        const st = app._ensureTabState(arg.sid);
        st.messages.splice(0, st.messages.length, ...app._historyEnvelopes(arg.sid, [
          {role: "user", text: "OLDER_PROMPT", uuid: "older-user", _turnRoot: true},
          {role: "assistant", text: "OLDER_REPLY", uuid: "older-reply",
           turn_status: "completed"},
        ]));
        Object.assign(st.messageRange, {
          visibleStart: 0, visibleEnd: 2, offset: 0, total: 4,
          preTotal: 0, order: "normal", generation: "older-window-g1",
        });
        st._loaded = true;
        st.messagesReady = true;
        st.messagesLoading = false;
        st.atBottom = false;
        st.draft.input = arg.prompt;
        app._activateTabState(arg.sid);
        return app.hasLaterMessages(arg.sid);
        """,
        {"sid": sid, "prompt": prompt},
    )

    send_result = _app_eval(
        page,
        """
        const result = await app.send();
        await new Promise(resolve => app.$nextTick(resolve));
        const st = app._ensureTabState(arg.sid);
        return {
          didNotFail: result !== false,
          streaming: st.streaming,
          composerClaim: st._composerSubmitToken,
          draft: st.draft.input,
          hasLater: app.hasLaterMessages(arg.sid),
          latestPresent: st.messages.some(m => m.uuid === "latest-reply"),
          promptCount: st.messages.filter(m => m.role === "user"
            && m.text === arg.prompt).length,
          promptVisible: !!document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(arg.sid)}"] .msg.user`
          ) && document.querySelector(
            `.msg-pane[data-tid="${CSS.escape(arg.sid)}"]`
          ).textContent.includes(arg.prompt),
        };
        """,
        {"sid": sid, "prompt": prompt},
    )

    assert requests, "send did not load the canonical latest tail"
    assert send_result == {
        "didNotFail": True,
        "streaming": True,
        "composerClaim": None,
        "draft": "",
        "hasLater": False,
        "latestPresent": True,
        "promptCount": 1,
        "promptVisible": True,
    }
    _assert_no_browser_errors(page, errors)


def test_newer_tail_request_wins_when_same_revision_responses_arrive_out_of_order(
    page: Page, backend_url, auth_token,
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)

    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = "tail-replace-owner-race";
          app.refreshSessions = async () => {};
          app._fetchTabUsage = async () => {};
          app._scheduleIdlePreload = () => {};
          app.sessions = [{id: sid, name: "Tail owner race", message_count: 2}];
          app.openTabIds = [sid];
          app.tabState = {};
          app.currentId = sid;
          const st = app._ensureTabState(sid);
          st._loaded = true;
          st.atBottom = true;
          st.messages.push({
            role: "user", text: "base", uuid: "base", _k: `${sid}:uuid:base`,
          });
          Object.assign(st.messageRange, {
            visibleStart: 0, visibleEnd: 1, offset: 0, total: 1,
            preTotal: 0, order: "normal", generation: "G0",
          });
          app._activateTabState(sid);

          const originalFetch = window.fetch;
          let requestCount = 0;
          let releaseFirst;
          const responseFor = (generation, suffix, updated) => new Response(
            JSON.stringify({
              id: sid, name: "Tail owner race", model: "e2e-model",
              permission: "bypassPermissions", thinking: true,
              messages: [
                {role: "user", text: "base", uuid: "base"},
                {role: "assistant", text: suffix, uuid: suffix,
                 turn_status: "completed"},
              ],
              offset: 0, total: 2, message_count: 2,
              pre_total: 0, history_order: "normal",
              history_generation: generation,
              // Deliberately identical: ownership must not depend on this field.
              runtime_ui_revision: "same-revision",
              updated_at: updated,
            }),
            {status: 200, headers: {"content-type": "application/json"}},
          );
          window.fetch = async url => {
            if (!String(url).includes(`/api/chat/sessions/${sid}?tail=`)) {
              return originalFetch(url);
            }
            requestCount++;
            if (requestCount === 1) {
              return await new Promise(resolve => {
                releaseFirst = () => resolve(responseFor("G1", "stale-reply", 1));
              });
            }
            return responseFor("G2", "fresh-reply", 2);
          };

          try {
            const first = app.loadSession(sid, {
              quiet: true, followTail: true, probeActive: false,
            });
            while (!releaseFirst) await new Promise(resolve => setTimeout(resolve, 0));
            const secondResult = await app.loadSession(sid, {
              quiet: true, followTail: true, probeActive: false,
            });
            releaseFirst();
            const firstResult = await first;
            return {
              firstResult,
              secondResult,
              generation: st.messageRange.generation,
              last: st.messages[st.messages.length - 1]?.uuid,
              total: st.messageRange.total,
              atBottom: st.atBottom,
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )

    assert result == {
        "firstResult": False,
        "secondResult": True,
        "generation": "G2",
        "last": "fresh-reply",
        "total": 2,
        "atBottom": True,
    }
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
        app._touchTranscriptPane(target);
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
              ready: app._ensureTabState(app.currentId).messagesReady,
              loading: app._ensureTabState(app.currentId).messagesLoading,
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
    first_visible = next(i for i, frame in enumerate(result["frames"]) if frame["visibleCount"] > 0)
    assert all(frame["visibleCount"] > 0 for frame in result["frames"][first_visible:]), result
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
          app._ensureTabState(app.currentId).messagesReady = true;
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

          st.activeTurnId = 'authoritative-stop-turn';
          st._stoppingTurnId = st.activeTurnId;
          st.streaming = false;
          st.draft.input = 'SEND DURING STOP';
          app._activateComposerState(sid);
          const disabledReason = app.composerDisabledReason(sid);
          const sendResult = await app.send();
          return {
            afterEdit,
            disabledReason,
            sendResult,
            stoppingDraft: st.draft.input,
            pendingAfterStop: st.pendingQueue.length,
            composerClaim: st._composerSubmitToken,
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
    assert result["disabledReason"] in {
        "Stopping the previous turn", "正在中断上一条任务",
    }
    assert result["sendResult"] is False
    assert result["stoppingDraft"] == "SEND DURING STOP"
    assert result["pendingAfterStop"] == 1
    assert result["composerClaim"] is None


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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
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
              ready: app._ensureTabState(app.currentId).messagesReady,
              loading: app._ensureTabState(app.currentId).messagesLoading,
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
              ready: app._ensureTabState(app.currentId).messagesReady,
              loading: app._ensureTabState(app.currentId).messagesLoading,
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
          const originalLoad = app.loadSession;
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
            app.loadSession = async sid => {
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
              while ((st.sessionSync.inheritedSourceSid || st.sessionSync.inFlight) && performance.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 5));
              }
              const spec = specs.get(child);
              outcomes.push({
                name: item.name,
                unread: !!st.unread,
                revision: st.runtimeUiRevision,
                loads: spec.loads,
              });
              app._disposeSessionSync(st);
              delete app.tabState[child];
              specs.delete(child);
            }
            return outcomes;
          } finally {
            window.fetch = originalFetch;
            app.loadSession = originalLoad;
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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
        app._ensureTabState(app.currentId).atBottom = true;
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
              ready: app._ensureTabState(app.currentId).messagesReady,
              loading: app._ensureTabState(app.currentId).messagesLoading,
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
          return app._ensureTabState(app.currentId).messagesReady === true
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
          return app._ensureTabState(app.currentId).messagesReady === true
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
            ready: document.querySelector("#app")._x_dataStack[0].activeSessionPane().messagesReady,
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
    assert _app_eval(page, "return app._ensureTabState(app.currentId).messagesReady === true && !app._ensureTabState(app.currentId).messagesLoading;") is True

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
    input_box.evaluate("el => { el.disabled = false; }")
    page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].onChatInputFocus()"
    )
    input_box.focus()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const menuLayer = document.querySelector('.activity-move-layer');
          const backdrop = document.querySelector(
            '.modal-backdrop .activity-modal')?.parentElement;
          return !app.activity.show && !app.activity.moveMenu.show
            && getComputedStyle(menuLayer).display === 'none'
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
            app._ensureTabState(app.currentId).streaming = false;
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
            app._ensureTabState(app.currentId).streaming = true;
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


def test_background_stream_buffers_token_rate_presentation_until_activation(
    page: Page, backend_url: str, auth_token: str,
):
    """Inactive SSE streams retain complete state without token-rate paints."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 1440, "height": 900})
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
    ids = ["background-buffer-a", "background-buffer-b"]
    _app_eval(
        page,
        """
        app.refreshSessions = async () => {};
        window.__backgroundUsageFetches = [];
        app._fetchTabUsage = async sid => {
          window.__backgroundUsageFetches.push(sid);
        };
        app._scheduleIdlePreload = () => {};
        app.availableModels = [{
          model: "e2e-model", label: "E2E model", group: "e2e",
          supports_thinking: true,
        }];
        app.model = app.defaultModel = "e2e-model";
        app.sessions = arg.map((id, index) => ({
          id, name: `Buffered ${index}`, updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }));
        app.openTabIds = arg.slice();
        app.tabState = {};
        for (const id of arg) {
          const st = app._blankTabState();
          st._loaded = true;
          st.messagesReady = true;
          st.messagesLoading = false;
          st.atBottom = true;
          app.tabState[id] = st;
        }
        app.currentId = arg[0];
        app._activateTabState(arg[0]);
        app.input = "start stream a";
        return true;
        """,
        ids,
    )
    _app_eval(page, "app.send(); return true;")
    page.wait_for_function("() => window.__fakeChatStreams().length === 1")
    _app_eval(
        page,
        """
        app.currentId = arg;
        await app.switchSession();
        app.input = "start stream b";
        app.send();
        return true;
        """,
        ids[1],
    )
    page.wait_for_function("() => window.__fakeChatStreams().length === 2")
    page.evaluate("() => { window.__backgroundUsageFetches.length = 0; }")

    page.evaluate(
        """() => {
          for (let i = 0; i < 200; i++) {
            window.__emitSseAt(0, "text", { text: `BG_TEXT_${i} ` });
          }
          for (let i = 0; i < 200; i++) {
            window.__emitSseAt(0, "thinking", { text: `BG_THINK_${i} ` });
          }
        }"""
    )
    before = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const thinking = [...st.messages].reverse().find(m => m.role === "thinking");
        return {
          currentId: app.currentId,
          streaming: st.streaming,
          thinkingText: thinking ? thinking.text : null,
          plainPaints: st._streamPlainRenderCount,
          usageFetches: window.__backgroundUsageFetches.slice(),
        };
        """,
        ids[0],
    )
    assert before["currentId"] == ids[1]
    assert before["streaming"] is True
    assert before["thinkingText"] == ""
    assert before["plainPaints"] == 0
    assert ids[0] not in before["usageFetches"]

    terminal = page.evaluate(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const originalRender = app._mdRenderUncached.bind(app);
          window.__backgroundRichCalls = 0;
          app._mdRenderUncached = (text, opts) => {
            if ((text || '').includes('BG_FINAL_RICH_MARKER')) {
              window.__backgroundRichCalls += 1;
            }
            return originalRender(text, opts);
          };
          const finalText = '**BG_FINAL_RICH_MARKER**\\n\\n'
            + 'background payload '.repeat(7500);
          window.__emitSseAt(0, 'text', {text: finalText});
          const started = performance.now();
          window.__emitSseAt(0, 'done', {
            total_cost_usd: 0.001,
            session_usage: {
              context_used_pct: 10,
              context_used: 1000,
              context_limit: 100000,
            },
          });
          const st = app._ensureTabState(sid);
          const last = [...st.messages].reverse().find(m =>
            m.role === 'assistant' && (m.text || '').includes('BG_FINAL_RICH_MARKER'));
          return {
            dispatchMs: performance.now() - started,
            streaming: st.streaming,
            richCalls: window.__backgroundRichCalls,
            plain: last?._streamPlain,
            deferred: last?._deferredRichReady,
            htmlLength: (last?.html || '').length,
            textLength: (last?.text || '').length,
          };
        }""",
        arg=[ids[0]],
    )
    assert terminal["streaming"] is False
    assert terminal["richCalls"] == 0
    assert terminal["plain"] is True
    assert terminal["deferred"] is True
    assert terminal["htmlLength"] == 0
    assert terminal["textLength"] >= 120_000
    assert terminal["dispatchMs"] < 250

    _app_eval(
        page,
        """
        app.currentId = arg;
        await app.switchSession();
        return true;
        """,
        ids[0],
    )
    page.wait_for_function(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(sid);
          return [...st.messages].reverse().some(m =>
            m.role === 'assistant'
            && m.text.includes('BG_FINAL_RICH_MARKER')
            && m._streamPlain === false
            && m._deferredRichReady === false
            && m.html.includes('BG_FINAL_RICH_MARKER'));
        }""",
        arg=ids[0],
    )
    activation = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        const thinking = [...st.messages].reverse().find(m => m.role === "thinking");
        return {
          currentId: app.currentId,
          thinkingText: thinking?.text || "",
          richCalls: window.__backgroundRichCalls,
        };
        """,
        ids[0],
    )
    assert activation["currentId"] == ids[0]
    assert "BG_THINK_199" in activation["thinkingText"]
    assert activation["richCalls"] == 1
    assert _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return st.messages.some(m => m.role === "assistant"
          && m.text.includes("BG_TEXT_199"));
        """,
        ids[0],
    )
    _assert_no_browser_errors(page, errors)


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
        app._ensureTabState(app.currentId).messagesReady = true;
        app._ensureTabState(app.currentId).messagesLoading = false;
        app.mobileTab = "chat";
        app.input = "stream a long deterministic answer";
        app._ensureTabState(app.currentId).atBottom = true;
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
          const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming === true
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
        const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
        return {
          streaming: app._ensureTabState(app.currentId).streaming,
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
          const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming === true
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
          const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming === true && last
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
          const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
          return app._ensureTabState(app.currentId).streaming === false
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
    assert _app_eval(page, "return app._ensureTabState(app.currentId).messages.length;") <= 50
    assert _app_eval(
        page,
        """
        const roles = app._ensureTabState(app.currentId).messages.map(m => m.role);
        const last = app._ensureTabState(app.currentId).messages[app._ensureTabState(app.currentId).messages.length - 1];
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
