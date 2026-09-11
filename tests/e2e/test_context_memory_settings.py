"""Settings budgets and maintenance diagnostics work through actual controls."""
import pytest
from playwright.sync_api import expect

from tests.e2e.test_settings_drafts import _login


@pytest.mark.parametrize("width", [390, 1440])
def test_context_budget_save_reload_and_reset(page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _login(page, backend_url, auth_token)
    def open_form():
        page.evaluate("() => document.querySelector('#app')._x_dataStack[0].openSettings('provider')")
        panel = page.get_by_test_id("context-limits-settings")
        panel.locator("summary").click()
        return panel
    panel = open_form()
    try:
        page.locator("#context-provider").select_option("ducc")
        page.locator("#context-tokens").fill("350000")
        with page.expect_response(lambda r: "/api/settings/context-limits" in r.url and r.request.method == "PUT") as response:
            panel.locator("button").click()
        assert response.value.status == 200
        page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].settings.contextSaving")
        page.reload()
        page.wait_for_selector(".chat-tabs-list")
        panel = open_form()
        expect(page.locator("#context-tokens")).to_have_value("350000")
        page.locator("#context-model").select_option("ducc:glm-5")
        page.locator("#context-tokens").fill("250000")
        with page.expect_response("**/api/settings/context-limits") as response:
            panel.locator("button").click()
        assert response.value.json()["context_limits"]["models"]["ducc:glm-5"] == 250000
        page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].settings.contextSaving")
        page.locator("#context-tokens").fill("")
        with page.expect_response("**/api/settings/context-limits") as response:
            panel.locator("button").click()
        assert "ducc:glm-5" not in response.value.json()["context_limits"]["models"]
        assert errors == []
    finally:
        for scope, key in [("providers", "ducc"), ("models", "ducc:glm-5")]:
            cleanup = page.request.put(backend_url + "/api/settings/context-limits",
                             headers={"X-Auth-Token": auth_token},
                             data={"scope": scope, "key": key, "tokens": None})
            assert cleanup.status == 200


def test_memory_diagnostics_render_safe_terminal_and_retry_states(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/api/memory/status", lambda route: route.fulfill(json={
        "enabled": True, "pending_index": 2, "last_success_at": 1700000000,
        "recent_jobs": [
            {"id": "job-a", "kind": "reindex_memories", "status": "queued", "attempts": 2,
             "category": "timeout", "reason": "timeout", "updated_at": 1700000000},
            {"id": "job-b", "kind": "cross_episode_dream", "status": "failed", "attempts": 3,
             "category": "malformed_response", "reason": "invalid_json", "updated_at": 1700000000},
        ],
        "reindex_progress": {"total_batches": 4, "done_batches": 2, "failed_batches": 1, "pending_batches": 1},
    }))
    page.evaluate("() => { const a=document.querySelector('#app')._x_dataStack[0]; a.lang='zh'; return a.openSettings('memory_engine'); }")
    panel = page.get_by_test_id("memory-diagnostics")
    panel.locator("summary").click()
    expect(panel).to_contain_text("等待重试")
    expect(panel).to_contain_text("失败")
    expect(panel).to_contain_text("2 / 4")
    expect(panel).to_contain_text("invalid_json")
    with page.expect_response("**/api/memory/status"):
        panel.locator("button").click()


@pytest.mark.parametrize("width", [390, 1440])
def test_recall_timeout_seconds_and_unlimited_save_reload(page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 900})
    _login(page, backend_url, auth_token)
    headers = {"X-Auth-Token": auth_token}
    original = page.request.get(backend_url + "/api/memory/config", headers=headers).json()

    def open_form():
        page.evaluate("""async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh'; await app.openSettings('memory_engine');
        }""")
        page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0].settings.memory.configLoaded")
        return page.locator('#setting-settings-memory-config-retrieval-soft-timeout-ms')

    try:
        field = open_form()
        expect(page.get_by_label('召回超时时间（秒）')).to_be_visible()
        expect(page.get_by_text('0 表示无超时。', exact=True)).to_be_visible()
        for seconds in (30, 0):
            field.fill(str(seconds))
            with page.expect_response(lambda r: '/api/memory/config' in r.url and r.request.method == 'PUT') as saved:
                page.get_by_role('button', name='保存记忆设置', exact=True).last.click()
            assert saved.value.status == 200
            assert saved.value.request.post_data_json['retrieval']['soft_timeout_ms'] == seconds * 1000
            page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].settings.memory.saving")
            page.reload()
            page.wait_for_selector('.chat-tabs-list')
            field = open_form()
            expect(field).to_have_value(str(seconds))
    finally:
        response = page.request.put(backend_url + '/api/memory/config?probe=false', headers=headers, data=original)
        assert response.status == 200
