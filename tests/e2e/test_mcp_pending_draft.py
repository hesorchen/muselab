"""A successful MCP creation leaves edits made during its request intact."""
import time

import pytest
from playwright.sync_api import expect

from tests.e2e.test_settings_drafts import _login


@pytest.mark.parametrize("edit_while_pending", [True, False])
def test_mcp_form_preserves_edits_during_save(
    page, backend_url, auth_token, edit_while_pending,
):
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const a = document.querySelector('#app')._x_dataStack[0];
      await a.openSettings('extensions');
      a.settings.mcpDraft.show = true;
      const add = a.addMcpFromDraft.bind(a);
      a.addMcpFromDraft = async () => {
        try { return await add(); }
        finally { window.__mcpSaveDone = true; }
      };
    }""")
    name = page.locator('[x-model="settings.mcpDraft.name"]')
    command = page.locator('[x-model="settings.mcpDraft.command"]')
    args = page.locator('[x-model="settings.mcpDraft.argsStr"]')
    server = "draft-mcp-edited" if edit_while_pending else "draft-mcp-plain"
    name.fill(server)
    command.fill("/bin/false")
    args.fill("submitted-argument")
    held = []

    def hold_creation(route):
        held.append((route, route.fetch()))

    page.route("**/api/settings/mcp/" + server, hold_creation)
    page.locator(".mcp-add-form .btn-primary").click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held) == 1
    assert held[0][1].status == 200
    try:
        if edit_while_pending:
            name.fill("next-mcp")
            args.fill("new-unsaved-argument")
        held[0][0].fulfill(response=held[0][1])
        page.wait_for_function("() => window.__mcpSaveDone === true")
        if edit_while_pending:
            expect(name).to_be_visible()
            expect(name).to_have_value("next-mcp")
            expect(args).to_have_value("new-unsaved-argument")
            assert page.evaluate(
                "() => document.querySelector('#app')._x_dataStack[0].settingsDirty()",
            )
        else:
            expect(name).to_be_hidden()
            assert page.evaluate("""() => {
              const d = document.querySelector('#app')._x_dataStack[0].settings.mcpDraft;
              return !d.name && !d.command && !d.argsStr;
            }""")
    finally:
        cleanup = page.request.delete(
            backend_url + "/api/settings/mcp/" + server,
            headers={"X-Auth-Token": auth_token},
        )
        assert cleanup.status == 200
