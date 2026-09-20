"""A provider creation response must not discard edits typed during the save."""
import time

import pytest
from playwright.sync_api import expect

from tests.e2e.test_settings_drafts import _login


@pytest.mark.parametrize("edit_while_pending", [True, False])
def test_new_provider_form_preserves_edits_during_save(
    page, backend_url, auth_token, edit_while_pending,
):
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const a = document.querySelector('#app')._x_dataStack[0];
      await a.openSettings('provider');
      a.settings.providerNew.show = true;
      const add = a.addProviderFromDraft.bind(a);
      a.addProviderFromDraft = async () => {
        try { return await add(); }
        finally { window.__newProviderSaveDone = true; }
      };
    }""")
    base_url = page.locator('[x-model="settings.providerNew.base_url"]')
    prefix = page.locator('[x-model="settings.providerNew.prefix"]')
    models = page.locator('[x-model="settings.providerNew.models"]')
    name = "pending-edit" if edit_while_pending else "unchanged-save"
    base_url.fill(f"https://{name}.example.test/anthropic")
    prefix.fill(name + ":")
    models.fill(name + ":model")
    held = []

    def hold_creation(route):
        held.append((route, route.fetch()))

    page.route("**/api/settings/providers", hold_creation)
    page.locator('[x-show="settings.providerNew.show"] .btn-primary').click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held) == 1
    assert held[0][1].status == 200
    try:
        if edit_while_pending:
            prefix.fill("next-provider:")
            models.fill("next-provider:model")
        held[0][0].fulfill(response=held[0][1])
        page.wait_for_function("() => window.__newProviderSaveDone === true")
        if edit_while_pending:
            expect(prefix).to_be_visible()
            expect(prefix).to_have_value("next-provider:")
            expect(models).to_have_value("next-provider:model")
            assert page.evaluate(
                "() => document.querySelector('#app')._x_dataStack[0].settingsDirty()",
            )
        else:
            expect(prefix).to_be_hidden()
            assert page.evaluate("""() => {
              const n = document.querySelector('#app')._x_dataStack[0].settings.providerNew;
              return !n.base_url && !n.prefix && !n.models && !n.api_key;
            }""")
    finally:
        cleanup = page.request.post(
            backend_url + "/api/settings/providers/delete",
            headers={"X-Auth-Token": auth_token},
            data={"id": held[0][1].json()["id"]},
        )
        assert cleanup.status == 200
