"""Settings list replacement retires old provider bindings without losing edits."""
from __future__ import annotations

from urllib.parse import urlsplit

from playwright.sync_api import expect

from .test_settings_save_lifecycle import (
    settings_fixture as settings_fixture,
    settings_frontend_url as settings_frontend_url,
)


def _provider(letter):
    # Full editable custom-provider schema from GET /api/settings. The
    # deleting peer changes only the synthetic API store, never Alpine state.
    pid = f"c:synthetic-{letter}"
    return {
        "kind": "third_party", "id": pid, "env_key": f"SYNTHETIC_{letter.upper()}_API_KEY",
        "display": f"Synthetic custom {letter.upper()}", "configured": False, "masked": "",
        "probe_model": pid + ":model", "disabled": False,
        "base_url": "https://synthetic.example.test/anthropic", "prefix": letter + ":",
        "models": [pid + ":model"], "supports_thinking": True, "supports_effort": False,
        "is_builtin": False, "is_overridden": False, "editable": True,
    }


def _row(page, letter):
    return page.locator(".provider-row").filter(
        has=page.locator("label span", has_text=f"Synthetic custom {letter.upper()}"),
    )


def _editor_button(row):
    return row.locator('[\\@click="toggleProviderEditor(p)"]').first


def _toolbar_open(page):
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/settings") as read:
        page.locator('button[\\@click="openSettings()"].icon-btn').click()
    assert read.value.status == 200
    return read.value.json()["providers"]


def _delete_while_closed(page, settings_fixture, settings_frontend_url):
    settings_fixture["settings"]["providers"] += [_provider("a"), _provider("b")]
    page.evaluate("() => document.querySelector('#app')._x_dataStack[0].openSettings('provider')")
    _editor_button(_row(page, "a")).click()
    expect(_row(page, "a").locator(".provider-editor .btn-ghost.danger")).to_have_text("Delete")
    retired_prefix = _row(page, "a").locator(".provider-editor input").nth(1).element_handle()
    assert settings_fixture["errors"] == []
    assert not page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settingsDirty()")
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    peer = page.context.new_page()
    deleted = []

    def delete(route):
        body = route.request.post_data_json
        assert body == {"id": "c:synthetic-a"}
        settings_fixture["settings"]["providers"] = [
            provider for provider in settings_fixture["settings"]["providers"]
            if provider["id"] != body["id"]
        ]
        deleted.append(True)
        route.fulfill(json={"ok": True, "changed": True})

    peer.route("**/synthetic-peer", lambda route: route.fulfill(content_type="text/html", body="<html></html>"))
    peer.route("**/api/settings/providers/delete", delete)
    try:
        peer.goto(settings_frontend_url + "/synthetic-peer")
        assert peer.evaluate("""async () => (await fetch('/api/settings/providers/delete', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:'c:synthetic-a'}),
        })).status""") == 200
        assert deleted == [True]
    finally:
        peer.close()
    return retired_prefix


def _assert_clean_reopen(page, settings_fixture):
    page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].settings.loading")
    expect(_row(page, "a")).to_have_count(0)
    expect(_row(page, "b")).to_be_visible()
    assert settings_fixture["errors"] == []
    assert not page.locator("#jserr").is_visible()
    assert page.evaluate("""() => !('c:synthetic-a' in
      document.querySelector('#app')._x_dataStack[0].settings.providerDrafts)""")


def test_deleted_custom_provider_does_not_poison_reopened_settings(page, settings_fixture, settings_frontend_url):
    _delete_while_closed(page, settings_fixture, settings_frontend_url)
    returned = _toolbar_open(page)
    assert not any(provider["id"] == "c:synthetic-a" for provider in returned)
    assert any(provider["id"] == "c:synthetic-b" for provider in returned)
    _assert_clean_reopen(page, settings_fixture)
    row = _row(page, "b")
    _editor_button(row).click()
    prefix = row.locator(".provider-editor input").nth(1)
    expect(prefix).to_be_visible()
    prefix.fill("retained-b:")
    page.locator(".settings-modal .modal-close").click()
    page.locator(".confirm-modal .modal-foot button").last.click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    page.locator('button[\\@click="openSettings()"].icon-btn').click()
    expect(prefix).to_have_value("retained-b:")
    expect(prefix).to_be_visible()
    assert settings_fixture["errors"] == []



def test_retired_provider_input_does_not_restore_deleted_draft(page, settings_fixture, settings_frontend_url):
    retired_prefix = _delete_while_closed(page, settings_fixture, settings_frontend_url)
    _toolbar_open(page)
    _assert_clean_reopen(page, settings_fixture)
    result = retired_prefix.evaluate("""el => {
      const before = el._x_model.get();
      el.value = 'retired-input:';
      el.dispatchEvent(new Event('input', {bubbles:true}));
      return {connected:el.isConnected, before, after:el._x_model.get()};
    }""")
    assert result == {"connected": False, "before": "", "after": ""}
    _assert_clean_reopen(page, settings_fixture)
    assert page.evaluate("""() => document.querySelector('#app')._x_dataStack[0]
      .settings.providerDrafts['c:synthetic-b'].prefix""") == "b:"
