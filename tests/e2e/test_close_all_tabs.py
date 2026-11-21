"""One-tap "close all / close others" for preview and session tabs.

Phone users cannot right-click, so both tab strips get a bulk-close footer.
These cases need a real browser: they exercise the Alpine click handlers, the
dropdown/footer stacking, and the end state after the last tab closes.

Closing a tab is reversible (the session stays in history), so the bulk
actions may only show a confirm when a tab would lose an unsent draft or
pending attachment.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_TAB = ".chat-tab"
SEL_PREVIEW_PICKER = ".tab-picker-pop"
SEL_HISTORY_PICKER = "#history-picker-pop"


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}", state="visible",
                           timeout=15000)
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


def _app(page: Page, body: str, arg=None):
    return page.evaluate(
        """([body, arg]) => {
            const app = document.querySelector("#app")._x_dataStack[0];
            const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
            return (new Function("app", "arg", "return (async () => {"
              + body + "})()"))(app, arg);
        }""",
        [body, arg],
    )


def _new_sessions(page: Page, base: str, token: str, count: int,
                  prefix: str) -> list[str]:
    headers = {"X-Auth-Token": token}
    sids: list[str] = []
    for i in range(count):
        res = page.request.post(
            f"{base}/api/chat/sessions", headers=headers,
            data={"name": f"{prefix} {i}"})
        assert res.ok, res.text()
        sids.append(res.json()["id"])
    return sids


def _open_chat_tabs(page: Page, sids: list[str], sizings: str = "chat") -> None:
    _app(page, f"""
        app.setMobileTab && app.setMobileTab({sizings!r});
        app.mobileTab = {sizings!r};
        await app.refreshSessions();
        for (const sid of arg) {{
          if (!app.sessions.find(s => s.id === sid)) {{
            throw new Error("session missing from list: " + sid);
          }}
          await app.openTab(sid);
        }}
    """, sids)
    page.wait_for_function(
        "([sids]) => { const app = document.querySelector('#app')._x_dataStack[0];"
        " return sids.every(sid => app.openTabIds.includes(sid)); }",
        arg=[sids],
    )


def _open_history_picker(page: Page) -> None:
    page.evaluate(
        """() => { const app = document.querySelector('#app')._x_dataStack[0];
                   app.mobileTab = 'chat'; app.setMobileTab && app.setMobileTab('chat'); }"""
    )
    page.locator("#history-picker-trigger").click()
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].sessionPickerOpen === true"
    )
    expect(page.locator(SEL_HISTORY_PICKER)).to_be_visible(timeout=5000)


def _snapshot_tabs(page: Page) -> dict:
    return page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const store = JSON.parse(localStorage.getItem('muselab_chat_tabs_v1') || '{}');
          return {openTabIds: app.openTabIds.slice(), currentId: app.currentId,
                  store: (store.openTabIds || []).slice()};
        }"""
    )


@pytest.mark.parametrize("width,height", [(390, 844), (1440, 900)])
def test_preview_dropdown_closes_others_and_all(page: Page, backend_url,
                                                auth_token, width, height):
    page.set_viewport_size({"width": width, "height": height})
    _login(page, backend_url, auth_token)
    _app(page, """
        app.mobileTab = 'preview';
        app.setMobileTab && app.setMobileTab('preview');
        app.tabs = [
          {path: 'README.md', name: 'README.md'},
          {path: 'notes.md', name: 'notes.md'},
          {path: 'smooth-preview.html', name: 'smooth-preview.html'},
        ];
        app.selected = 'notes.md';
        app.previewSurface = 'file';
        app.savePrefs();
    """)

    page.locator(".tab-picker-btn").click()
    expect(page.locator(SEL_PREVIEW_PICKER)).to_be_visible(timeout=5000)
    page.locator(f'{SEL_PREVIEW_PICKER} [data-close-scope="others"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabs.length === 1 && app.tabs[0].path === 'notes.md'
            && app.selected === 'notes.md' && app.editorTabPickerOpen === false;
        }"""
    )

    page.locator(".tab-picker-btn").click()
    expect(page.locator(SEL_PREVIEW_PICKER)).to_be_visible(timeout=5000)
    page.locator(f'{SEL_PREVIEW_PICKER} [data-close-scope="all"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabs.length === 0 && app.selected === ''
            && app.editorTabPickerOpen === false;
        }"""
    )


@pytest.mark.parametrize("width,height", [(390, 844), (1440, 900)])
def test_session_footer_close_others_keeps_current(page: Page, backend_url,
                                                   auth_token, width, height):
    page.set_viewport_size({"width": width, "height": height})
    _login(page, backend_url, auth_token)
    sids = _new_sessions(page, backend_url, auth_token, 3, "Close others fixture")
    _open_chat_tabs(page, sids)

    _open_history_picker(page)
    page.locator(f'{SEL_HISTORY_PICKER} [data-close-scope="others"]').click()
    page.wait_for_function(
        """([keep]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.openTabIds.length === 1 && app.openTabIds[0] === keep
            && app.currentId === keep;
        }""",
        arg=[sids[-1]],
    )
    snapshot = _snapshot_tabs(page)
    assert snapshot["currentId"] == sids[-1]
    assert snapshot["store"] == [sids[-1]]
    assert page.locator(SEL_TAB).count() == 1


