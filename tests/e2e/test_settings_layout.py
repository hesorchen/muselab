"""Settings refresh: Save states its scope, providers in use come first."""
from __future__ import annotations

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def _open(page, section):
    _app_eval(page, "await app.openSettings(arg); "
              "while (app.settings.loading) await new Promise(r => setTimeout(r, 20));", section)


def test_save_button_counts_pending_drafts_and_live_pages_say_so(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setLang('en')")
    _open(page, "defaults")
    save = page.locator(".settings-modal .modal-foot .btn-primary:visible")
    note = page.locator(".settings-foot-note")
    expect(save).to_be_disabled()
    expect(note).to_have_text("No unsaved changes")
    field = page.locator("#setting-settings-draftdefaults-permission")
    current = field.input_value()
    field.select_option("acceptEdits" if current != "acceptEdits" else "default")
    expect(save).to_be_enabled()
    expect(note).to_have_text("1 unsaved change")
    expect(note).to_have_class("settings-foot-note pending")
    field.select_option(current)
    expect(save).to_be_disabled()
    _app_eval(page, "app.selectSettingsPage('general')")
    expect(note).to_have_text("Changes on this page apply immediately")
    expect(page.locator(".settings-modal .modal-foot .btn-primary:visible")).to_have_count(0)


def test_provider_page_lists_providers_in_use_first(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setLang('en')")
    _open(page, "provider")
    section = page.locator('.settings-section[data-page="provider"]')
    providers = _app_eval(page, "return app.settings.providers.filter(p => p.editable)"
                                ".map(p => ({id: p.id, configured: !!p.configured, builtin: !!p.is_builtin}));")
    keyless = [p for p in providers if not p["configured"] and p["builtin"]]
    rows = section.locator(".provider-list .provider-row:not(.provider-ducc):visible")
    expect(rows).to_have_count(len(providers) - len(keyless))
    if keyless:
        toggle = section.locator(".settings-fold-btn")
        expect(toggle).to_contain_text(f"({len(keyless)})")
        toggle.click()
        expect(rows).to_have_count(len(providers))
        order = _app_eval(page, "return app.settingsProviderRows().map(p => !!p.configured);")
        assert order == sorted(order, reverse=True)
    if _app_eval(page, "return app.settingsDuccModelCount();"):
        expect(section.locator(".provider-ducc")).to_be_visible()
    # Claude auth and the context budget stay reachable, just folded.
    expect(section.locator(".claude-fold")).to_have_count(1)
    expect(page.get_by_test_id("context-limits-settings")).to_have_count(1)
