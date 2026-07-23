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

        terminal_entry = page.locator(".chat-terminal-btn")
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
