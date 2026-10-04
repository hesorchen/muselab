"""Real MCP settings controls against synthetic configuration responses."""
import copy
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect
from .test_settings_save_lifecycle import (
    settings_fixture as settings_fixture,
    settings_frontend_url as settings_frontend_url,
)


@pytest.fixture
def mcp_fixture(page, settings_fixture, request):
    state = {"disabled": False, "hold_next": False, "pending": [], "requests": []}
    page.evaluate("""() => {
      const a = document.querySelector('#app')._x_dataStack[0];
      const refresh = a.refreshMcpList;
      window.__mcpListSettled = 0;
      a.refreshMcpList = function(...args) {
        const promise = refresh.apply(this, args);
        promise.then(() => window.__mcpListSettled++, () => window.__mcpListSettled++);
        return promise;
      };
    }""")

    def payload():
        return {"servers": [{"name": "synthetic-mcp", "command": "synthetic-no-execution",
                             "args": [], "disabled": state["disabled"]}], "examples": []}

    def handle(route):
        method = route.request.method
        path = urlsplit(route.request.url).path
        state["requests"].append({"method": method, "path": path})
        if method == "GET" and path == "/api/settings/mcp":
            if state["hold_next"]:
                state["hold_next"] = False
                state["pending"].append((route, copy.deepcopy(payload())))
            else:
                route.fulfill(json=payload())
        elif method == "PATCH" and path == "/api/settings/mcp/synthetic-mcp/toggle":
            state["disabled"] = route.request.post_data_json["disabled"]
            route.fulfill(json={"ok": True})
        else:
            raise AssertionError(f"unexpected synthetic MCP API: {method} {path}")

    page.route("**/api/settings/mcp**", handle)
    page.locator(".settings-menu-item").filter(has_text="Extensions").click()
    expect(page.locator(".mcp-server-row input[type=checkbox]")).to_be_checked()
    yield state
    receipt = {"test": request.node.name, "requests": state["requests"],
               "server_disabled": state["disabled"],
               "visible_disabled": page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settings.mcpServers[0]?.disabled"),
               "settings_visible": page.locator(".settings-modal").is_visible(),
               "settled": page.evaluate("() => window.__mcpListSettled"),
               "pageerrors": settings_fixture["errors"]}
    if os.environ.get("MUSELAB_MCP_RECEIPTS"):
        directory = Path(os.environ["MUSELAB_MCP_RECEIPTS"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (request.node.name.replace("/", "_") + ".json")).write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8",
        )
    for route, _ in state["pending"]:
        try:
            route.abort()
        except Exception:
            pass
    assert settings_fixture["errors"] == []


def hold_old_read(page, state):
    state["hold_next"] = True
    page.locator(".settings-menu-item").filter(has_text="Extensions").click()
    deadline = time.monotonic() + 5
    while not state["pending"] and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(state["pending"]) == 1


def release_old_read(page, state, status=200):
    settled = page.evaluate("() => window.__mcpListSettled")
    route, old = state["pending"].pop()
    route.fulfill(status=status, json=old if status == 200 else {"detail": "synthetic unavailable"})
    page.wait_for_function("count => window.__mcpListSettled > count", arg=settled)


def test_mcp_old_list_does_not_revert_confirmed_toggle(page, mcp_fixture):
    checkbox = page.locator(".mcp-server-row input[type=checkbox]")
    hold_old_read(page, mcp_fixture)
    page.locator(".mcp-server-row .switch-slider").click()
    page.wait_for_function("() => window.__mcpListSettled === 2")
    assert mcp_fixture["disabled"] is True
    expect(checkbox).not_to_be_checked()
    release_old_read(page, mcp_fixture)
    expect(checkbox).not_to_be_checked()


@pytest.mark.parametrize("status", [200, 503])
def test_mcp_reopen_keeps_current_list_when_old_read_finishes(page, mcp_fixture, status):
    hold_old_read(page, mcp_fixture)
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    mcp_fixture["disabled"] = True  # A synthetic configuration update while closed.
    page.locator('[title="Settings"]').click()
    page.locator(".settings-menu-item").filter(has_text="Extensions").click()
    checkbox = page.locator(".mcp-server-row input[type=checkbox]")
    expect(checkbox).not_to_be_checked()
    page.wait_for_function("() => window.__mcpListSettled === 2")
    release_old_read(page, mcp_fixture, status)
    expect(checkbox).not_to_be_checked()
    expect(page.locator(".settings-modal")).to_be_visible()
