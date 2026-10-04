"""Bounded synthetic terminal-manager desktop keyboard controls."""

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect
from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


@pytest.fixture
def terminal_manager(page, settings_frontend_url):
    errors = []
    requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def handle(route):
        path = urlsplit(route.request.url).path
        requests.append({"method": route.request.method, "path": path})
        if path == "/api/terminals":
            route.fulfill(json={"terminals": [], "profiles": [], "limits": {"max_sessions": 8}})
        elif path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.token='terminal-keyboard-synthetic-token32';
      app.authed=true; app.appReady=true;
      app.activeWorkspace='/synthetic/workspace';
      app.sessionWorkspaces=[{path:'/synthetic/workspace',name:'Synthetic workspace',primary:true}];
      app.sessions=[{id:'terminal-keyboard-fixture',name:'Synthetic session',cwd:'/synthetic/workspace'}];
      app.openTabIds=['terminal-keyboard-fixture']; app.currentId='terminal-keyboard-fixture';
      await app.fetchTerminals();
    }""")
    expect(page.locator(".terminal-manager-btn")).to_be_visible()
    yield {"errors": errors, "requests": requests}
    assert errors == []
    assert not any(r["method"] != "GET" for r in requests)


def test_escape_closes_keyboard_opened_terminal_manager(page, terminal_manager):
    entry = page.locator(".terminal-manager-btn")
    entry.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".terminal-manager-pop")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.locator(".terminal-create-btn")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator(".terminal-manager-pop")).to_be_hidden()
    expect(entry).to_be_focused()
    page.keyboard.press("Space")
    expect(page.locator(".terminal-manager-pop")).to_be_visible()


def test_keyboard_toggle_preserves_terminal_manager_entry(page, terminal_manager):
    entry = page.locator(".terminal-manager-btn")
    entry.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".terminal-manager-pop")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.locator(".terminal-create-btn")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(entry).to_be_focused()
    page.keyboard.press("Space")
    expect(page.locator(".terminal-manager-pop")).to_be_hidden()
    expect(entry).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator(".terminal-manager-pop")).to_be_visible()
