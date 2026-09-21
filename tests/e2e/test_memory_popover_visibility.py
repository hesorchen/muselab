"""File navigation must never expose a closed memory-recall popover."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright is required")
from playwright.sync_api import expect  # noqa: E402

from .test_files_preview import _login  # noqa: E402


@pytest.mark.parametrize("width", [390, 1440])
def test_file_click_keeps_closed_memory_recall_hidden(
    page, backend_url, auth_token, width, tmp_path,
):
    page.set_viewport_size({"width": width, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _login(page, backend_url, auth_token)
    for path in ["README.md", "cache-0.html"]:
        if width < 720:
            page.locator(".mobile-tab-bar button").nth(0).click()
        row = page.locator(f'.filelist li[data-path="{path}"]')
        expect(row).to_be_visible()
        row.click()
        page.wait_for_function(
            "path => document.querySelector('#app')._x_dataStack[0].selected === path",
            arg=path,
        )
        assert page.evaluate(
            "document.querySelector('#app')._x_dataStack[0].memoryRecallPopover.show"
        ) is False
        page.screenshot(path=str(tmp_path / f"file-click-{width}-{path}.png"))
        expect(page.locator(".memory-recall-global")).to_be_hidden()
        expect(page.locator(".pane.preview")).to_be_visible()
        if path.endswith(".html"):
            expect(page.frame_locator(".pane.preview iframe:visible")
                   .locator("h1")).to_have_text("Cache fixture 0")
        else:
            expect(page.locator(".pane.preview .markdown:visible")).to_contain_text(
                "muselab e2e fixture"
            )
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_memory_recall_can_close_reopen_and_then_navigate_files(
    page, backend_url, auth_token, width,
):
    page.set_viewport_size({"width": width, "height": 844})
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      await app.newSession();
      const state = app._ensureTabState(app.currentId);
      state.messages = [{
        _k: 'recall-navigation-fixture', role: 'assistant',
        text: 'Recall navigation fixture', ts: Date.now(), elapsed: 1,
        turn_status: 'completed', model: 'e2e-model',
        memoryRecall: {
          id: 'recall-navigation-trace', count: 1, latency_ms: 5, status: 'ok',
          items: [{id: 'recall-navigation-item', kind: 'preference',
            content: 'Synthetic memory detail', _traceback: []}],
        },
      }];
      state._loaded = true;
      state.messagesReady = true;
      app._activateTabState(app.currentId);
      app.setMobileTab('chat');
    }""")
    trigger = page.locator(".memory-recall-trace:visible")
    expect(trigger).to_have_count(1)
    popover = page.locator(".memory-recall-global")
    for close_with_escape in [False, True]:
        trigger.click()
        expect(popover).to_be_visible()
        expect(popover).to_contain_text("Synthetic memory detail")
        page.wait_for_function("""() => {
          const node = document.querySelector('.memory-recall-global');
          if (!node) return false;
          const r = node.getBoundingClientRect();
          return getComputedStyle(node).visibility === 'visible'
            && r.left >= 0 && r.top >= 0
            && r.right <= innerWidth && r.bottom <= innerHeight;
        }""")
        if close_with_escape:
            page.keyboard.press("Escape")
        else:
            popover.locator(".memory-recall-head button").click()
        expect(popover).to_be_hidden()
    if width < 720:
        page.locator(".mobile-tab-bar button").nth(0).click()
    page.locator('.filelist li[data-path="README.md"]').click()
    expect(page.locator(".pane.preview .markdown:visible")).to_contain_text(
        "muselab e2e fixture"
    )
    expect(popover).to_be_hidden()
