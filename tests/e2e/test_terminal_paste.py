"""Terminal pastes retain xterm semantics and the original terminal owner."""
from __future__ import annotations

import pytest

from .test_terminal_mobile import _login


@pytest.mark.parametrize("bracketed", [False, True])
def test_toolbar_paste_uses_terminal_paste_protocol(page, backend_url, auth_token, bracketed):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        r"""async bracketed => {
          const app = document.querySelector("#app")._x_dataStack[0];
          await app._loadTerminalLib();
          const term = new window.Terminal();
          const host = document.createElement("div");
          document.body.appendChild(host);
          term.open(host);
          const sent = [];
          const oldClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
          Object.defineProperty(navigator, "clipboard", {configurable: true,
            value: {readText: async () => "first line\nsecond line\r\nthird line"}});
          app._terminal = term;
          app._terminalSocket = {readyState: WebSocket.OPEN,
            send: data => sent.push(new TextDecoder().decode(data))};
          app.activeTerminalId = "paste-fixture";
          const listener = term.onData(data => app._terminalHandleInput(data, term));
          try {
            await new Promise(resolve => term.write(
              bracketed ? "\x1b[?2004h" : "\x1b[?2004l", resolve));
            await app.terminalPaste();
            return sent;
          } finally {
            listener.dispose();
            app._terminal = null;
            app._terminalSocket = null;
            term.dispose();
            host.remove();
            if (oldClipboard) Object.defineProperty(navigator, "clipboard", oldClipboard);
            else delete navigator.clipboard;
          }
        }""",
        bracketed,
    )
    expected = "first line\rsecond line\rthird line"
    if bracketed:
        expected = "\x1b[200~" + expected + "\x1b[201~"
    assert result == [expected]


@pytest.mark.parametrize("replacement", ["terminal", "socket", "generation"])
def test_delayed_clipboard_cannot_paste_into_replacement(
        page, backend_url, auth_token, replacement):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async replacement => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const sent = [];
          let finish;
          const oldClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
          Object.defineProperty(navigator, "clipboard", {configurable: true,
            value: {readText: () => new Promise(resolve => { finish = resolve; })}});
          const terminal = name => ({paste: text => sent.push([name, text])});
          const socket = name => ({readyState: WebSocket.OPEN,
            send: data => sent.push([name, new TextDecoder().decode(data)])});
          app._terminal = terminal("original");
          app._terminalSocket = socket("original");
          app.activeTerminalId = "paste-fixture";
          try {
            const pending = app.terminalPaste();
            if (replacement === "terminal") {
              app._terminal = terminal("replacement");
              app.activeTerminalId = "replacement-fixture";
            } else if (replacement === "socket") {
              app._terminalSocket = socket("replacement");
            } else {
              app._terminalConnectSeq += 1;
            }
            finish("deferred clipboard text");
            await pending;
            return sent;
          } finally {
            app._terminal = null;
            app._terminalSocket = null;
            if (oldClipboard) Object.defineProperty(navigator, "clipboard", oldClipboard);
            else delete navigator.clipboard;
          }
        }""",
        replacement,
    )
    assert result == []


def test_keyboard_paste_works_without_async_clipboard_api(
        browser, browser_name, backend_url, auth_token):
    if browser_name != "chromium":
        pytest.skip("trusted clipboard setup uses Chromium permissions")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=backend_url)
    page = context.new_page()
    created_id = ""
    try:
        _login(page, backend_url, auth_token)
        created_id = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const result = await app.api("/api/terminals", {
                method: "POST", headers: app.fileHdr(),
                json: {rows: 20, cols: 80, profile_id: ""},
              });
              if (!result.ok) throw new Error("fixture terminal creation failed");
              app.terminals = [...app.terminals, result.data];
              await app.openTerminal(result.data.id);
              return result.data.id;
            }"""
        )
        page.wait_for_function(
            "() => document.querySelector('#app')._x_dataStack[0]"
            ".terminalConnection === 'connected'"
        )
        page.evaluate(
            r"""async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              await navigator.clipboard.writeText("paste first\npaste second");
              Object.defineProperty(navigator, "clipboard",
                {configurable: true, value: undefined});
              window.__pasteOutbound = [];
              app._terminalSend = text => window.__pasteOutbound.push(text);
              await new Promise(resolve => app._terminal.write("\x1b[?2004h", resolve));
              app._terminal.focus();
            }"""
        )
        for shortcut in ("Control+V", "Control+Shift+V"):
            page.evaluate("window.__pasteOutbound = []")
            page.keyboard.press(shortcut)
            page.wait_for_function("window.__pasteOutbound.length > 0", timeout=3000)
            assert page.evaluate("window.__pasteOutbound") == [
                "\x1b[200~paste first\rpaste second\x1b[201~",
            ]
    finally:
        if created_id:
            page.evaluate(
                "id => document.querySelector('#app')._x_dataStack[0]"
                ".closeTerminal(id, {confirm: false})",
                created_id,
            )
        context.close()
