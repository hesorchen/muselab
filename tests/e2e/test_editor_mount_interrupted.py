"""An editor mount that is interrupted while CodeMirror loads never leaves
"Loading editor…" up forever; it offers a retry that mounts the editor."""
from __future__ import annotations

import time

from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_interrupted_editor_mount_offers_retry(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/static/vendor/cm/codemirror.min.js*",
               lambda route: (time.sleep(2.0), route.continue_()))
    name = f"interrupted-{int(time.time() * 1000)}.txt"
    _app_eval(page, "app.prompt = async () => arg; void app.doNewFile({path: ''});", name)
    page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0].editorLoading")
    owner = _app_eval(page, "return app.fileWorkspacePath();")
    # Stand-in for an owner change racing the asset load.
    _app_eval(page, "app.activeWorkspace = '/tmp/other-editor-owner';")
    page.wait_for_function(
        "() => !document.querySelector('#app')._x_dataStack[0].editorLoading",
        timeout=10000)
    expect(page.locator(".editor-notice[role=status]").first).to_be_hidden()
    retry = page.locator(".editor-notice[role=alert]:visible button").first
    expect(retry).to_be_visible()
    _app_eval(page, "app.activeWorkspace = arg;", owner)
    retry.click()
    expect(page.locator(".editor-cm .CodeMirror")).to_be_visible(timeout=10000)
    assert _app_eval(page, "return app.editorLoading;") is False
