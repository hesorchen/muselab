"""Real-browser regressions for the mobile terminal surface."""
from __future__ import annotations

import re

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="install with: uv add --group dev pytest-playwright",
)
from playwright.sync_api import Browser, Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".login, .chat-tabs-list", state="visible", timeout=5000)
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized
            && app.terminalEnabled;
        }"""
    )


def _touch_swipe_down(page: Page, x: float, start_y: float, end_y: float) -> None:
    """Dispatch a trusted Chromium touch gesture (finger down = history up)."""
    client = page.context.new_cdp_session(page)
    client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": start_y}]},
    )
    for step in range(1, 7):
        y = start_y + (end_y - start_y) * step / 6
        client.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]},
        )
        page.wait_for_timeout(16)
    client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    client.detach()


def _touch_tap(page: Page, x: float, y: float) -> None:
    """Dispatch one trusted Chromium tap."""
    client = page.context.new_cdp_session(page)
    client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
    )
    page.wait_for_timeout(50)
    client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    client.detach()


def test_desktop_terminal_mouse_selection_copies_once(
        page: Page, backend_url: str, auth_token: str):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const host = document.createElement("div");
          document.body.appendChild(host);
          let selectionHandler = null;
          let disposed = 0;
          let copies = 0;
          const term = {
            hasSelection: () => true,
            getSelection: () => "selected terminal text",
            onSelectionChange: handler => {
              selectionHandler = handler;
              return {dispose: () => { disposed += 1; }};
            },
          };
          const realTerminal = app._terminal;
          const realCopy = app.terminalCopy;
          app._terminal = term;
          app.terminalCopy = async () => { copies += 1; return true; };
          try {
            app._attachTerminalSelectionCopy(host, term);
            // Changing selection without a drag that started in this terminal
            // must not overwrite the clipboard.
            selectionHandler();
            document.dispatchEvent(new MouseEvent("mouseup", {
              bubbles: true, button: 0,
            }));
            await Promise.resolve();
            const beforeDrag = copies;

            host.dispatchEvent(new MouseEvent("mousedown", {
              bubbles: true, button: 0,
            }));
            selectionHandler();
            document.dispatchEvent(new MouseEvent("mouseup", {
              bubbles: true, button: 0,
            }));
            await Promise.resolve();
            const afterDrag = copies;

            // A later plain mouseup must not copy the old selection again.
            document.dispatchEvent(new MouseEvent("mouseup", {
              bubbles: true, button: 0,
            }));
            await Promise.resolve();
            return {beforeDrag, afterDrag, final: copies, disposed};
          } finally {
            if (app._terminalSelectionCleanup) app._terminalSelectionCleanup();
            app._terminal = realTerminal;
            app.terminalCopy = realCopy;
            host.remove();
          }
        }"""
    )
    assert result == {
        "beforeDrag": 0,
        "afterDrag": 1,
        "final": 1,
        "disposed": 0,
    }