def test_session_footer_close_all_matches_closing_last_tab(
        page: Page, backend_url, auth_token):
    """Close-all must leave the same state as closing the last tab today
    (a fresh empty session), never issue DELETE, and keep every session."""
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)

    deletes: list[str] = []

    def track_delete(route):
        request = route.request
        path = urlparse(request.url).path
        if request.method == "DELETE" and re.match(
                r"^/api/chat/sessions/[^/]+$", path):
            deletes.append(path)
        route.continue_()

    page.route("**/api/chat/sessions/*", track_delete)

    # Baseline: closing the last tab one-by-one today.
    seq_sids = _new_sessions(page, backend_url, auth_token, 3, "Seq close fixture")
    _open_chat_tabs(page, seq_sids)
    _app(page, "for (const id of app.openTabIds.slice()) "
               "await app.closeChatTab(id);")
    baseline = _snapshot_tabs(page)
    assert len(baseline["openTabIds"]) == 1
    assert baseline["currentId"] not in seq_sids
    assert baseline["store"] == baseline["openTabIds"]

    # Bulk: the new footer action on a fresh set of session tabs.
    bulk_sids = _new_sessions(page, backend_url, auth_token, 3, "Bulk close fixture")
    _open_chat_tabs(page, [s for s in bulk_sids if s not in seq_sids])
    _open_history_picker(page)
    page.locator(f'{SEL_HISTORY_PICKER} [data-close-scope="all"]').click()
    page.wait_for_function(
        """([oldIds]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const store = JSON.parse(localStorage.getItem('muselab_chat_tabs_v1') || '{}');
          return app.openTabIds.length === 1
            && !oldIds.includes(app.currentId)
            && !oldIds.some(id => app.openTabIds.includes(id))
            && (store.openTabIds || []).join() === app.openTabIds.join();
        }""",
        arg=[bulk_sids],
    )
    bulk = _snapshot_tabs(page)
    # Same terminal state shape as the sequential baseline.
    assert len(bulk["openTabIds"]) == len(baseline["openTabIds"]) == 1
    assert bulk["currentId"] == bulk["openTabIds"][0]
    assert bulk["currentId"] not in bulk_sids
    assert page.locator(SEL_TAB).count() == 1

    assert deletes == [], f"closing tabs must not DELETE sessions: {deletes}"
    for sid in seq_sids + bulk_sids:
        res = page.request.get(
            f"{backend_url}/api/chat/sessions/{sid}",
            headers={"X-Auth-Token": auth_token})
        assert res.ok, f"session {sid} was deleted: {res.text()}"


def test_bulk_close_confirm_when_a_tab_has_a_draft(page: Page, backend_url,
                                                   auth_token):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    sids = _new_sessions(page, backend_url, auth_token, 1, "Draft fixture")
    _open_chat_tabs(page, sids)
    _app(page, """
        const st = app.tabState[arg[0]];
        st.draft.input = 'unsent draft text';
    """, sids)

    _open_history_picker(page)
    before_count = page.locator(SEL_TAB).count()
    page.locator(f'{SEL_HISTORY_PICKER} [data-close-scope="all"]').click()
    expect(page.locator(".confirm-modal")).to_be_visible(timeout=5000)
    body = page.locator(".confirm-modal .modal-body").inner_text()
    assert "1" in body

    # Cancelling keeps every tab (and the draft) intact.
    page.locator(".confirm-modal .btn-ghost").click()
    page.wait_for_function(
        "([sid]) => { const app = document.querySelector('#app')._x_dataStack[0];"
        " return app.openTabIds.includes(sid)"
        "   && app.tabState[sid] && app.tabState[sid].draft.input === 'unsent draft text'; }",
        arg=[sids[0]],
    )
    assert page.locator(SEL_TAB).count() == before_count

    # Confirming closes the tab and the session survives.
    _open_history_picker(page)
    page.locator(f'{SEL_HISTORY_PICKER} [data-close-scope="all"]').click()
    expect(page.locator(".confirm-modal")).to_be_visible(timeout=5000)
    page.locator(".confirm-modal .btn-danger").click()
    page.wait_for_function(
        "([sid]) => { const app = document.querySelector('#app')._x_dataStack[0];"
        " return !app.openTabIds.includes(sid) && app.openTabIds.length === 1; }",
        arg=[sids[0]],
    )
    res = page.request.get(
        f"{backend_url}/api/chat/sessions/{sids[0]}",
        headers={"X-Auth-Token": auth_token})
    assert res.ok
