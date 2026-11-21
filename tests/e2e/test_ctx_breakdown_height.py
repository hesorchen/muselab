"""The context-breakdown popup fits its usual content without scrolling.

The popup was capped at 520 px and its body at 60vh, so a normal breakdown
(about ten categories plus the deferred block and the footnote) always
scrolled on a phone even with free space above the composer.
"""
from __future__ import annotations

import json

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
    && app.openTabIds.includes(app.currentId);
}"""

BREAKDOWN = {
    "totalTokens": 462_800,
    "maxTokens": 1_000_000,
    "rawMaxTokens": 1_000_000,
    "percentage": 46.0,
    "model": "claude-opus-5-5",
    "categories": [
        {"name": "System prompt", "tokens": 74, "color": "promptBorder"},
        {"name": "System tools", "tokens": 10_900, "color": "inactive"},
        {"name": "MCP tools", "tokens": 567, "color": "cyan_FOR_SUBAGENTS_ONLY"},
        {"name": "Memory files", "tokens": 4_000, "color": "claude"},
        {"name": "Skills", "tokens": 6_200, "color": "warning"},
        {"name": "Messages", "tokens": 440_900, "color": "purple_FOR_SUBAGENTS_ONLY"},
        {"name": "Autocompact buffer", "tokens": 33_000, "color": "inactive"},
        {"name": "Free space", "tokens": 504_200, "color": "promptBorder"},
        {"name": "MCP tools (deferred)", "tokens": 696, "color": "inactive"},
        {"name": "System tools (deferred)", "tokens": 4_000, "color": "inactive"},
    ],
    "memoryFiles": [{"path": "/home/u/.claude/CLAUDE.md", "type": "User", "tokens": 4_000}],
    "mcpTools": [{"name": "memory_search", "serverName": "muselab-memory", "tokens": 567}],
    "skills": {"skillFrontmatter": [{"name": "a", "source": "user", "tokens": 6_200}]},
}


def _login(page, base: str, token: str) -> None:
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=20000)
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=20000)
    page.wait_for_function(READY, timeout=20000)


@pytest.mark.parametrize("viewport", [
    {"width": 390, "height": 844},
    {"width": 1440, "height": 900},
])
def test_ctx_breakdown_popup_shows_all_rows_without_scrolling(
    page, backend_url, auth_token, viewport,
):
    page.set_viewport_size(viewport)
    page.route("**/api/chat/context-breakdown/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(BREAKDOWN)))
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const app = document.querySelector("#app")._x_dataStack[0];
      await app.showCtxBreakdown();
    }""")
    pop = page.locator(".ctx-breakdown-pop")
    expect(pop).to_be_visible(timeout=5000)
    expect(pop.locator(".ctx-breakdown-hint")).to_be_attached(timeout=5000)
    page.wait_for_timeout(400)
    m = page.evaluate("""() => {
      const pop = document.querySelector(".ctx-breakdown-pop");
      const scrollers = [pop, ...pop.querySelectorAll(".ctx-breakdown-body")]
        .filter(el => el.getClientRects().length)
        .map(el => ({cls: el.className, sh: el.scrollHeight, ch: el.clientHeight}));
      const r = pop.getBoundingClientRect();
      return {scrollers, top: r.top, bottom: r.bottom, vh: innerHeight};
    }""")
    for s in m["scrollers"]:
        assert s["sh"] <= s["ch"] + 1, m
    assert m["top"] >= 0 and m["bottom"] <= m["vh"], m