def test_desktop_real_terminal_drag_selection_updates_clipboard(
        browser: Browser, browser_name: str, backend_url: str, auth_token: str):
    if browser_name != "chromium":
        pytest.skip("clipboard permission setup is Chromium-specific")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=backend_url)
    page = context.new_page()
    created_id = ""
    try:
        _login(page, backend_url, auth_token)
        created_id = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const result = await app.api("/api/terminals", {
                method: "POST",
                headers: app.fileHdr(),
                json: {rows: 20, cols: 80, profile_id: ""},
              });
              if (!result.ok) throw new Error(result.error || "create failed");
              app.terminals = [...app.terminals, result.data];
              await app.openTerminal(result.data.id);
              return result.data.id;
            }"""
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              .terminalConnection === "connected" """
        )
        page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              ._terminalSend("printf '\\\\nMOUSE_COPY_SENTINEL\\\\n'\\n")"""
        )
        page.wait_for_function(
            """() => {
              const term = document.querySelector("#app")._x_dataStack[0]._terminal;
              const buffer = term?.buffer?.active;
              if (!buffer) return false;
              for (let i = 0; i < buffer.length; i += 1) {
                if (buffer.getLine(i)?.translateToString(true)
                    === "MOUSE_COPY_SENTINEL") return true;
              }
              return false;
            }"""
        )
        line = page.evaluate(
            """() => {
              const term = document.querySelector("#app")._x_dataStack[0]._terminal;
              const buffer = term.buffer.active;
              let target = -1;
              for (let i = 0; i < buffer.length; i += 1) {
                if (buffer.getLine(i)?.translateToString(true)
                    === "MOUSE_COPY_SENTINEL") target = i;
              }
              return {
                row: target - buffer.viewportY,
                rows: term.rows,
                cols: term.cols,
              };
            }"""
        )
        screen = page.locator(".terminal-host .xterm-screen")
        box = screen.bounding_box()
        assert box is not None
        assert 0 <= line["row"] < line["rows"]
        cell_w = box["width"] / line["cols"]
        cell_h = box["height"] / line["rows"]
        y = box["y"] + (line["row"] + 0.5) * cell_h
        start_x = box["x"] + 0.35 * cell_w
        end_x = box["x"] + 19.4 * cell_w
        page.mouse.move(start_x, y)
        page.mouse.down()
        page.mouse.move(end_x, y, steps=12)
        page.mouse.up()
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              ._terminal?.getSelection().includes("MOUSE_COPY_SENTINEL") """
        )
        page.wait_for_function(
            """async () => (await navigator.clipboard.readText())
              .includes("MOUSE_COPY_SENTINEL") """
        )

        # Ctrl+Shift+V must stay on xterm's trusted native paste-event path.
        # Replacing it with navigator.clipboard.readText breaks LAN HTTP
        # deployments where the async Clipboard API is unavailable.
        page.evaluate(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              app.__nativePasteOutbound = [];
              const send = app._terminalSend.bind(app);
              app._terminalSend = text => {
                app.__nativePasteOutbound.push(text);
                send(text);
              };
            }"""
        )
        for shortcut, sentinel in (
            ("Control+V", "CTRL_V_PASTE_SENTINEL"),
            ("Control+Shift+V", "CTRL_SHIFT_V_PASTE_SENTINEL"),
        ):
            page.evaluate(
                """async sentinel => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  app.__nativePasteOutbound = [];
                  await navigator.clipboard.writeText(sentinel);
                  app._terminal.focus();
                }""",
                sentinel,
            )
            page.keyboard.press(shortcut)
            page.wait_for_function(
                """sentinel => document.querySelector("#app")._x_dataStack[0]
                  .__nativePasteOutbound.some(value => value.includes(sentinel))""",
                arg=sentinel,
            )
            paste_outbound = page.evaluate(
                """() => document.querySelector("#app")._x_dataStack[0]
                  .__nativePasteOutbound"""
            )
            assert not any("\x16" in value for value in paste_outbound)
    finally:
        if created_id:
            page.evaluate(
                """id => document.querySelector("#app")?._x_dataStack?.[0]
                  ?.closeTerminal(id, {confirm: false})""",
                created_id,
            )
        context.close()


def test_mobile_terminal_sheet_create_and_real_touch_scrollback(
        browser: Browser, browser_name: str, backend_url: str, auth_token: str):
    if browser_name != "chromium":
        pytest.skip("trusted touch dispatch uses the Chromium CDP")

    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    created_id = ""
    try:
        _login(page, backend_url, auth_token)

        # The terminal is a preview-pane surface, and since 2026-07-25 the
        # preview header holds its ONLY entry point (the chat-header duplicate
        # was removed). Switch panes the way the mobile tab bar would, then use
        # that button.
        page.evaluate(
            """() => {
              document.querySelector("#app")._x_dataStack[0].setMobileTab("preview");
            }"""
        )
        terminal_entry = page.locator(".terminal-manager-btn")
        expect(terminal_entry).to_be_visible()
        terminal_entry.click()

        sheet = page.locator(".terminal-manager-pop")
        expect(page.locator(".terminal-manager-backdrop")).to_be_visible()
        expect(sheet).to_be_visible()
        create = sheet.locator(".terminal-manager-head .terminal-create-btn")
        expect(create).to_be_visible()
        expect(create).to_have_text(re.compile(r"新建终端|New terminal"))

        # Keep Alpine's model on the default while changing only the native
        # select value. This reproduces the delayed mobile-picker commit that
        # used to make every creation fall back to the default profile.
        profiles = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const first = await app.api("/api/terminals/profiles", {
                method: "POST",
                json: {name: "Default", command: "printf '__DEFAULT__\\\\n'",
                       is_default: true},
              });
              const second = await app.api("/api/terminals/profiles", {
                method: "POST",
                json: {name: "Alternate", command: "printf '__ALTERNATE__\\\\n'",
                       is_default: false},
              });
              await app.fetchTerminals();
              return {first: first.data, second: second.data};
            }"""
        )
        page.evaluate(
            """value => {
              document.querySelector("#app")._x_dataStack[0].terminalProfileId = value;
            }""",
            profiles["first"]["id"],
        )
        page.wait_for_function(
            """value => document.querySelector(".terminal-profile-select")?.value === value""",
            arg=profiles["first"]["id"],
        )
        page.locator(".terminal-profile-select").evaluate(
            "(select, value) => { select.value = value; }",
            profiles["second"]["id"],
        )
        assert page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              .terminalProfileId"""
        ) == profiles["first"]["id"]

        sheet_box = sheet.bounding_box()
        assert sheet_box is not None
        assert abs((sheet_box["y"] + sheet_box["height"]) - 844) <= 2
        assert sheet_box["width"] >= 388

        create.click()
        page.wait_for_function(
            """() => {
              const app = document.querySelector("#app")?._x_dataStack?.[0];
              return app?.previewSurface === "terminal"
                && app?._terminal
                && document.querySelector(".terminal-host .xterm");
            }"""
        )
        created_id = page.evaluate(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              return app.activeTerminalId;
            }"""
        )
        page.wait_for_function(
            """expected => {
              const app = document.querySelector("#app")._x_dataStack[0];
              return app.terminals.find(row => row.id === app.activeTerminalId)
                ?.profile_id === expected;
            }""",
            arg=profiles["second"]["id"],
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              .terminalConnection === "connected" """
        )

        # Theme changes must recolor the already-open xterm immediately. The
        # three ANSI palettes are deliberately distinct; this catches a future
        # regression where only the surrounding preview chrome changes.
        terminal_themes = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const original = app.theme;
              const snapshots = {};
              for (const theme of ["dark", "light", "eyecare"]) {
                app.setTheme(theme);
                await new Promise(requestAnimationFrame);
                const colors = app._terminal.options.theme;
                snapshots[theme] = {
                  documentTheme: document.documentElement.dataset.theme,
                  background: colors.background,
                  brightWhite: colors.brightWhite,
                  brightGreen: colors.brightGreen,
                  diffAdd: colors.extendedAnsi?.[22 - 16] ?? null,
                  diffDel: colors.extendedAnsi?.[52 - 16] ?? null,
                  selectionBackground: colors.selectionBackground,
                  previewBackground: getComputedStyle(
                    document.querySelector(".preview-body.terminal-active")
                  ).backgroundColor,
                };
              }
              app.setTheme(original);
              return snapshots;
            }"""
        )
        assert terminal_themes["dark"]["documentTheme"] == "dark"
        assert terminal_themes["dark"]["background"] == "#0e1014"
        assert terminal_themes["dark"]["brightWhite"] == "#ffffff"
        assert terminal_themes["dark"]["diffAdd"] is None
        assert terminal_themes["dark"]["diffDel"] is None
        assert terminal_themes["dark"]["previewBackground"] == "rgb(14, 16, 20)"
        assert terminal_themes["light"]["documentTheme"] == "light"
        assert terminal_themes["light"]["background"] == "#ffffff"
        assert terminal_themes["light"]["brightWhite"] == "#1a1d22"
        assert terminal_themes["light"]["diffAdd"] == "#c7e5d0"
        assert terminal_themes["light"]["diffDel"] == "#f2c9c6"
        assert terminal_themes["light"]["previewBackground"] == "rgb(255, 255, 255)"
        assert terminal_themes["eyecare"]["documentTheme"] == "eyecare"
        assert terminal_themes["eyecare"]["background"] == "#f5f0e0"
        assert terminal_themes["eyecare"]["brightWhite"] == "#3d3526"
        assert terminal_themes["eyecare"]["diffAdd"] == "#c6d8b8"
        assert terminal_themes["eyecare"]["diffDel"] == "#dfb9aa"
        assert terminal_themes["eyecare"]["previewBackground"] == "rgb(245, 240, 224)"
        assert len({
            terminal_themes[theme]["brightGreen"]
            for theme in ("dark", "light", "eyecare")
        }) == 3

        # A TUI that exits without disabling mouse tracking leaves xterm
        # encoding ordinary clicks as coordinate sequences. In the normal
        # shell buffer muselab must drop that input and reset the local mode.
        page.evaluate(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              app.__terminalOutboundData = [];
              const send = app._terminalSend.bind(app);
              app._terminalSend = text => {
                app.__terminalOutboundData.push(text);
                send(text);
              };
            }"""
        )

        # iOS Chinese IMEs can update xterm's hidden textarea only at keyup
        # for keyCode=229 digits/punctuation. Exercise that exact event shape,
        # including an equal-length replacement which needs DEL + insert.
        ime_outbound = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const term = app._terminal;
              const textarea = term.textarea;
              const dispatch229 = (type, key) => {
                const event = new KeyboardEvent(type, {bubbles: true, key});
                Object.defineProperty(event, "keyCode", {value: 229});
                Object.defineProperty(event, "which", {value: 229});
                textarea.dispatchEvent(event);
              };
              const run = async (before, after, key) => {
                app.__terminalOutboundData = [];
                textarea.value = before;
                dispatch229("keydown", key);
                textarea.value = after;
                dispatch229("keyup", key);
                await new Promise(resolve => setTimeout(resolve, 30));
                return app.__terminalOutboundData.slice();
              };
              const result = {
                digit: await run("", "1", "1"),
                punctuation: await run("", "，", "，"),
                replacement: await run(" ", "。", "。"),
                replayReply: app._terminalDataIsReplayReply("\\u001b[>0;276;0c"),
                printable: app._terminalDataIsReplayReply("1，。"),
              };
              app._terminalSend("\\u0003");
              await new Promise(resolve => setTimeout(resolve, 30));
              return result;
            }"""
        )
        assert "".join(ime_outbound["digit"]) == "1"
        assert "".join(ime_outbound["punctuation"]) == "，"
        assert "".join(ime_outbound["replacement"]) == "\x7f。"
        assert ime_outbound["replayReply"] is True
        assert ime_outbound["printable"] is False

        # A device query produced by an old process remains in the server
        # replay buffer. Reopening the terminal must render it without sending
        # xterm's fresh DA2 reply (ESC[>0;276;0c) into the current shell.
        page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              ._terminalSend("printf '\\\\033[>c'\\n")"""
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              .__terminalOutboundData.some(
                value => /^\\u001b\\[>\\d+;\\d+;\\d+c$/.test(value)
              )"""
        )
        page.evaluate(
            """id => {
              const app = document.querySelector("#app")._x_dataStack[0];
              app.__terminalOutboundData = [];
              return app.openTerminal(id, {reconnect: true});
            }""",
            created_id,
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              .terminalConnection === "connected" """
        )
        page.wait_for_timeout(500)
        replay_outbound = page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              .__terminalOutboundData"""
        )
        assert not any(
            re.fullmatch(r"\x1b\[>\d+;\d+;\d+c", value)
            for value in replay_outbound
        ), repr(replay_outbound)

        page.evaluate(
            """() => new Promise(resolve => document.querySelector("#app")
              ._x_dataStack[0]._terminal.write(
                "\\u001b[?1000h\\u001b[?1006h\\u001b[?1016h", resolve
              ))"""
        )
        host_box = page.locator(".terminal-host").bounding_box()
        assert host_box is not None
        page.mouse.click(
            host_box["x"] + host_box["width"] / 2,
            host_box["y"] + host_box["height"] / 2,
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              ._terminal?.modes?.mouseTrackingMode === "none" """
        )
        outbound = page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              .__terminalOutboundData"""
        )
        assert not any(value.startswith("\x1b[<") for value in outbound)

        # Touch gestures must never leak mouse-coordinate reports while a
        # full-screen app has enabled the alternate buffer and SGR pixels.
        page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              app.__terminalRawInput = [];
              app.__terminalOutboundData = [];
              const handle = app._terminalHandleInput.bind(app);
              app._terminalHandleInput = (data, term) => {
                app.__terminalRawInput.push(data);
                handle(data, term);
              };
              await new Promise(resolve => app._terminal.write(
                "\\u001b[?1049h\\u001b[?1002h\\u001b[?1006h\\u001b[?1016h",
                resolve,
              ));
            }"""
        )
        _touch_swipe_down(
            page,
            host_box["x"] + host_box["width"] / 2,
            host_box["y"] + host_box["height"] * 0.35,
            host_box["y"] + host_box["height"] * 0.78,
        )
        _touch_tap(
            page,
            host_box["x"] + host_box["width"] / 2,
            host_box["y"] + host_box["height"] / 2,
        )
        page.wait_for_timeout(1000)
        raw_touch_input = page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              .__terminalRawInput"""
        )
        touch_outbound = page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              .__terminalOutboundData"""
        )
        assert any(value.startswith("\x1b[<") for value in raw_touch_input)
        assert not any(value.startswith("\x1b[<") for value in touch_outbound)
        page.evaluate(
            """() => new Promise(resolve => document.querySelector("#app")
              ._x_dataStack[0]._terminal.write(
                "\\u001b[?1002l\\u001b[?1006l\\u001b[?1016l\\u001b[?1049l",
                resolve,
              ))"""
        )

        # A shell/app may request a steady bar via DECSCUSR 6. muselab keeps
        # the requested shape but forces the cursor to remain blinking.
        cursor = page.evaluate(
            """async () => {
              const term = document.querySelector("#app")._x_dataStack[0]._terminal;
              await new Promise(resolve => term.write("\\u001b[6 q", resolve));
              return {
                blink: term.options.cursorBlink,
                style: term.options.cursorStyle,
                focused: document.activeElement === term.textarea,
              };
            }"""
        )
        assert cursor == {"blink": True, "style": "bar", "focused": True}

        # A real accessory-key click must deliver one literal backslash to the
        # live PTY and return focus to xterm's hidden mobile textarea.
        page.evaluate(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              app.__terminalMobileKeyData = app.__terminalOutboundData;
            }"""
        )
        page.locator('[data-terminal-key="backslash"]').click()
        page.wait_for_function(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              return app.__terminalMobileKeyData?.at(-1) === "\\\\"
                && document.activeElement === app._terminal?.textarea;
            }"""
        )

        before = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const term = app._terminal;
              await new Promise(resolve => term.write(
                Array.from({length: 240}, (_, i) => `touch-line-${i}\\r\\n`).join(""),
                resolve,
              ));
              term.scrollToBottom();
              return {
                baseY: term.buffer.active.baseY,
                viewportY: term.buffer.active.viewportY,
                scrollback: term.options.scrollback,
              };
            }"""
        )
        assert before["baseY"] > 100
        assert before["scrollback"] == 3000

        host_box = page.locator(".terminal-host").bounding_box()
        assert host_box is not None
        _touch_swipe_down(
            page,
            host_box["x"] + host_box["width"] / 2,
            host_box["y"] + host_box["height"] * 0.35,
            host_box["y"] + host_box["height"] * 0.78,
        )
        page.wait_for_function(
            """before => {
              const term = document.querySelector("#app")._x_dataStack[0]._terminal;
              return term && term.buffer.active.viewportY < before;
            }""",
            arg=before["viewportY"],
        )
    finally:
        if created_id:
            page.evaluate(
                """id => {
                  const app = document.querySelector("#app")?._x_dataStack?.[0];
                  return app?.closeTerminal(id, {confirm: false});
                }""",
                created_id,
            )
        context.close()


