"""Settings save failures and late responses stay with their own form."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


_STATIC_SERVER = r'''
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import sys
from urllib.parse import urlsplit

class FrontendOnly(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = urlsplit(path).path
        if path.startswith("/static/"):
            path = path[len("/static"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])),
                    partial(FrontendOnly, directory=sys.argv[1])).serve_forever()
'''


@pytest.fixture
def settings_frontend_url(tmp_path):
    # This server imports no MuseLab backend, auth/config code or SDK. It only
    # serves the actual frontend; every API request is handled by the fixture.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = tmp_path / "isolated"
    root.mkdir()
    env = {**os.environ, **{key: str(root / value) for key, value in {
        "MUSELAB_ROOT": "workspace", "MUSELAB_ENV_PATH": "runtime.env",
        "MUSELAB_SESSIONS_DIR": "sessions", "MUSELAB_CONFIG_DIR": "config",
        "MUSELAB_MEMORY_DIR": "memory", "XDG_STATE_HOME": "state",
    }.items()}, "MUSELAB_TOKEN": "settings-synthetic-token-min-32-chars"}
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    with (root / "static-server.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", _STATIC_SERVER, str(frontend), str(port)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("isolated static server exited")
                try:
                    with urllib.request.urlopen(base + "/static/app.js", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.025)
            else:
                raise RuntimeError("isolated static server did not start")
            yield base
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.fixture
def settings_fixture(page, settings_frontend_url):
    settings = {
        "providers": [{"id": "fixture", "name": "Synthetic provider", "env_key": "FIXTURE_API_KEY",
                       "models": ["fixture-model"], "base_url": "https://provider.example.test",
                       "has_key": True, "models_editable": False}],
        "defaults": {"model": "fixture-model", "permission": "default", "busy_send_mode": "adjust"},
        "context_groups": [], "context_limits": {"providers": {}, "models": {}},
        "params": {},
    }
    pending = []
    requests = []
    failures = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append((request.method, path))
        if path == "/api/settings":
            if request.method == "PUT":
                body = request.post_data_json
                if failures:
                    if failures.pop() == "network":
                        route.abort("failed")
                    else:
                        route.fulfill(status=200, content_type="application/json", body="{")
                    return
                settings["defaults"] = {
                    "model": body["default_model"], "permission": body["default_permission"],
                    "busy_send_mode": body["busy_send_mode"],
                }
                pending.append(route)
                return
            route.fulfill(json=copy.deepcopy(settings))
        elif path == "/api/chat/providers":
            route.fulfill(json={"models": [{"model": "fixture-model", "label": "Fixture", "group": "Fixture"}]})
        elif path == "/api/chat/context-info":
            route.fulfill(json={"has_any_provider": True})
        elif path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            # All optional reads/writes stay synthetic too; the static server
            # has no settings/backend routes that could touch real config.
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.lang = 'en'; app.token = 'settings-synthetic-token-min-32-chars';
      app.authed = true; app.appReady = true;
      app.availableModels = [{model:'fixture-model',label:'Fixture',group:'Fixture'}];
      window.__settingsUnhandled = [];
      window.__settingsSaveSettled = 0;
      const save = app.saveSettings;
      app.saveSettings = function(...args) {
        const promise = save.apply(this, args);
        promise.then(() => { window.__settingsSaveSettled += 1; },
                     () => { window.__settingsSaveSettled += 1; });
        return promise;
      };
      window.addEventListener('unhandledrejection', event => {
        window.__settingsUnhandled.push(String(event.reason?.message || event.reason));
      });
    }""")
    page.evaluate("() => document.querySelector('#app')._x_dataStack[0].openSettings('defaults')")
    expect(page.locator(".settings-modal")).to_be_visible()
    expect(page.locator("#setting-settings-draftdefaults-permission")).to_have_value("default")
    yield {"pending": pending, "requests": requests, "failures": failures, "settings": settings, "errors": errors}
    for route in pending:
        try:
            route.abort()
        except Exception:
            pass


def _save(page):
    return page.locator(".settings-modal .modal-foot .btn-primary:visible")


def _wait_pending(page, fixture, count):
    deadline = time.monotonic() + 5
    while len(fixture["pending"]) < count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(fixture["pending"]) >= count


