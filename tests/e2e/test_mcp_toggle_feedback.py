"""MCP native slider agrees with confirmed synthetic configuration."""
import json
import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect
from .test_mcp_list_lifecycle import mcp_fixture as mcp_fixture
from .test_settings_save_lifecycle import (
    settings_fixture as settings_fixture,
    settings_frontend_url as settings_frontend_url,
)


@pytest.mark.parametrize("status", [503, 200])
def test_mcp_slider_reflects_confirmed_toggle_result(page, mcp_fixture, status, request):
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const toggle = app.toggleMcp;
      window.__mcpToggleSettled = 0;
      app.toggleMcp = function(...args) {
        const pending = toggle.apply(this, args);
        pending.then(() => window.__mcpToggleSettled++, () => window.__mcpToggleSettled++);
        return pending;
      };
    }""")
    pending = []
    page.route("**/api/settings/mcp/synthetic-mcp/toggle", lambda route: pending.append(route))
    checkbox = page.locator(".mcp-server-row input[type=checkbox]")
    page.locator(".mcp-server-row .switch-slider").click()
    deadline = time.monotonic() + 5
    while not pending and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(pending) == 1
    assert pending[0].request.method == "PATCH"
    assert pending[0].request.post_data_json == {"disabled": True}
    if status == 200:
        mcp_fixture["disabled"] = True
    pending.pop().fulfill(status=status, json={"ok": status == 200, "detail": "synthetic read unavailable"})
    page.wait_for_function("() => window.__mcpToggleSettled === 1")
    if status == 200:
        page.wait_for_function("() => window.__mcpListSettled === 2")
    receipt = {"status": status, "checkbox_checked": checkbox.is_checked(),
               "server_disabled": mcp_fixture["disabled"],
               "model_disabled": page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settings.mcpServers[0].disabled"),
               "mutation_settled": page.evaluate("() => window.__mcpToggleSettled"),
               "list_settled": page.evaluate("() => window.__mcpListSettled"),
               "toast": page.locator(".toast:visible").all_text_contents()}
    if os.environ.get("MUSELAB_MCP_RECEIPTS"):
        root = Path(os.environ["MUSELAB_MCP_RECEIPTS"])
        root.mkdir(parents=True, exist_ok=True)
        (root / (request.node.name + "-mutation.json")).write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8",
        )
    assert receipt["model_disabled"] is (status == 200)
    if status == 503:
        expect(page.locator(".toast").filter(has_text="Save failed")).to_be_visible()
        expect(checkbox).to_be_checked()
    else:
        expect(checkbox).not_to_be_checked()
