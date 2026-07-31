"""Browser stress checks for long chat rendering.

These tests deliberately run against the real frontend bundle and Alpine DOM,
but keep the model/provider path deterministic by injecting controlled session
state or a fake EventSource stream. They cover the long-history and long-stream
regression classes that static lint cannot see.
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

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
        app._residentTabIds = [arg.sid];
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg.sid);
        app._promoteResident(arg.sid);
        return true;
        """,
        {"sid": sid, "name": name},
    )


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
    """Switch repeatedly between long resident chat panes on a mobile viewport."""
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
        app._MAX_RESIDENT_PANES = 2;
        app._residentTabIds = sessionIds.slice(0, 4);
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
          app.tabState[id] = st;
          app._ensureTabState(id);
          app._capLiveMessages(st);
        }
        app.currentId = sessionIds[0];
        app.messagesReady = true;
        app.messagesLoading = false;
        app.mobileTab = "chat";
        app._activateTabState(app.currentId);
        app._promoteResident(app.currentId);
        app.$nextTick(() => app.scrollToBottom(true));
        return true;
        """,
    )
    mounted_cap = _app_eval(page, "return app._mountedMessageCap();")

    page.wait_for_function(
        """mountedCap => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"))
            .filter(p => getComputedStyle(p).display !== "none");
          return panes.length === 1
            && panes[0].querySelectorAll(".msg").length === mountedCap;
        }""",
        arg=mounted_cap,
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
            app._promoteResident(arg);
            app.$nextTick(() => app.scrollToBottom(true));
            """,
            sid,
        )
        expected_tail = f"history {sid.rsplit('-', 1)[1]}:89"
        try:
            page.wait_for_function(
                """({ expected, mountedCap }) => {
                  const panes = Array.from(document.querySelectorAll(".msg-pane"))
                    .filter(p => getComputedStyle(p).display !== "none");
                  return panes.some(p => p.textContent.includes(expected)
                    && p.querySelectorAll(".msg").length === mountedCap);
                }""",
                arg={"expected": expected_tail, "mountedCap": mounted_cap},
                timeout=5000,
            )
        except TimeoutError as exc:
            diag = page.evaluate(
                """() => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  return {
                    currentId: app.currentId,
                    resident: app.residentPaneIds(),
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
        assert snap["msgCount"] <= mounted_cap
        assert expected_tail in snap["text"]
        assert page.locator(".msg-pane").count() <= 1
        assert _app_eval(page, "return app.residentPaneIds().length;") <= 1

    _assert_no_browser_errors(page, errors)


def test_desktop_warm_session_switch_keeps_panes_and_composer_stable(
    page: Page, backend_url: str, auth_token: str,
):
    """Desktop prioritizes instant warm switches without footer/layout jumps."""
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
          st.messagesReady = true;
          st.messagesLoading = false;
          st.atBottom = true;
          app.tabState[id] = st;
          app._ensureTabState(id);
        }
        app.currentId = arg.ids[0];
        app._residentTabIds = arg.ids.slice();
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(app.currentId);
        app._promoteResident(app.currentId);
        return true;
        """,
        payload,
    )
    page.wait_for_function(
        """() => {
          const panes = Array.from(document.querySelectorAll(".msg-pane"));
          const visible = panes.filter(
            pane => getComputedStyle(pane).display !== "none");
          return panes.length === 4
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
              resident: app.residentPaneIds().length,
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
    # interaction is what users feel across repeated warm switches.
    assert elapsed[len(elapsed) // 2] < 700, switches
    assert max(elapsed) < 1500, switches
    assert all(row["resident"] == 4 for row in switches)
    assert all(row["panes"] == 4 and row["visible"] == 1 for row in switches)
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
          earlier: st._earlierMessages.length,
          later: st._laterMessages.length,
          loadedOffset: st._loadedOffset,
          total: st._total,
          hasMore: st._hasMoreHistory,
          resident: app.residentPaneIds().length,
          ready: app.messagesReady,
          bodyText: document.querySelector(".chat-body")?.textContent || "",
        };
        """,
        sid,
    )
    assert requests and requests[0]["tail"] == 75
    assert state["messages"] <= 60
    assert state["loadedOffset"] == 105
    assert state["total"] == 180
    assert state["hasMore"] is True
    assert state["resident"] <= 1
    assert "WINDOW_MSG_179" in state["bodyText"]
    assert "WINDOW_MSG_000" not in state["bodyText"]
    assert page.locator(".msg-pane").count() <= 1
    assert page.locator(".msg-pane:visible .msg").count() <= 75

    # Traverse all the way through a history larger than the memory cap. The
    # window must slide backward (evicting far-future bubbles) until message 0
    # is mounted; the old implementation stopped forever at the first cap.
    for _ in range(24):
        _app_eval(page, "return app.loadEarlierMessages(arg);", sid)
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
          earlier: st._earlierMessages.length,
          later: st._laterMessages.length,
          loadedOffset: st._loadedOffset,
          total: st._total,
          hasMore: st._hasMoreHistory,
          hasServerLater: st._hasServerLater,
          cached: st.messages.length + st._earlierMessages.length + st._laterMessages.length,
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
    assert final_state["messages"] <= 60
    assert final_state["cached"] <= 120
    assert final_state["later"] > 0
    assert final_state["hasServerLater"] is True
    latest_after_load_earlier = _app_eval(
        page,
        """
        const st = app._ensureTabState(arg);
        return {
          latestInMessages: st.messages.some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInLater: st._laterMessages.some(m => (m.text || "").includes("WINDOW_MSG_179")),
          latestInDom: document.querySelector(".chat-body")?.textContent.includes("WINDOW_MSG_179"),
          hasServerLater: st._hasServerLater,
          ready: st.messagesReady,
        };
        """,
        sid,
    )
    assert latest_after_load_earlier == {
        "latestInMessages": False,
        "latestInLater": False,
        "latestInDom": False,
        "hasServerLater": True,
        "ready": True,
    }
    _app_eval(page, "app.returnToLatest(arg); return true;", sid)
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.msg-pane'))
          .filter(p => getComputedStyle(p).display !== 'none')
          .some(p => p.textContent.includes('WINDOW_MSG_179'))""",
        timeout=5000,
    )
    assert _app_eval(page, "return app._ensureTabState(arg)._laterMessages.length;", sid) == 0
    assert page.locator(".msg-pane").count() <= 1
    assert page.locator(".msg-pane:visible .msg").count() <= 60

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
        st.historyGeneration = "gen-old";
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
          earlier: st._earlierMessages.length,
          later: st._laterMessages.length,
          targetMounted: st.messages.some(m => m.uuid === "around-target"),
          order: st._historyOrder,
          hasServerLater: st._hasServerLater,
        };
        """,
        sid,
    )
    assert around_state["mounted"] <= 60
    assert around_state["targetMounted"] is True
    assert around_state["order"] == "full"
    assert around_state["hasServerLater"] is True
    assert len([call for call in calls if "around_uuid" in call]) == 2
    assert len([call for call in calls if "tail" in call]) == 1

    returned = _app_eval(page, "return app.returnToLatest(arg);", sid)
    assert returned is True
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          return st._historyOrder === 'normal' && !st._hasServerLater
            && st.messages.some(m => (m.text || '').includes('CANONICAL_LATEST_VISIBLE'));
        }""",
        timeout=10000,
    )
    assert len([call for call in calls if "tail" in call]) == 2
    assert page.locator(".msg-pane:visible .msg").count() <= 60
    _assert_no_browser_errors(page, errors)


def test_bidirectional_cap_preserves_keyed_scroll_anchor(
    page: Page, backend_url, auth_token,
):
    """Top/bottom eviction must not move the surviving reading anchor."""
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    sid = "perf-bidirectional-cap"
    _app_eval(
        page,
        """
        const sid = arg;
        app.refreshSessions = async () => {};
        app._fetchTabUsage = async () => {};
        app.sessions = [{
          id: sid, name: "Perf bidirectional cap", updated_at: Date.now() / 1000,
          model: "e2e-model", permission: "bypassPermissions", thinking: true,
        }];
        app.openTabIds = [sid];
        app.tabState = {};
        app.tabState[sid] = app._blankTabState();
        const st = app._ensureTabState(sid);
        const cap = app._mountedMessageCap();
        const make = (prefix, i) => ({
          role: "assistant", uuid: `${prefix}-uuid-${i}`,
          _k: `${prefix}-key-${i}`, _noAnim: true,
          text: `${prefix}-${i} ` + "variable height ".repeat(8 + (i % 5) * 8),
          html: `<p>${prefix}-${i} ${"variable height ".repeat(8 + (i % 5) * 8)}</p>`,
        });
        st.messages.splice(0, st.messages.length,
          ...Array.from({ length: cap }, (_, i) => make("mounted", i)));
        st._earlierMessages = Array.from({ length: 10 }, (_, i) => make("older", i));
        st._laterMessages = [];
        st._loadedOffset = 0;
        st._total = cap + st._earlierMessages.length;
        st._hasServerLater = false;
        st.messagesReady = true;
        st.messagesLoading = false;
        app.currentId = sid;
        app._residentTabIds = [sid];
        app._activateTabState(sid);
        app._promoteResident(sid);
        app.mobileTab = "chat";
        return true;
        """,
        sid,
    )
    mounted_cap = _app_eval(page, "return app._mountedMessageCap();")
    page.wait_for_function(
        """mountedCap => Array.from(document.querySelectorAll('.msg-pane'))
          .filter(p => getComputedStyle(p).display !== 'none')
          .reduce((n, p) => n + p.querySelectorAll('.msg').length, 0) === mountedCap""",
        arg=mounted_cap,
        timeout=10000,
    )
    page.evaluate(
        """() => {
          const body = document.querySelector('.chat-body');
          body.scrollTop = Math.min(500, body.scrollHeight - body.clientHeight);
        }"""
    )
    older_anchor = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          const key = st.messages[0]._k;
          const top = document.querySelector(
            `.msg[data-message-key="${CSS.escape(key)}"]`).getBoundingClientRect().top;
          return { key, top };
        }"""
    )
    _app_eval(page, "return app.loadEarlierMessages(arg);", sid)
    page.wait_for_function(
        """({ key, mountedCap }) => {
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(p => getComputedStyle(p).display !== "none");
          return pane && pane.querySelectorAll(".msg").length === mountedCap
            && pane.querySelector(
              `.msg[data-message-key="${CSS.escape(key)}"]`);
        }""",
        arg={"key": older_anchor["key"], "mountedCap": mounted_cap},
        timeout=10000,
    )
    after_older = page.evaluate(
        """key => document.querySelector(
          `.msg[data-message-key="${CSS.escape(key)}"]`).getBoundingClientRect().top""",
        older_anchor["key"],
    )
    assert abs(after_older - older_anchor["top"]) < 2

    newer_anchor = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          const key = st.messages[st.messages.length - 1]._k;
          const top = document.querySelector(
            `.msg[data-message-key="${CSS.escape(key)}"]`).getBoundingClientRect().top;
          return { key, top };
        }"""
    )
    _app_eval(page, "return app.loadLaterMessages(arg);", sid)
    page.wait_for_function(
        """({ key, mountedCap }) => {
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(p => getComputedStyle(p).display !== "none");
          return pane && pane.querySelectorAll(".msg").length === mountedCap
            && pane.querySelector(
              `.msg[data-message-key="${CSS.escape(key)}"]`);
        }""",
        arg={"key": newer_anchor["key"], "mountedCap": mounted_cap},
        timeout=10000,
    )
    after_newer = page.evaluate(
        """key => document.querySelector(
          `.msg[data-message-key="${CSS.escape(key)}"]`).getBoundingClientRect().top""",
        newer_anchor["key"],
    )
    assert abs(after_newer - newer_anchor["top"]) < 2
    assert page.locator(".msg-pane:visible .msg").count() == mounted_cap
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
        app._residentTabIds = [arg];
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg);
        app._promoteResident(arg);
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
        app._residentTabIds = [sid];
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
          const last = app.messages[app.messages.length - 1];
          const pane = Array.from(document.querySelectorAll(".msg-pane"))
            .find(el => getComputedStyle(el).display !== "none");
          return app.streaming && last?.role === "assistant" && last.text === text
            && pane?.querySelector(".msg.assistant");
        }""",
        arg=final_text,
        timeout=10000,
    )
    live = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const last = app.messages[app.messages.length - 1];
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
        "() => document.querySelector('#app')._x_dataStack[0].streaming === false",
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
            const pane = Array.from(document.querySelectorAll(".msg-pane"))
              .find(el => getComputedStyle(el).display !== "none");
            frames.push({
              ready: app.messagesReady,
              loading: app.messagesLoading,
              visible: !!pane && pane.textContent.includes(text),
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
        app._residentTabIds = [sid];
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
    expect(footer.locator(".msg-ts")).to_have_text(expected_time)
    expect(footer.locator(".msg-elapsed")).to_have_text("· 2m05s")
    expect(footer.locator(".turn-fork-btn")).to_be_visible()

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
        "streaming": False,
    }
    assert ":live:" in live_key
    assert active_requests, "canonical barrier never checked /active"
    assert history_requests == [], (
        "canonical history loaded even though /active still reported true"
    )
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
        const targetState = app._ensureTabState(target);
        targetState._loaded = true;
        targetState.messages.push({
          role: "assistant", text: arg.finalText,
          html: `<p>${arg.finalText}</p>`, _k: `${target}:live:1`,
        });
        targetState.messagesReady = true;
        targetState.messagesLoading = false;
        app.currentId = other;
        app._residentTabIds = [other, target];
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
    _route_windowed_session(page, sid, messages)
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
          permission: "bypassPermissions", thinking: true }];
        app.openTabIds = [arg];
        app.tabState = {};
        app.currentId = arg;
        app._residentTabIds = [arg];
        app.mobileTab = "chat";
        app.messagesReady = true;
        app.messagesLoading = false;
        app._activateTabState(arg);
        app._promoteResident(arg);
        return true;
        """,
        sid,
    )

    _app_eval(page, "return app.loadSession(arg);", sid)
    # A pending background task is tracked, but it no longer makes the session
    # busy: the turn already reached ResultMessage, and the backend pump owns
    # the stream, so the user can keep talking while the task runs.
    page.wait_for_function(
        """sid => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[sid];
          return st && st.backgroundActive === true
            && st.streaming === false && app._isBusy(sid) === false
            && st.streamElapsed >= 89;
        }""",
        arg=sid,
        timeout=10000,
    )
    # The "background task running · new messages will queue" strip is gone
    # along with the queueing it described, and the turn footer no longer
    # spins for a task that is not this turn's work.
    expect(page.locator(".background-task-strip")).to_have_count(0)
    expect(page.locator(".msg-pane:visible .thinking-dots:visible")).to_have_count(0)
    # The tab dot still surfaces that something is running in the background.
    expect(page.locator(".chat-tab.active .chat-tab-stream-dot.is-background")).to_be_visible(
        timeout=5000,
    )
    assert tickets == [], "background-only state must not open an empty SSE"
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
        }, { preview: false });
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
    page.locator(SEL_MOBILE_TAB).nth(1).click()
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
    page.locator(SEL_MOBILE_TAB).nth(2).click()
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
                    .filter(child => !child.classList.contains("icon")
                      && !child.classList.contains("chat-toolbar-queue-badge"))
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
        ({"width": 1440, "height": 900}, False),
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
        app._residentTabIds = [sid];
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
            && last.html.includes("MID_STREAM_VISIBLE_1")
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
          htmlLength: last.html.length,
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
            && last.html.length >= prev.htmlLength
            && last.html.includes("MID_STREAM_VISIBLE_2")
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
          cached: st.messages.length + st._earlierMessages.length + st._laterMessages.length,
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
    assert render_stats["cached"] <= 120
    long_tasks = page.evaluate("() => window.__longTasks || []")
    assert max(long_tasks or [0]) < 2000, long_tasks

    _assert_no_browser_errors(page, errors)
