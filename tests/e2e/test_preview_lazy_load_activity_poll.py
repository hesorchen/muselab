"""Boot-path e2e: KaTeX / hljs stay lazy, and the activity summary poll is
gated on the activity SSE stream.

These tests run against a real backend + real frontend bundle. Session
history is served by route mocks (same pattern as test_chat_render_perf) so
the fixtures are deterministic without a model provider.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"


def _login(page: Page, base: str, token: str) -> None:
    # Real users boot with a saved token (the _bootApp path that schedules the
    # deferred boot work); a first-visit login skips that path entirely, so
    # seed localStorage to exercise the shipped boot sequence.
    page.add_init_script(
        "try { localStorage.setItem('muselab_token', %s); } catch (_) {}"
        % json.dumps(token)
    )
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(
        f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=20000
    )
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=20000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed === true && app.appReady
            && app._sessionsInitialized && app.currentId;
        }""",
        timeout=20000,
    )


def _app_eval(page: Page, body: str, arg=None):
    return page.evaluate(
        """([body, arg]) => {
            const app = document.querySelector("#app")._x_dataStack[0];
            const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
            return (new AsyncFunction("app", "arg", body))(app, arg);
        }""",
        [body, arg],
    )


def _bootstrap_session(page: Page, sid: str, name: str) -> None:
    """Point the app at a synthetic session without touching the backend."""
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
        app.sessions = [{ id: arg.sid, name: arg.name,
          updated_at: Date.now() / 1000, model: "e2e-model",
          permission: "bypassPermissions", thinking: true }];
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


def _route_session(page: Page, sid: str, messages: list[dict]) -> None:
    def handle(route) -> None:
        qs = parse_qs(urlparse(route.request.url).query)
        total = len(messages)
        offset = 0
        window = messages
        if "tail" in qs:
            offset = max(0, total - int(qs["tail"][0]))
            window = messages[offset:]
        elif "offset" in qs and "limit" in qs:
            offset = int(qs["offset"][0])
            window = messages[offset:offset + int(qs["limit"][0])]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": sid,
                "name": "Lazy fixture",
                "model": "e2e-model",
                "permission": "bypassPermissions",
                "thinking": True,
                "messages": window,
                "offset": offset,
                "total": total,
                "has_more": offset > 0,
                "history_order": "normal",
                "history_generation": "gen-e2e-lazy",
            }),
        )

    page.route(f"**/api/chat/sessions/{sid}?*", handle)


def _load_session(page: Page, sid: str, messages: list[dict]) -> None:
    _route_session(page, sid, messages)
    _bootstrap_session(page, sid, "Lazy fixture")
    loaded = _app_eval(page, "return await app._ensureSessionLoaded(arg);", sid)
    assert loaded is True, f"session {sid} did not load"
    page.wait_for_function(
        """(sid) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[sid];
          return !!(st && st._loaded && st.messagesReady && st.messages.length);
        }""",
        arg=sid,
        timeout=20000,
    )


def _paths(requests: list[str]) -> list[str]:
    return [urlparse(url).path for url in requests]


_PLAIN_MESSAGES = [
    {"role": "user", "text": "hello there", "ts": 1_700_000_000, "uuid": "u-plain"},
    {"role": "assistant",
     "text": "A plain prose answer with no fenced code and no math delimiters. "
             "It is only here so the transcript has a real message to paint.",
     "ts": 1_700_000_001, "uuid": "a-plain"},
]

_MIXED_MESSAGES = [
    {"role": "user", "text": "show me a dockerfile and a formula",
     "ts": 1_700_000_000, "uuid": "u-mixed"},
    {"role": "assistant",
     "text": (
         "Here is a container recipe:\n\n"
         "```dockerfile\n"
         "FROM alpine:3.19\n"
         "RUN apk add --no-cache curl\n"
         'CMD ["sh"]\n'
         "```\n\n"
         "And a formula:\n\n"
         "$$\n"
         "\\frac{a}{b} = c\n"
         "$$\n"
     ),
     "ts": 1_700_000_001, "uuid": "a-mixed"},
]


def test_boot_without_math_or_code_does_not_download_katex_or_hljs_langs(
    page: Page, backend_url, auth_token,
):
    page.bring_to_front()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    _login(page, backend_url, auth_token)
    _load_session(page, "lazy-boot-plain", _PLAIN_MESSAGES)

    # The old boot prewarm drained one vendor bundle per idle callback, which
    # always fit inside this window.
    page.wait_for_timeout(8000)
    paths = _paths(requests)
    katex = [p for p in paths if p.startswith("/static/vendor/katex/")]
    packs = [p for p in paths if p.startswith("/static/vendor/hljs-langs/")]
    assert katex == [], f"KaTeX fetched on a math-free boot: {katex}"
    assert packs == [], f"hljs language packs fetched with no code block: {packs}"


