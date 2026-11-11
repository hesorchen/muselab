"""Browser Back/Forward move between the sessions the user switched to.

Every session switch records a `?session=` history entry. Back reopens the
previous session, Forward returns, and closing the shown tab replaces its
entry rather than leaving one that points at a closed tab.
"""
from __future__ import annotations

from playwright.sync_api import Page

from .test_chat_render_perf import _app_eval, _login

_CURRENT = "return app.currentId;"


def _wait_current(page: Page, sid: str) -> None:
    page.wait_for_function(
        "([sid]) => document.querySelector('#app')._x_dataStack[0].currentId === sid"
        " && new URL(location.href).searchParams.get('session') === sid",
        arg=[sid], timeout=10000,
    )


def _new_session(page: Page) -> str:
    before = _app_eval(page, _CURRENT)
    page.locator(".chat-tab-new").click()
    page.wait_for_function(
        "([prev]) => { const a = document.querySelector('#app')._x_dataStack[0];"
        " return a.currentId && a.currentId !== prev; }",
        arg=[before], timeout=10000,
    )
    sid = _app_eval(page, _CURRENT)
    _wait_current(page, sid)
    return sid


def test_back_and_forward_switch_sessions(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    first = _app_eval(page, _CURRENT)
    _wait_current(page, first)
    second = _new_session(page)
    third = _new_session(page)
    assert len({first, second, third}) == 3

    page.go_back()
    _wait_current(page, second)
    page.go_back()
    _wait_current(page, first)
    page.go_forward()
    _wait_current(page, second)

    # A switch after going back drops the forward entries, as in any browser.
    _app_eval(page, "await app.activateTab(arg);", third)
    _wait_current(page, third)
    page.go_back()
    _wait_current(page, second)


def test_closing_shown_tab_replaces_its_entry(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    first = _app_eval(page, _CURRENT)
    _wait_current(page, first)
    second = _new_session(page)
    length = page.evaluate("history.length")

    _app_eval(page, "await app.closeChatTab(arg);", second)
    page.wait_for_function(
        "([sid]) => document.querySelector('#app')._x_dataStack[0].currentId !== sid",
        arg=[second], timeout=10000,
    )
    shown = _app_eval(page, _CURRENT)
    _wait_current(page, shown)
    assert page.evaluate("history.length") == length
    assert _app_eval(page, "return app.openTabIds.includes(arg);", second) is False


def test_session_url_reopens_that_session(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    first = _app_eval(page, _CURRENT)
    second = _new_session(page)
    # Reopen the first session through the URL: the deep link wins over the
    # saved last-active tab and the entry stays on it.
    page.goto(f"{backend_url}/?session={first}", wait_until="domcontentloaded")
    _wait_current(page, first)
    assert _app_eval(page, "return app.openTabIds.includes(arg);", second)
