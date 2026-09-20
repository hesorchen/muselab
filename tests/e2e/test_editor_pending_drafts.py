"""Real saves must preserve fields edited while their responses are pending."""
import time
import uuid

import pytest
from playwright.sync_api import expect

from tests.e2e.test_settings_drafts import _login


@pytest.mark.parametrize("kind", ["terminal", "group"])
@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("change", [False, True])
def test_editor_keeps_pending_changes(page, backend_url, auth_token, kind, editing, change):
    _login(page, backend_url, auth_token)
    endpoint = "/api/terminals/profiles" if kind == "terminal" else "/api/activity/groups"
    headers = {"X-Auth-Token": auth_token}
    initial_name = "pending-" + uuid.uuid4().hex[:12]
    record = None
    created_id = ""
    if editing:
        payload = {"name": initial_name, "command": "echo original"} if kind == "terminal" else {
            "name": initial_name, "color": "blue",
        }
        response = page.request.post(backend_url + endpoint, headers=headers, data=payload)
        assert response.ok
        record = response.json() if kind == "terminal" else response.json()["group"]
        created_id = record["id"]
    page.evaluate("""({kind, record}) => {
      const a = document.querySelector('#app')._x_dataStack[0];
      let method;
      if (kind === 'terminal') {
        a.terminalManagerOpen = true;
        a.editTerminalProfile(record);
        method = 'saveTerminalProfile';
      } else {
        a.activity.show = true;
        a.activity.view = 'groups';
        a.openActivityGroupEditor(record);
        method = 'saveActivityGroup';
      }
      const save = a[method].bind(a);
      a[method] = async () => {
        try { return await save(); }
        finally { window.__editorSaveDone = true; }
      };
    }""", {"kind": kind, "record": record})
    prefix = "terminalProfileEditor" if kind == "terminal" else "activity.groupEditor"
    name = page.locator('[x-model="' + prefix + '.name"]')
    name.fill(initial_name)
    if kind == "terminal":
        page.locator('[x-model="terminalProfileEditor.command"]').fill("echo submitted")
    held = []

    def hold_save(route):
        if route.request.method in {"POST", "PATCH"}:
            held.append((route, route.fetch()))
        else:
            route.continue_()

    url = backend_url + endpoint + ("/" + created_id if editing else "")
    page.route(url, hold_save)
    save_button = page.locator(
        '.terminal-profile-editor .btn-primary' if kind == "terminal"
        else '.activity-group-editor [type="submit"]',
    )
    try:
        save_button.click()
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1
        assert held[0][1].ok
        saved = held[0][1].json()
        created_id = saved["id"] if kind == "terminal" else saved["group"]["id"]
        if change:
            name.fill(initial_name + "-edited")
            if kind == "terminal":
                page.locator('[x-model="terminalProfileEditor.command"]').fill("echo edited")
            else:
                page.locator('.activity-group-swatch.is-green').click()
        held[0][0].fulfill(response=held[0][1])
        page.wait_for_function("() => window.__editorSaveDone === true")
        page.unroute(url, hold_save)
        if change:
            expect(name).to_be_visible()
            expect(name).to_have_value(initial_name + "-edited")
            expect(save_button).to_be_enabled()
            if kind == "terminal":
                expect(page.locator('[x-model="terminalProfileEditor.command"]')).to_have_value("echo edited")
            else:
                expect(page.locator('.activity-group-swatch.is-green')).to_have_attribute('aria-pressed', 'true')
            with page.expect_response(lambda r: r.request.method == "PATCH" and r.url == backend_url + endpoint + "/" + created_id) as updated:
                save_button.click()
            assert updated.value.ok
            row = updated.value.json()
            row = row if kind == "terminal" else row["group"]
            assert row["id"] == created_id
            assert row["name"] == initial_name + "-edited"
            assert row["command" if kind == "terminal" else "color"] == (
                "echo edited" if kind == "terminal" else "green"
            )
        expect(name).to_be_hidden()
    finally:
        if created_id:
            response = page.request.delete(backend_url + endpoint + "/" + created_id, headers=headers)
            assert response.ok