def _finish(page, route):
    settled = page.evaluate("() => window.__settingsSaveSettled")
    route.fulfill(status=200, json={"updated_count": 1})
    # Observe the actual Save promise's completion, including catalog refresh.
    # A currently-visible form must not make the late-response check pass early.
    page.wait_for_function("count => window.__settingsSaveSettled > count", arg=settled)


def test_settings_save_failure_keeps_draft_and_explains_retry(page, settings_fixture):
    field = page.locator("#setting-settings-draftdefaults-permission")
    field.select_option("acceptEdits")
    settings_fixture["failures"].append("network")
    _save(page).click()
    page.wait_for_function("() => window.__settingsSaveSettled === 1")
    expect(page.locator(".toast").filter(has_text="Save failed")).to_be_visible()
    expect(field).to_have_value("acceptEdits")
    expect(_save(page)).to_be_enabled()
    assert page.evaluate("() => window.__settingsUnhandled") == []
    assert settings_fixture["errors"] == []
    settings_fixture["failures"].append("json")
    _save(page).click()
    page.wait_for_function("() => window.__settingsSaveSettled === 2")
    expect(page.locator(".toast").filter(has_text="Could not confirm the save response")).to_be_visible()
    expect(field).to_have_value("acceptEdits")
    expect(_save(page)).to_be_enabled()
    assert page.evaluate("() => window.__settingsUnhandled") == []
    assert settings_fixture["errors"] == []
    _save(page).click()
    _wait_pending(page, settings_fixture, 1)
    _finish(page, settings_fixture["pending"].pop())
    expect(page.locator(".settings-modal")).to_be_hidden()


def test_settings_pending_save_preserves_newer_visible_edits(page, settings_fixture):
    field = page.locator("#setting-settings-draftdefaults-permission")
    field.select_option("acceptEdits")
    _save(page).click()
    _wait_pending(page, settings_fixture, 1)
    field.select_option("bypassPermissions")
    _finish(page, settings_fixture["pending"].pop())
    expect(field).to_have_value("bypassPermissions")
    expect(page.locator(".settings-modal")).to_be_visible()
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settingsDirty()")
    expect(_save(page)).to_be_enabled()


def _reopen_fresh_form(page):
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".confirm-modal")).to_be_visible()
    page.locator(".confirm-modal .modal-foot button").last.click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    page.evaluate("() => document.querySelector('#app')._x_dataStack[0].openSettings('defaults')")
    expect(page.locator(".settings-modal")).to_be_visible()
    # Discard the retained old draft and reopen a freshly read form while the
    # older successful Save response is still held.
    page.locator(".settings-draft-notice button").click()
    page.locator(".confirm-modal .modal-foot button").last.click()
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    page.evaluate("() => document.querySelector('#app')._x_dataStack[0].openSettings('defaults')")
    expect(page.locator(".settings-modal")).to_be_visible()


def test_settings_late_save_does_not_close_a_reopened_fresh_form(page, settings_fixture):
    field = page.locator("#setting-settings-draftdefaults-permission")
    field.select_option("acceptEdits")
    _save(page).click()
    _wait_pending(page, settings_fixture, 1)
    _reopen_fresh_form(page)
    expect(field).to_have_value("acceptEdits")
    assert not page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settingsDirty()")
    _finish(page, settings_fixture["pending"].pop())
    # Complete the old real Promise before checking the new clean form. This
    # core regression does not depend on another Save being in progress.
    expect(page.locator(".settings-modal")).to_be_visible()
    expect(field).to_have_value("acceptEdits")
    assert not page.evaluate("() => document.querySelector('#app')._x_dataStack[0].settingsDirty()")

    # Repeat the same close/reopen boundary with a Save from the newer form,
    # using its real enabled button, to protect that request's pending state.
    field.select_option("default")
    _save(page).click()
    _wait_pending(page, settings_fixture, 1)
    _reopen_fresh_form(page)
    expect(field).to_have_value("default")
    expect(_save(page)).to_be_enabled()
    _save(page).click()
    _wait_pending(page, settings_fixture, 2)
    _finish(page, settings_fixture["pending"].pop(0))
    expect(page.locator(".settings-modal")).to_be_visible()
    expect(field).to_have_value("default")
    expect(_save(page)).to_be_disabled()
    assert page.evaluate("() => document.querySelector('#app')._x_dataStack[0].defaultPermission") == "default"
    _finish(page, settings_fixture["pending"].pop())
    expect(page.locator(".settings-modal")).to_be_hidden()