def test_mobile_reopen_keeps_last_pane_while_terminal_restores(
        browser: Browser, browser_name: str, backend_url: str, auth_token: str):
    """A restored/reconnecting terminal must not route a phone away from chat/files."""
    if browser_name != "chromium":
        pytest.skip("mobile cold-start coverage runs on Chromium")

    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    created_id = ""
    try:
        _login(page, backend_url, auth_token)
        created_id = page.evaluate(
            """async () => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const result = await app.api("/api/terminals", {
                method: "POST",
                headers: app.fileHdr(),
                json: {rows: 20, cols: 80, profile_id: ""},
              });
              if (!result.ok) throw new Error(result.error || "create failed");
              app.terminals = [...app.terminals, result.data];
              await app.openTerminal(result.data.id);
              return result.data.id;
            }"""
        )
        page.wait_for_function(
            """() => document.querySelector("#app")._x_dataStack[0]
              .terminalConnection === "connected" """
        )

        def reopen_on(pane: str) -> dict:
            page.evaluate(
                """pane => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  app.setMobileTab(pane);
                  app.savePrefs();
                }""",
                pane,
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                """() => {
                  const app = document.querySelector("#app")?._x_dataStack?.[0];
                  return app?.authed
                    && app.terminals.some(row => row.id === app.activeTerminalId)
                    && app.terminalConnection === "connected";
                }""",
                # Earlier tests deliberately leave a very large current chat
                # in the disposable backend. Its first render can occupy the
                # mobile main thread even though terminal restore is already
                # progressing, so use the suite's normal slow-start budget.
                timeout=30000,
            )
            # Let the delayed terminal restore and preference coalescer settle;
            # the old race changed both values shortly after initial paint.
            page.wait_for_timeout(250)
            return page.evaluate(
                """() => {
                  const app = document.querySelector("#app")._x_dataStack[0];
                  const prefs = JSON.parse(
                    localStorage.getItem("muselab_prefs") || "{}");
                  return {
                    mobileTab: app.mobileTab,
                    storedTab: prefs.mobileTab,
                    previewSurface: app.previewSurface,
                    activeTerminalId: app.activeTerminalId,
                    terminalConnected: app.terminalConnection === "connected",
                    hiddenTerminalFocused:
                      document.activeElement === app._terminal?.textarea,
                  };
                }"""
            )

        chat_state = reopen_on("chat")
        assert chat_state == {
            "mobileTab": "chat",
            "storedTab": "chat",
            "previewSurface": "terminal",
            "activeTerminalId": created_id,
            "terminalConnected": True,
            "hiddenTerminalFocused": False,
        }

        # A transport reconnect uses the same openTerminal plumbing. It must
        # remain a background repair while the terminal pane is hidden.
        page.evaluate(
            """() => document.querySelector("#app")._x_dataStack[0]
              ._terminalSocket.close()"""
        )
        page.wait_for_function(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              return app.terminalConnection === "connected"
                && app.mobileTab === "chat";
            }""",
            timeout=10000,
        )
        page.wait_for_timeout(250)
        assert page.evaluate(
            """() => {
              const app = document.querySelector("#app")._x_dataStack[0];
              const prefs = JSON.parse(
                localStorage.getItem("muselab_prefs") || "{}");
              return app.mobileTab === "chat" && prefs.mobileTab === "chat"
                && document.activeElement !== app._terminal?.textarea;
            }"""
        )

        files_state = reopen_on("files")
        assert files_state == {
            "mobileTab": "files",
            "storedTab": "files",
            "previewSurface": "terminal",
            "activeTerminalId": created_id,
            "terminalConnected": True,
            "hiddenTerminalFocused": False,
        }
    finally:
        if created_id:
            page.evaluate(
                """id => document.querySelector("#app")?._x_dataStack?.[0]
                  ?.closeTerminal(id, {confirm: false})""",
                created_id,
            )
        context.close()
