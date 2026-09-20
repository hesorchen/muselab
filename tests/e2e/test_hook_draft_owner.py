"""A completed hook request must not dismiss a replacement editor."""
import json
import time
import uuid

import pytest
from playwright.sync_api import expect

from tests.e2e.test_settings_drafts import _login


@pytest.mark.parametrize("replacement", ["none", "cancel-and-add", "switch-scope"])
def test_hook_save_respects_draft_owner(page, backend_url, auth_token, replacement):
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const a = document.querySelector('#app')._x_dataStack[0];
      await a.openSettings('hooks');
      a.settings.hooks.activeScope = 'project';
      a.startAddHook();
      const save = a.saveHookDraft.bind(a);
      a.saveHookDraft = async () => {
        try { return await save(); }
        finally { window.__hookSaveDone = true; }
      };
    }""")
    matcher = page.locator('[x-model="settings.hooks.draft.matcher"]')
    handler = page.locator('[x-model="settings.hooks.draft.handlerJson"]')
    unique_matcher = "pending-" + uuid.uuid4().hex
    matcher.fill(unique_matcher)
    handler.fill(json.dumps({"type": "command", "command": "echo submitted"}))
    held = []

    def hold_save(route):
        held.append((route, route.fetch()))

    page.route("**/api/settings/hooks/project/handlers", hold_save)
    page.locator('.hook-editor .btn-primary').click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held) == 1
    response = held[0][1]
    assert response.ok
    try:
        if replacement != "none":
            if replacement == "cancel-and-add":
                page.locator('.hook-editor-actions .btn-ghost').click()
            else:
                page.locator('.hook-scope-tabs .seg-btn').nth(2).click()
            page.locator('.hook-add-row .btn-ghost').click()
            matcher.fill("next-unsaved-hook")
            handler.fill(json.dumps({"type": "command", "command": "echo newer"}))
        held[0][0].fulfill(response=response)
        page.wait_for_function("() => window.__hookSaveDone === true")
        if replacement == "none":
            expect(handler).to_be_hidden()
        else:
            expect(handler).to_be_visible()
            expect(matcher).to_have_value("next-unsaved-hook")
            expect(handler).to_have_value(json.dumps({"type": "command", "command": "echo newer"}))
            expect(page.locator('.hook-editor .btn-primary')).to_be_enabled()
            assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settingsDirty()")
        headers = {"X-Auth-Token": auth_token}
        saved = page.request.get(backend_url + "/api/settings/hooks/project", headers=headers).json()
        matches = [g for g in saved["hooks"]["PreToolUse"] if g.get("matcher") == unique_matcher]
        assert len(matches) == 1
        assert matches[0]["hooks"][0]["command"] == "echo submitted"
    finally:
        page.unroute("**/api/settings/hooks/project/handlers", hold_save)
        headers = {"X-Auth-Token": auth_token}
        saved = page.request.get(backend_url + "/api/settings/hooks/project", headers=headers).json()
        groups = saved["hooks"].get("PreToolUse", [])
        index = next(i for i, group in enumerate(groups) if group.get("matcher") == unique_matcher)
        cleaned = page.request.delete(backend_url + "/api/settings/hooks/project/handlers", headers=headers, data={
            "revision": saved["revision"], "event": "PreToolUse", "group_index": index, "handler_index": 0,
        })
        assert cleaned.ok
