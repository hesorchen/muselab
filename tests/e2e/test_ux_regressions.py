"""Regression coverage for four frontend UX defects.

1. Manual ctx compact was a dead end: no elapsed time and no way to stop the
   wait (only auto-compact, which rides a turn stream, had either).
2. The finished-turn footer is a single non-wrapping flex row; on phones the
   right-hand Fork/Retry buttons were pushed outside the transcript.
3. The 90 s transcript watchdog only stopped perf sampling, so a stuck
   `_settleTranscriptLoad` could leave the opaque shield and inert chat body
   up forever.
4. The three permission-mode selects and a few aria labels/one label rendered
   raw English tokens in the Chinese UI.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import expect  # noqa: E402


SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"

READY = """() => {
  const app = document.querySelector("#app")?._x_dataStack?.[0];
  return app && app.authed === true && app.appReady && app._modelsLoaded
    && app._sessionsInitialized && app.currentId
    && app.openTabIds.includes(app.currentId) && app.sessions.length > 0;
}"""


def _login(page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}",
                           state="visible", timeout=20000)
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=20000)
    page.wait_for_function(READY, timeout=20000)


def _app_eval(page, body: str, arg=None):
    return page.evaluate(
        """([body, arg]) => {
            const app = document.querySelector("#app")._x_dataStack[0];
            const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
            return (new AsyncFunction("app", "arg", body))(app, arg);
        }""",
        [body, arg],
    )


# ---------------------------------------------------------------------------
# 1. Manual compact: elapsed time + Stop
# ---------------------------------------------------------------------------

_HANG_COMPACT = """
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (url, init) => {
    if (String(url).includes("/native-compact")) {
      // A slow real compact: the POST stays pending until aborted.
      window.__compactAborted = false;
      return new Promise((resolve, reject) => {
        const signal = init && init.signal;
        if (signal) {
          signal.addEventListener("abort", () => {
            window.__compactAborted = true;
            reject(new DOMException("Aborted", "AbortError"));
          }, { once: true });
        }
      });
    }
    return originalFetch(url, init);
  };
})();
"""


def test_manual_compact_shows_elapsed_and_can_be_stopped(
    page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.add_init_script(_HANG_COMPACT)
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length,
            {role: "user", text: "hello", uuid: "u1", ts: 1700000000,
             _k: "e2e-c0"},
            {role: "assistant", text: "hi", uuid: "a1", ts: 1700000001,
             _k: "e2e-c1"});
          app.sessions = app.sessions.map(
            s => s.id === sid ? {...s, message_count: 2} : s);
          window.__compactSid = sid;
          void app.runCompact(sid, {skipConfirm: true});
        }"""
    )
    expect(page.locator(".compact-pending")).to_be_visible(timeout=5000)

    # The 📦 bubble must show elapsed time just like the auto-compact path.
    page.wait_for_function(
        """() => {
          const el = document.querySelector(
            ".compact-pending .thinking-label");
          return !!el && /·\\s*\\d+s/.test(el.textContent || "");
        }""",
        timeout=6000,
    )

    # Stop must be offered while the manual wait is pending and must abort it.
    stop = page.locator(".chat-toolbar-stop")
    expect(stop).to_be_visible(timeout=3000)
    stop.click()
    page.wait_for_selector(".compact-pending", state="hidden", timeout=5000)
    state = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[window.__compactSid];
          return {
            compacting: !!st.compacting,
            abortCleared: !st._compactAbort,
            aborted: !!window.__compactAborted,
            toasts: (app.toasts || []).map(t => t.msg),
          };
        }"""
    )
    assert state["compacting"] is False, state
    assert state["abortCleared"] is True, state
    assert state["aborted"] is True, state
    # Truthful notice: the CLI command may still finish server-side.
    assert any("停止等待压缩" in msg or "Stopped waiting" in msg
               for msg in state["toasts"]), state


# ---------------------------------------------------------------------------
# 2. Finished-turn footer fits a phone width; Fork/Retry stay reachable
# ---------------------------------------------------------------------------

_PHONE_TURN = """
() => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const sid = app.currentId;
  const st = app._ensureTabState(sid);
  const ts = 1700000000;
  st.messages.splice(0, st.messages.length,
    {role: "user", text: "run the fixture check", uuid: "u1", ts,
     _k: "e2e-k0"},
    {role: "assistant", text: "working on it", uuid: "a1", ts: ts + 1,
     model: "e2e-model-long-name", _k: "e2e-k1"},
    {role: "tool_result", tool_name: "Bash", text: "ok", uuid: "t1",
     ts: ts + 2, model: "e2e-model-long-name", _k: "e2e-k2",
     model_usage: {"e2e-model-long-name": {
       inputTokens: 123456, outputTokens: 67890, costUSD: 0.0123}}});
  st._loaded = true;
  st.messagesReady = true;
  st.streaming = false;
  st.atBottom = true;
  st.messageRange = st.messageRange || {};
  st.messageRange.visibleStart = 0;
  st.messageRange.visibleEnd = st.messages.length;
  app.currentId = sid;
}
"""

_FOOTER_BOXES = """
() => {
  const pane = document.querySelector(".msg-pane:not([aria-hidden])")
    || document.querySelector(".msg-pane");
  const body = document.querySelector(".chat-body");
  const b = body.getBoundingClientRect();
  const rect = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {left: r.left, right: r.right, width: r.width, height: r.height};
  };
  return {
    body: {left: b.left, right: b.left + body.clientWidth,
           width: body.clientWidth, scrollWidth: body.scrollWidth},
    fork: rect(pane && pane.querySelector(".turn-fork-btn:not(.turn-retry-btn)")),
    retry: rect(pane && pane.querySelector(".turn-retry-btn")),
    footer: rect(pane && pane.querySelector(".turn-footer")),
    separator: (() => {
      const el = pane && pane.querySelector(".turn-footer");
      if (!el) return null;
      return {
        before: parseFloat(getComputedStyle(el, "::before").width) || 0,
        after: parseFloat(getComputedStyle(el, "::after").width) || 0,
      };
    })(),
  };
}
"""


def test_phone_turn_footer_keeps_fork_and_retry_inside_chat_body(
    page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    page.evaluate(_PHONE_TURN)
    page.wait_for_timeout(500)
    boxes = page.evaluate(_FOOTER_BOXES)
    body = boxes["body"]
    assert boxes["footer"], boxes
    for name in ("fork", "retry"):
        box = boxes[name]
        assert box, f"{name} button missing: {boxes}"
        assert box["width"] >= 24 and box["height"] >= 24, (name, box, body)
        assert box["left"] >= body["left"] - 1, (name, box, body)
        assert box["right"] <= body["right"] + 1, (name, box, body)
    # Nothing shoved horizontally out of the transcript.
    assert body["scrollWidth"] <= body["width"] + 2, boxes
    # The separator hairlines survive the narrow layout instead of collapsing.
    sep = boxes["separator"]
    assert sep and sep["before"] >= 6 and sep["after"] >= 6, boxes


# ---------------------------------------------------------------------------
# 3. The transcript shield cannot outlive its watchdog
# ---------------------------------------------------------------------------

def test_transcript_shield_watchdog_lifts_a_stuck_load(
    page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    page.evaluate(
        """() => {
          // Reproduce the documented stall: the settle path's awaited paint
          // never resolves (double rAF paused by a backgrounded page).
          const app = document.querySelector("#app")._x_dataStack[0];
          app._afterPaint = () => {};
          // Fire the 90 s watchdog quickly instead of waiting it out.
          const realSetTimeout = window.setTimeout.bind(window);
          window.setTimeout = (fn, ms, ...rest) =>
            realSetTimeout(fn, ms === 90000 ? 800 : ms, ...rest);
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          st.messages.splice(0, st.messages.length,
            {role: "assistant", text: "resident", uuid: "a1", ts: 1700000000});
          st.messagesReady = true;
          const token = app._beginTranscriptLoad(sid, st, "fetching");
          window.__loadSid = sid;
          window.__loadToken = token;
          void app._settleTranscriptLoad(token, {returnToLatest: false});
        }"""
    )
    # While settling, the opaque shield is up and the body is inert.
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return app.tabState[window.__loadSid].transcriptLoadPhase
            === "settling";
        }""",
        timeout=3000,
    )
    # The hang really is showing the opaque shield and an inert transcript.
    assert page.evaluate(
        """() => {
          const overlay = document.querySelector(
            ".chat-transcript-loading-overlay");
          const body = document.querySelector(".chat-body");
          return !!overlay && getComputedStyle(overlay).display !== "none"
            && !!body && body.inert === true;
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const st = app.tabState[window.__loadSid];
          const overlay = document.querySelector(
            ".chat-transcript-loading-overlay");
          const body = document.querySelector(".chat-body");
          return st.transcriptLoadPhase === "error"
            && overlay && getComputedStyle(overlay).display === "none"
            && body && body.inert === false;
        }""",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# 4. Chinese UI must not print raw permission-mode tokens
# ---------------------------------------------------------------------------

_PERMISSION_STATE = """
() => {
  const app = document.querySelector("#app")._x_dataStack[0];
  const selects = [...document.querySelectorAll("select")].filter(
    s => [...s.options].some(o => o.value === "acceptEdits"));
  const byHandler = handler => {
    const el = [...document.querySelectorAll("button")]
      .find(node => (node.getAttribute("@click") || "").includes(handler));
    return el ? el.getAttribute("aria-label") : null;
  };
  return {
    lang: app.lang,
    selects: selects.map(s => [...s.options].map(
      o => ({value: o.value, text: (o.textContent || "").trim()}))),
    reload: byHandler("reloadApp()"),
    todoClose: byHandler("closeSessionTodoBoard()"),
    activityClose: byHandler("closeActivityCenter()"),
    thinking: (() => {
      const el = document.querySelector(".thinking-label-text");
      return el ? (el.textContent || "").trim() : null;
    })(),
  };
}
"""


def test_chinese_ui_localizes_permission_modes_and_labels(
    page, backend_url, auth_token,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(
        page,
        """
        const sid = app.currentId, st = app._ensureTabState(sid);
        const ts = 1700000000;
        st.messages.splice(0, st.messages.length,
          {role: "thinking", text: "pondering", ts, _k: "e2e-think"},
          {role: "assistant", text: "answer", uuid: "a1", ts: ts + 1,
           _k: "e2e-answer"});
        st.messagesReady = true;
        st.messageRange = st.messageRange || {};
        st.messageRange.visibleStart = 0;
        st.messageRange.visibleEnd = st.messages.length;
        app.setLang("zh");
        await new Promise(r => setTimeout(r, 150));
        """,
    )
    state = page.evaluate(_PERMISSION_STATE)
    assert state["lang"] == "zh", state
    assert len(state["selects"]) >= 3, state["selects"]
    expected_zh = {
        "bypassPermissions": "跳过权限提示",
        "acceptEdits": "自动接受编辑",
        "default": "逐项确认",
        "dontAsk": "仅执行已授权操作",
        "auto": "Auto 模式",
        "plan": "Plan 模式",
    }
    for options in state["selects"]:
        texts = {row["value"]: row["text"] for row in options}
        assert set(texts) == set(expected_zh), texts
        for value, text in texts.items():
            assert text and text != value, (value, text)
            assert text == expected_zh[value], (value, text)
    assert state["reload"] == "重新加载应用", state
    assert state["todoClose"] == "关闭", state
    assert state["activityClose"] == "关闭", state
    assert state["thinking"] == "思考过程", state

    _app_eval(page, "app.setLang('en'); await new Promise(r => setTimeout(r, 150));")
    state_en = page.evaluate(_PERMISSION_STATE)
    first = {row["value"]: row["text"] for row in state_en["selects"][0]}
    assert first["acceptEdits"] == "Accept edits", first
    assert first["bypassPermissions"] == "Bypass prompts", first
    assert state_en["reload"] == "Reload app", state_en
    assert state_en["thinking"] == "thinking", state_en