def test_first_view_of_dockerfile_and_math_renders_highlighting_and_katex(
    page: Page, backend_url, auth_token,
):
    page.bring_to_front()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    _login(page, backend_url, auth_token)
    assert not any(p.startswith("/static/vendor/katex/") for p in _paths(requests))

    _load_session(page, "lazy-mixed", _MIXED_MESSAGES)

    # Fenced ```dockerfile block → worker loads core + the dockerfile pack.
    code = page.locator(".chat-body pre code.hljs").first
    expect(code).to_be_visible(timeout=20000)
    page.wait_for_function(
        """() => {
          const nodes = document.querySelectorAll(".chat-body pre code.hljs");
          return Array.from(nodes).some(n => n.querySelector("span"));
        }""",
        timeout=20000,
    )
    code_html = code.inner_html()
    assert "FROM" in code_html
    assert "hljs-" in code_html, "code block has no highlight.js token spans"

    # $$ ... $$ → KaTeX output, on the first view of this session.
    katex = page.locator(".chat-body .katex").first
    expect(katex).to_be_visible(timeout=20000)
    assert page.locator(".chat-body .katex-error").count() == 0

    paths = _paths(requests)
    assert any(p.endswith("/vendor/katex/katex.min.js") for p in paths), paths
    assert any(p.endswith("/vendor/hljs-langs/dockerfile.min.js") for p in paths), paths
    # The boot itself stayed clean; the heavy assets arrived with content.
    first_katex = next(i for i, p in enumerate(paths)
                       if p.startswith("/static/vendor/katex/"))
    assert first_katex > next(i for i, p in enumerate(paths)
                              if p == "/static/app.js"), paths


def _install_activity_event_source(page: Page) -> None:
    """Fake only the activity SSE stream; other EventSources stay native."""
    page.add_init_script(
        """
        (() => {
          const streams = [];
          class FakeEventSource extends EventTarget {
            constructor(url) {
              super();
              this.url = String(url);
              this.readyState = 0;
              this.onerror = null;
              streams.push(this);
              setTimeout(() => {
                if (this.readyState === 0) this.readyState = 1;
              }, 0);
            }
            close() { this.readyState = 2; }
          }
          FakeEventSource.CONNECTING = 0;
          FakeEventSource.OPEN = 1;
          FakeEventSource.CLOSED = 2;
          window.EventSource = FakeEventSource;
          window.__activityStreams = () => streams.filter(
            es => String(es.url || "").includes("/api/activity/events?"));
          window.__emitActivity = (type, payload) => {
            const es = window.__activityStreams().at(-1);
            if (!es) throw new Error("no activity stream");
            es.dispatchEvent(new MessageEvent(type, {
              data: JSON.stringify(payload),
            }));
          };
          window.__failActivity = () => {
            const es = window.__activityStreams().at(-1);
            if (!es) throw new Error("no activity stream");
            es.readyState = 2;
            if (es.onerror) es.onerror(new Event("error"));
          };
        })();
        """
    )


def test_activity_summary_poll_yields_to_live_sse_and_resumes_when_blocked(
    page: Page, backend_url, auth_token,
):
    page.bring_to_front()
    _install_activity_event_source(page)
    summary_requests: list[str] = []
    page.on(
        "request",
        lambda r: summary_requests.append(r.url)
        if urlparse(r.url).path == "/api/activity/summary" else None,
    )
    _login(page, backend_url, auth_token)
    page.wait_for_function(
        """() => {
          const streams = window.__activityStreams ? window.__activityStreams() : [];
          return streams.length > 0 && streams.some(es => es.readyState === 1);
        }""",
        timeout=20000,
    )
    # A real server sends `ready` right after connecting; use it to settle the
    # one-shot catch-up fetch before measuring the steady-state window.
    page.evaluate(
        """() => window.__emitActivity("ready",
          { generation: "g-e2e-live", revision: 1 })"""
    )
    page.wait_for_timeout(1500)
    settled = len(summary_requests)

    page.wait_for_timeout(25000)
    assert len(summary_requests) == settled, (
        "GET /api/activity/summary polled while the activity SSE was healthy: "
        f"{summary_requests[settled:]}"
    )

    # A pushed update must still move the badge without any poll.
    page.evaluate(
        """() => window.__emitActivity("update", {
          generation: "g-e2e-live", revision: 2,
          summary: {"generation": "g-e2e-live", "revision": 2,
            "running": 1, "unread": 2, "attention": 1,
            "groups": {"review": 2, "running": 1, "failed": 0, "history": 0},
            "group_unread": {"review": 2, "running": 1, "failed": 0, "history": 0},
            "workspaces": []},
        })"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.activity.summary.unread === 2;
        }""",
        timeout=10000,
    )
    assert "active" in (page.locator(".activity-center-btn").get_attribute("class") or "")

    # Stream dies → the 10 s loop must fall back to polling the summary.
    before_block = len(summary_requests)
    page.evaluate("() => window.__failActivity()")
    page.wait_for_timeout(20000)
    assert len(summary_requests) > before_block, (
        "fallback summary poll did not resume after the activity SSE failed"
    )
