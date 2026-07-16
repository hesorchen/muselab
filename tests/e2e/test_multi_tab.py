"""Browser-level smoke tests for the multi-tab chat UI.

These cover the regression classes that bit us during the 2026-05-17
multi-tab sprint and can ONLY be caught in a real browser:
- DOM event wiring (click, drag, contextmenu)
- Alpine x-effect / x-show / x-if reactivity races
- localStorage round-tripping (preview path, open tabs)
- document.title responding to streaming + session changes

Skipped by default. Enable with `RUN_E2E=1`. See tests/e2e/README.md."""
from __future__ import annotations
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


# Selectors mirror frontend/index.html. Centralised so a UI rename only
# breaks one place.
SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_TAB = ".chat-tab"
SEL_TAB_ACTIVE = ".chat-tab.active"
SEL_TAB_NAME = ".chat-tab-name"
SEL_TAB_RENAME = ".chat-tab-rename-input"
SEL_TAB_CLOSE = ".chat-tab-close"
SEL_TAB_NEW = ".chat-tab-new"


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base)
    # Wait for either the login screen or (if a token is already stored)
    # the tab strip to appear.
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=5000)
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed === true && app.currentId
            && app.openTabIds.includes(app.currentId) && app.sessions.length > 0;
        }"""
    )


def test_new_and_switch_and_close_tabs(page: Page, backend_url, auth_token):
    """Open multiple chat tabs, switch between them, close one — verify the
    bar reflects each operation and no tab is silently lost."""
    _login(page, backend_url, auth_token)
    initial = page.locator(SEL_TAB).count()

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 2)

    # Switch to the first tab.
    page.locator(SEL_TAB).first.click()
    expect(page.locator(SEL_TAB_ACTIVE)).to_have_count(1)

    # Close the active tab via its × button.
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_CLOSE}").click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)


def test_inline_rename_via_dblclick(page: Page, backend_url, auth_token):
    """Double-click a tab title to swap in the rename input; Enter commits.
    Guards the x-if/blur race regression."""
    _login(page, backend_url, auth_token)
    active_name = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}")
    active_name.dblclick()

    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    expect(inp).to_be_visible()
    inp.fill("e2e-renamed")
    inp.press("Enter")
    expect(active_name).to_contain_text("e2e-renamed")


def test_browser_title_reflects_session(page: Page, backend_url, auth_token):
    """document.title should include the active session's name after rename
    — exercises the x-effect on the root element."""
    _login(page, backend_url, auth_token)
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}").dblclick()
    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    inp.fill("title-probe")
    inp.press("Enter")
    page.wait_for_function("document.title.includes('title-probe')")
    assert "muselab" in page.title()


def test_keyboard_shortcut_ctrl_t_opens_tab(page: Page, backend_url, auth_token):
    """Ctrl+T opens a new tab and makes it active."""
    _login(page, backend_url, auth_token)
    start = page.locator(SEL_TAB).count()
    # Click into the tab strip first so focus is inside the app — global
    # keydown only fires when nothing else is consuming the event.
    page.locator(SEL_TABS).click()
    page.keyboard.press("Control+t")
    expect(page.locator(SEL_TAB)).to_have_count(start + 1)


def test_workspace_picker_switches_files_previews_and_sessions_together(
        page: Page, backend_url, auth_token, tmp_path):
    """A workspace switch restores its files, preview and conversation."""
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    primary_id = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    other = Path(primary) / ("workspace-two-" + tmp_path.name)
    other.mkdir()
    (other / "WORKSPACE_ONLY.md").write_text(
        "# second workspace\n\nworkspace-isolated-preview\n", encoding="utf-8")

    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.rawText.includes('muselab e2e');
        }""")

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-add").click()
    modal = page.locator(".workspace-browser-modal")
    expect(modal).to_be_visible()
    row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{other}"]')
    expect(row).to_be_visible(timeout=5000)
    row.locator(".workspace-browser-entry").click()
    page.locator(".workspace-browser-confirm").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(item => item.id === app.currentId);
          return app.currentWorkspacePath() === path && !app.workspaceSwitching
            && session?.cwd === path;
        }""",
        arg=[str(other)],
        timeout=15000,
    )

    state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            visible: app.visible.map(item => item.path),
            selected: app.selected,
            currentId: app.currentId,
            tabCwds: app.workspaceOpenTabIds().map(id =>
              app.sessions.find(item => item.id === id)?.cwd),
          };
        }""")
    assert "WORKSPACE_ONLY.md" in state["visible"]
    assert "README.md" not in state["visible"]
    assert state["selected"] == ""
    assert state["tabCwds"] and set(state["tabCwds"]) == {str(other)}
    secondary_id = state["currentId"]

    page.locator('.filelist li[data-path="WORKSPACE_ONLY.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview');
        }""")

    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{primary}"] .workspace-picker-select').click()
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'README.md' && app.rawText.includes('muselab e2e')
            && !app.workspaceSwitching;
        }""",
        arg=[primary, primary_id],
        timeout=15000,
    )

    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{other}"] .workspace-picker-select').click()
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview')
            && !app.workspaceSwitching;
        }""",
        arg=[str(other), secondary_id],
        timeout=15000,
    )

    # Remove the registry entry through the UI; project files remain untouched.
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{primary}"] .workspace-picker-select').click()
    page.wait_for_function(
        "([path]) => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath() === path",
        arg=[primary],
    )
    page.locator(".workspace-picker-btn").click()
    page.locator(
        f'.workspace-picker-row[title="{other}"] .workspace-picker-remove').click()
    expect(page.locator(".confirm-modal")).to_be_visible()
    page.locator(".confirm-modal .btn-danger").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return !app.workspaceSwitching
            && !app.sessionWorkspaces.some(item => item.path === path);
        }""",
        arg=[str(other)],
    )


def test_workspace_folder_browser_is_fullscreen_and_navigable_on_mobile(
        page: Page, backend_url, auth_token, tmp_path):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    parent = Path(primary) / ("mobile-picker-" + tmp_path.name)
    child = parent / "nested-project"
    child.mkdir(parents=True)
    (child / "package.json").write_text('{"name":"nested"}\n', encoding="utf-8")

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-add").click()
    modal = page.locator(".workspace-browser-modal")
    expect(modal).to_be_visible()
    page.wait_for_timeout(250)
    box = modal.bounding_box()
    assert box is not None
    assert box["x"] == 0
    assert box["y"] == 0
    assert box["width"] >= 389
    assert box["height"] >= 843

    parent_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{parent}"]')
    expect(parent_row).to_be_visible(timeout=5000)
    parent_row.locator(".workspace-browser-open").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[str(parent)],
    )
    child_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{child}"]')
    expect(child_row).to_be_visible()
    expect(child_row).to_contain_text("Node.js")

    page.locator(".workspace-browser-up").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[primary],
    )
    page.locator(".workspace-browser-modal .modal-close").click()
    expect(modal).to_be_hidden()


# Note: drag-and-drop tab reorder and right-click context menu are harder
# to drive reliably with Playwright's HTML5 drag emulation across browsers.
# Left as manual smoke for now.
