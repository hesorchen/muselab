"""Browser-level smoke tests for the multi-tab chat UI.

These cover the regression classes that bit us during the 2026-05-17
multi-tab sprint and can ONLY be caught in a real browser:
- DOM event wiring (click, drag, contextmenu)
- Alpine x-effect / x-show / x-if reactivity races
- localStorage round-tripping (preview path, open tabs)
- document.title responding to streaming + session changes

Skipped by default. Enable with `RUN_E2E=1`. See tests/e2e/README.md."""
from __future__ import annotations
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


# Selectors mirror frontend/index.html. Centralised so a UI rename only
# breaks one place.
SEL_LOGIN = ".login"
SEL_LOGIN_INPUT = '.login input[type="password"]'
SEL_TABS = ".chat-tabs-list"
SEL_TAB = ".chat-tab"
SEL_TAB_ACTIVE = ".chat-tab.active"
SEL_TAB_NAME = ".chat-tab-name"
SEL_TAB_RENAME = ".chat-tab-rename-input"
SEL_TAB_CLOSE = ".chat-tab-close"
SEL_TAB_NEW = ".chat-tab-new"


def _activate_chat_tab(page: Page, sid: str) -> None:
    page.evaluate(
        """async ([target]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.activateTab(target);
        }""",
        arg=[sid],
    )
    page.wait_for_function(
        "([target]) => document.querySelector('#app')._x_dataStack[0].currentId === target",
        arg=[sid],
    )


def _login(page: Page, base: str, token: str) -> None:
    page.goto(base)
    # Wait for either the login screen or (if a token is already stored)
    # the tab strip to appear.
    page.wait_for_selector(f"{SEL_LOGIN}, {SEL_TABS}", state="visible", timeout=5000)
    if page.locator(SEL_LOGIN).is_visible():
        page.fill(SEL_LOGIN_INPUT, token)
        page.keyboard.press("Enter")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app.authed === true && app._modelsLoaded && app.currentId
            && app.openTabIds.includes(app.currentId) && app.sessions.length > 0;
        }"""
    )


def test_new_and_switch_and_close_tabs(page: Page, backend_url, auth_token):
    """Open multiple chat tabs, switch between them, close one — verify the
    bar reflects each operation and no tab is silently lost."""
    _login(page, backend_url, auth_token)
    initial = page.locator(SEL_TAB).count()

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)

    page.locator(SEL_TAB_NEW).click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 2)

    # Switch to the first tab.
    page.locator(f"{SEL_TAB} {SEL_TAB_NAME}").first.click()
    expect(page.locator(SEL_TAB_ACTIVE)).to_have_count(1)
    if page.locator("#jserr").is_visible():
        pytest.fail(page.locator("#jserr").inner_text())

    # Close the active tab via its × button.
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_CLOSE}").click()
    expect(page.locator(SEL_TAB)).to_have_count(initial + 1)


def test_closed_tab_stays_closed_after_stale_prefs_write_and_hard_refresh(
        page: Page, backend_url, auth_token):
    """A legacy/stale prefs writer must not resurrect a closed chat tab."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.locator(SEL_TAB_NEW).click()
    before = page.evaluate(
        "document.querySelector('#app')._x_dataStack[0].openTabIds.slice()")
    closed = before[0]

    page.locator(
        f'{SEL_TAB}[data-tid="{closed}"] {SEL_TAB_CLOSE}').click()
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const store = JSON.parse(localStorage.getItem(
            'muselab_chat_tabs_v1') || '{}');
          return !app.openTabIds.includes(sid)
            && !store.openTabIds?.includes(sid);
        }""",
        arg=[closed],
    )

    # Simulate a page still running the pre-v10 code: it rewrites the shared
    # prefs record with an old tab list while changing an unrelated preference.
    page.evaluate(
        """([staleIds]) => {
          const prefs = JSON.parse(localStorage.getItem('muselab_prefs') || '{}');
          prefs.openTabIds = staleIds;
          prefs.leftOpen = !prefs.leftOpen;
          localStorage.setItem('muselab_prefs', JSON.stringify(prefs));
        }""",
        arg=[before],
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app._sessionsInitialized && app.currentId
            && !app.openTabIds.includes(sid);
        }""",
        arg=[closed],
    )
    restored = page.evaluate(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const store = JSON.parse(localStorage.getItem(
            'muselab_chat_tabs_v1') || '{}');
          return {
            runtimeHasClosed: app.openTabIds.includes(sid),
            storedHasClosed: store.openTabIds.includes(sid),
            prefsStillContainsTabStrip: Array.isArray(JSON.parse(
              localStorage.getItem('muselab_prefs') || '{}').openTabIds),
          };
        }""",
        arg=[closed],
    )
    assert restored == {
        "runtimeHasClosed": False,
        "storedHasClosed": False,
        "prefsStillContainsTabStrip": False,
    }


def test_hidden_runtime_successor_restores_tab_and_draft_after_hard_refresh(
        page: Page, backend_url, auth_token):
    """A missed detached-runtime response must repair the persisted source id
    to its public successor instead of dropping the tab on the next boot."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    target = page.evaluate(
        "document.querySelector('#app')._x_dataStack[0].currentId")
    source = "11111111-2222-4333-8444-555555555555"
    draft = "draft survives runtime redirect"

    def inject_redirect(route):
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        body = response.json()
        body["session_redirects"] = {source: target}
        route.fulfill(response=response, json=body)

    page.route("**/api/chat/sessions?*", inject_redirect)
    page.evaluate(
        """([source, draft]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const prefs = JSON.parse(localStorage.getItem('muselab_prefs') || '{}');
          prefs.currentId = source;
          prefs.workspaceLastSession = {
            ...(prefs.workspaceLastSession || {}),
            [app.currentWorkspacePath()]: source,
          };
          localStorage.setItem('muselab_prefs', JSON.stringify(prefs));
          localStorage.setItem('muselab_chat_tabs_v1', JSON.stringify({
            schema: 1,
            revision: 1000000,
            openTabIds: [source],
          }));
          localStorage.setItem('muselab_chat_drafts_v1', JSON.stringify({
            schema: 1,
            drafts: {
              [source]: { text: draft, pending: '', updatedAt: Date.now() },
            },
          }));
          app._setChatMuxUnsupported();
        }""",
        arg=[source, draft],
    )

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """([source, target, draft]) => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          const tabs = JSON.parse(localStorage.getItem(
            'muselab_chat_tabs_v1') || '{}');
          const drafts = JSON.parse(localStorage.getItem(
            'muselab_chat_drafts_v1') || '{}').drafts || {};
          return app && app._sessionsInitialized
            && app.currentId === target
            && app.openTabIds.includes(target)
            && !app.openTabIds.includes(source)
            && tabs.openTabIds?.includes(target)
            && !tabs.openTabIds?.includes(source)
            && app.tabState[target]?.draft?.input === draft
            && !drafts[source];
        }""",
        arg=[source, target, draft],
    )
    restored = page.evaluate(
        """([source, target]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            currentId: app.currentId,
            tabs: app.openTabIds.slice(),
            sourcePresent: app.sessions.some(row => row.id === source),
            targetPresent: app.sessions.some(row => row.id === target),
          };
        }""",
        arg=[source, target],
    )
    assert restored == {
        "currentId": target,
        "tabs": [target],
        "sourcePresent": False,
        "targetPresent": True,
    }


def test_same_origin_pages_share_strip_without_focus_theft_or_writeback(
        page: Page, backend_url, auth_token):
    """Storage events converge the strip but keep each page's active tab local."""
    _login(page, backend_url, auth_token)
    page.locator(SEL_TAB_NEW).click()
    page.locator(SEL_TAB_NEW).click()
    before = page.evaluate(
        "document.querySelector('#app')._x_dataStack[0].openTabIds.slice()")

    peer = page.context.new_page()
    try:
        _login(peer, backend_url, auth_token)
        peer_current = before[0]
        _activate_chat_tab(peer, peer_current)
        peer.evaluate(
            """() => {
              const app = document.querySelector('#app')._x_dataStack[0];
              window.__chatTabStoreWrites = 0;
              const original = app._writeChatTabStore.bind(app);
              app._writeChatTabStore = (...args) => {
                window.__chatTabStoreWrites += 1;
                return original(...args);
              };
            }"""
        )

        non_current = before[1]
        page.locator(
            f'{SEL_TAB}[data-tid="{non_current}"] {SEL_TAB_CLOSE}').click()
        peer.wait_for_function(
            """([sid, current]) => {
              const app = document.querySelector('#app')._x_dataStack[0];
              return !app.openTabIds.includes(sid) && app.currentId === current;
            }""",
            arg=[non_current, peer_current],
        )
        assert peer.evaluate("window.__chatTabStoreWrites") == 0

        # Closing this page's active tab elsewhere selects a local fallback. The
        # storage-event consumer still must not write the shared strip back.
        page.locator(
            f'{SEL_TAB}[data-tid="{peer_current}"] {SEL_TAB_CLOSE}').click()
        peer.wait_for_function(
            """([closed]) => {
              const app = document.querySelector('#app')._x_dataStack[0];
              return app.currentId !== closed && !app.openTabIds.includes(closed);
            }""",
            arg=[peer_current],
        )
        assert peer.evaluate("window.__chatTabStoreWrites") == 0

        # Even if the peer's in-memory list is stale, an unrelated savePrefs()
        # cannot overwrite the standalone authoritative tab record.
        peer.evaluate(
            """([staleIds]) => {
              const app = document.querySelector('#app')._x_dataStack[0];
              app.openTabIds = staleIds;
              app.leftOpen = !app.leftOpen;
              app.savePrefs();
            }""",
            arg=[before],
        )
        stored = peer.evaluate(
            "JSON.parse(localStorage.getItem('muselab_chat_tabs_v1'))")
        assert non_current not in stored["openTabIds"]
        assert peer_current not in stored["openTabIds"]

        # Two same-origin pages already hold the app's long-lived SSE surfaces.
        # Retire both root mux transports before navigation so Chromium always
        # has a connection available for the reload itself; the fresh document
        # starts its own coordinator after session initialization.
        for browser_page in (page, peer):
            browser_page.evaluate(
                """() => document.querySelector('#app')._x_dataStack[0]
                  ._setChatMuxUnsupported()"""
            )
        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_function(
            """([closed]) => {
              const app = document.querySelector('#app')?._x_dataStack?.[0];
              return app && app._sessionsInitialized
                && closed.every(id => !app.openTabIds.includes(id));
            }""",
            arg=[[non_current, peer_current]],
        )
    finally:
        peer.close()


def test_mobile_typing_cannot_duplicate_chat_tabs(page: Page, backend_url, auth_token):
    """Dirty restored ids, Alpine input ticks and duplicate touch activation must
    still produce one DOM tab per session id."""
    page.set_viewport_size({"width": 390, "height": 844})
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    _login(page, backend_url, auth_token)

    sid = page.evaluate(
        "document.querySelector('#app')._x_dataStack[0].currentId")
    page.evaluate(
        """([sid]) => {
          const prefs = JSON.parse(localStorage.getItem("muselab_prefs") || "{}");
          prefs.schema = 9;
          prefs.currentId = sid;
          prefs.openTabIds = [sid, sid];
          prefs.mobileTab = "chat";
          localStorage.removeItem("muselab_chat_tabs_v1");
          localStorage.setItem("muselab_prefs", JSON.stringify(prefs));
        }""",
        arg=[sid],
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector("#app")?._x_dataStack?.[0];
          return app && app._sessionsInitialized && app.currentId === sid;
        }""",
        arg=[sid],
    )

    composer = page.locator(".chat-input-textarea")
    expect(composer).to_be_visible()
    composer.fill("mobile duplicate-tab probe")
    restored = page.evaluate(
        """([sid]) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return {
            storedCount: app.openTabIds.filter(id => id === sid).length,
            projectedCount: app.workspaceOpenTabIds().filter(id => id === sid).length,
            domCount: document.querySelectorAll(
              `.chat-tab[data-tid="${CSS.escape(sid)}"]`).length,
            activeCount: document.querySelectorAll(".chat-tab.active").length,
          };
        }""",
        arg=[sid],
    )
    assert restored == {
        "storedCount": 1,
        "projectedCount": 1,
        "domCount": 1,
        "activeCount": 1,
    }

    # Runtime defence: even if a stale caller pollutes the array after boot,
    # the render projection must never hand duplicate keys to Alpine.
    page.evaluate(
        """([sid]) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          app.openTabIds = [sid, sid];
        }""",
        arg=[sid],
    )
    composer.fill("mobile duplicate-tab probe 2")
    runtime = page.evaluate(
        """([sid]) => {
          const app = document.querySelector("#app")._x_dataStack[0];
          return {
            projectedCount: app.workspaceOpenTabIds().filter(id => id === sid).length,
            domCount: document.querySelectorAll(
              `.chat-tab[data-tid="${CSS.escape(sid)}"]`).length,
          };
        }""",
        arg=[sid],
    )
    assert runtime == {"projectedCount": 1, "domCount": 1}

    # Two immediate mobile activations are one user intent. The second call
    # returns the same optimistic session rather than opening another blank tab.
    deduped = page.evaluate(
        """() => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const before = app.workspaceOpenTabIds().length;
          const first = app.newSession();
          const second = app.newSession();
          return {
            sameId: first.id === second.id,
            delta: app.workspaceOpenTabIds().length - before,
            currentId: app.currentId,
            createdId: first.id,
          };
        }"""
    )
    assert deduped["sameId"] is True
    assert deduped["delta"] == 1
    assert deduped["currentId"] == deduped["createdId"]

    # A stuck modifier from a mobile IME or remote keyboard must not turn a
    # composer keystroke into another new chat tab.
    composer = page.locator(".chat-input-textarea")
    composer.focus()
    before_shortcut = page.locator(SEL_TAB).count()
    composer.dispatch_event("keydown", {"key": "t", "ctrlKey": True,
                                         "bubbles": True})
    page.wait_for_timeout(50)
    assert page.locator(SEL_TAB).count() == before_shortcut
    assert browser_errors == []


def test_inline_rename_via_dblclick(page: Page, backend_url, auth_token):
    """Double-click a tab title to swap in the rename input; Enter commits.
    Guards the x-if/blur race regression."""
    _login(page, backend_url, auth_token)
    active_name = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}")
    active_name.dblclick()

    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    expect(inp).to_be_visible()
    renamed = "e2e-renamed-after-refresh"
    inp.fill(renamed)
    with page.expect_response(
        lambda response: response.request.method == "PATCH"
        and "/api/chat/sessions/" in response.url,
    ) as response_info:
        inp.press("Enter")
    assert response_info.value.ok
    expect(active_name).to_contain_text(renamed)

    page.evaluate(
        "document.querySelector('#app')._x_dataStack[0]._setChatMuxUnsupported()")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """([name]) => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          const current = app && app.sessions.find(row => row.id === app.currentId);
          return app && app._sessionsInitialized && current?.name === name;
        }""",
        arg=[renamed],
    )
    expect(page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}")).to_contain_text(renamed)


def test_browser_title_reflects_session(page: Page, backend_url, auth_token):
    """document.title should include the active session's name after rename
    — exercises the x-effect on the root element."""
    _login(page, backend_url, auth_token)
    page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_NAME}").dblclick()
    inp = page.locator(f"{SEL_TAB_ACTIVE} {SEL_TAB_RENAME}")
    inp.fill("title-probe")
    inp.press("Enter")
    page.wait_for_function("document.title.includes('title-probe')")
    assert "muselab" in page.title()


def test_keyboard_shortcut_ctrl_t_opens_tab(page: Page, backend_url, auth_token):
    """Ctrl+T opens a new tab and makes it active."""
    _login(page, backend_url, auth_token)
    start = page.locator(SEL_TAB).count()
    # Click into the tab strip first so focus is inside the app — global
    # keydown only fires when nothing else is consuming the event.
    page.locator(SEL_TABS).click()
    page.keyboard.press("Control+t")
    expect(page.locator(SEL_TAB)).to_have_count(start + 1)


def test_composer_drafts_are_isolated_between_tabs(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    textarea = page.locator(".chat-input-textarea")
    sid_a = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    textarea.fill("draft-a")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'draft-a'")

    page.locator(SEL_TAB_NEW).click()
    sid_b = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    assert sid_b != sid_a
    expect(textarea).to_have_value("")
    textarea.fill("draft-b")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'draft-b'")

    _activate_chat_tab(page, sid_a)
    expect(textarea).to_have_value("draft-a")
    _activate_chat_tab(page, sid_b)
    expect(textarea).to_have_value("draft-b")


def test_pending_send_text_survives_hard_refresh(
        page: Page, backend_url, auth_token):
    """The composer is visually cleared before the ticket resolves. A refresh
    in that exact no-response window must recover the submitted text."""
    _login(page, backend_url, auth_token)
    marker = "refresh-safe-pending-send"
    textarea = page.locator(".chat-input-textarea")
    textarea.fill(marker)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._ensureChatMux = async () => true;
          app._confirmSessionBusy = async () => false;
          const originalFetch = window.fetch.bind(window);
          window.fetch = (url, init) => {
            if (String(url).includes('/api/chat/turns/start')) {
              window.__streamStartBlocked = true;
              return new Promise(() => {});
            }
            return originalFetch(url, init);
          };
          void app.send();
        }"""
    )
    page.wait_for_function(
        """([marker]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const store = JSON.parse(localStorage.getItem(
            'muselab_chat_drafts_v1') || '{}');
          return window.__streamStartBlocked === true && app.input === ''
            && store.drafts?.[app.currentId]?.pending === marker;
        }""",
        arg=[marker],
    )
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(SEL_TABS)).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app._sessionsInitialized && app.currentId;
        }"""
    )
    restored = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            input: app.input,
            currentId: app.currentId,
            stateDraft: app.tabState[app.currentId]?.draft?.input,
            store: JSON.parse(localStorage.getItem(
              'muselab_chat_drafts_v1') || '{}'),
          };
        }"""
    )
    assert restored["input"] == marker, restored
    expect(page.locator(".chat-input-textarea")).to_have_value(marker)


@pytest.mark.parametrize("receipt_state", ["failed", "not_found"])
def test_turn_start_failure_restores_draft_and_idle_state(
        page: Page, backend_url, auth_token, receipt_state):
    import json

    attempts = 0

    def reject_turn_start(route) -> None:
        nonlocal attempts
        attempts += 1
        route.fulfill(
            status=422 if receipt_state == "failed" else 503,
            content_type="application/json",
            body='{"detail":"ticket unavailable"}',
        )

    page.route("**/api/chat/turns/start", reject_turn_start)
    # A definite rejection restores the draft; an ambiguous 5xx must not be
    # retried or silently restored as fresh input while it may have executed.
    page.route("**/submissions/**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"state": receipt_state,
                         "result": {"status": 422} if receipt_state == "failed" else {}}),
    ))
    _login(page, backend_url, auth_token)
    marker = "ticket-failure-recovered"
    page.locator(".chat-input-textarea").fill(marker)
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._ensureChatMux = async () => true;
          app.pendingImages.push({
            id: 'recover-image', mime: 'image/png', preview: 'data:image/png;base64,',
            uploading: false, error: false,
          });
          app.pendingDocs.push({
            id: 'recover-doc', name: 'recover.txt', kind: 'text',
            uploading: false, error: false,
          });
        }"""
    )
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const returned = await app.send();
          const record = app._chatDraftRecord(app.currentId);
          return {
            returned,
            input: app.input,
            streaming: app._ensureTabState(app.currentId).streaming,
            pending: record.pending,
            storedText: record.text,
            imageIds: app.pendingImages.map(item => item.id),
            docIds: app.pendingDocs.map(item => item.id),
            bubbleCount: app._ensureTabState(app.currentId).messages.filter(
              m => m.role === 'user' && m.text === 'ticket-failure-recovered'
            ).length,
            claimToken: app.tabState[app.currentId]._composerSubmitToken,
            claimPhase: app.tabState[app.currentId]._composerSubmitPhase,
            uncertain: app.tabState[app.currentId]._uncertainSubmission || null,
            hasToast: app.toasts.some(t => t.msg.includes('发送失败')
              || t.msg.includes('Send failed')),
          };
        }"""
    )
    assert attempts == 1
    if receipt_state == "failed":
        assert result == {
            "returned": False,
            "input": marker,
            "streaming": False,
            "pending": "",
            "storedText": marker,
            "imageIds": ["recover-image"],
            "docIds": ["recover-doc"],
            "bubbleCount": 0,
            "claimToken": None,
            "claimPhase": "",
            "uncertain": None,
            "hasToast": True,
        }
    else:
        assert result["returned"] is False
        assert result["streaming"] is False
        assert result["bubbleCount"] == 0
        assert result["pending"] == marker
        assert result["uncertain"]["input"] == marker
        assert [a["id"] for a in result["uncertain"]["pendingImages"]] == ["recover-image"]
        assert [a["id"] for a in result["uncertain"]["pendingDocs"]] == ["recover-doc"]
        expect(page.locator(".queue-outbox .queued-text")).to_have_text(marker)
        assert page.evaluate("""async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._setChatInput('KEEP NEW DRAFT');
          return await app.send();
        }""") is False
        expect(page.locator(".chat-input-textarea")).to_have_value("KEEP NEW DRAFT")
        assert attempts == 1
    assert not page.locator("#jserr").is_visible()


def test_upload_completion_stays_with_starting_tab(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid_a = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    page.locator(SEL_TAB_NEW).click()
    sid_b = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    _activate_chat_tab(page, sid_a)

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          window.__resolveUpload = null;
          window.__uploadProgress = null;
          app._uploadAttachment = (_fd, options) => {
            window.__uploadProgress = options.onProgress;
            return new Promise(resolve => { window.__resolveUpload = resolve; });
          };
        }""")
    page.locator('input[type="file"][x-ref="attachInput"]').set_input_files({
        "name": "race.txt",
        "mimeType": "text/plain",
        "buffer": b"owner-a",
    })
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[sid]?.draft.pendingDocs.length === 1
            && app.tabState[sid].draft.pendingDocs[0].uploading;
        }""",
        arg=[sid_a],
    )
    page.evaluate("() => window.__uploadProgress(37)")
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const doc = app.tabState[sid]?.draft.pendingDocs[0];
          return doc?.progressKnown === true && doc.progress === 37;
        }""",
        arg=[sid_a],
    )
    assert "37%" in page.locator(".doc-chip-kind").inner_text()

    _activate_chat_tab(page, sid_b)
    page.evaluate(
        """() => window.__resolveUpload(new Response(
          JSON.stringify({id: 'upload-a', kind: 'text'}),
          {status: 200, headers: {'Content-Type': 'application/json'}}))""")
    page.wait_for_function(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const doc = app.tabState[sid]?.draft.pendingDocs[0];
          return doc?.id === 'upload-a' && doc.uploading === false;
        }""",
        arg=[sid_a],
    )
    state = page.evaluate(
        """([a, b]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            current: app.currentId,
            aDocs: app.tabState[a].draft.pendingDocs.length,
            bDocs: app.tabState[b].draft.pendingDocs.length,
            visibleDocs: app.pendingDocs.length,
          };
        }""",
        arg=[sid_a, sid_b],
    )
    assert state == {"current": sid_b, "aDocs": 1, "bDocs": 0, "visibleDocs": 0}


def test_send_upload_wait_is_owned_by_starting_tab(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid_a = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    page.locator(SEL_TAB_NEW).click()
    sid_b = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    _activate_chat_tab(page, sid_a)

    page.evaluate(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          app.input = 'send-a';
          app.pendingDocs.push({
            id: null, name: 'slow.txt', kind: 'text', uploading: true, error: false,
          });
          app._captureComposerState(sid);
          window.__sendPromise = app.send();
        }""",
        arg=[sid_a],
    )
    page.wait_for_function(
        """([sid]) => document.querySelector('#app')._x_dataStack[0]
          .tabState[sid]?.draft._sendWaitingForUpload === true""",
        arg=[sid_a],
    )
    _activate_chat_tab(page, sid_b)
    waiting = page.evaluate(
        """([a, b]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            root: app._sendWaitingForUpload,
            a: app.tabState[a].draft._sendWaitingForUpload,
            b: app.tabState[b].draft._sendWaitingForUpload,
          };
        }""",
        arg=[sid_a, sid_b],
    )
    assert waiting == {"root": False, "a": True, "b": False}

    result = page.evaluate(
        """async ([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const doc = app.tabState[sid].draft.pendingDocs[0];
          doc.error = true;
          doc.uploading = false;
          return await window.__sendPromise;
        }""",
        arg=[sid_a],
    )
    assert result is False
    settled = page.evaluate(
        """([a, b]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return [
            app.tabState[a].draft._sendWaitingForUpload,
            app.tabState[b].draft._sendWaitingForUpload,
            app._sendWaitingForUpload,
          ];
        }""",
        arg=[sid_a, sid_b],
    )
    assert settled == [False, False, False]


def test_composer_internal_phases_stay_hidden_and_busy_states_still_queue(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.locator(".chat-input-textarea").fill("NEXT")
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          app.lang = 'en';
          app._modelsLoaded = true;
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          st.compacting = true;
        }"""
    )
    send = page.locator(".chat-toolbar-queue")
    expect(send).to_be_enabled()
    expect(page.locator("#composer-send-status")).to_have_count(0)
    assert "queue" in (send.get_attribute("title") or "").lower()

    states = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          const reason = phase => {
            st._composerSubmitToken = 'claim-visible';
            st._composerSubmitPhase = phase;
            return app.composerDisabledReason(app.currentId);
          };
          const values = {
            queue: reason('queue'),
            streamStart: reason('stream_start'),
            rollover: reason('rollover'),
          };
          app._releaseComposerClaim('claim-visible');
          st.compacting = false;
          st.draft.pendingDocs = [{id: '', uploading: false, error: true}];
          values.uploadError = app.composerDisabledReason(app.currentId);
          values.uploadErrorStatus = app.composerStatusReason(app.currentId);
          return values;
        }"""
    )
    assert states == {
        "queue": "",
        "streamStart": "",
        "rollover": "",
        "uploadError": "An attachment failed to upload; remove or re-upload it",
        "uploadErrorStatus": "An attachment failed to upload; remove or re-upload it",
    }
    expect(send).to_be_disabled()
    expect(page.locator("#composer-send-status")).to_have_count(0)

    empty = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          st.draft.pendingDocs = [];
          st.draft.input = '';
          app._activateComposerState(app.currentId);
          return {
            disabled: app.composerDisabledReason(app.currentId),
            status: app.composerStatusReason(app.currentId),
          };
        }"""
    )
    assert empty == {"disabled": "Type a message to send", "status": ""}
    expect(send).to_be_disabled()
    expect(send).to_have_attribute("title", "Type a message to send")
    expect(send).to_have_attribute("aria-label", "Type a message to send")
    expect(page.locator("#composer-send-status")).to_have_count(0)


def test_composer_and_send_stay_visible_with_tall_content(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    metrics = page.evaluate(
        """() => {
          const body = document.querySelector('.chat-body');
          const pane = document.querySelector('.pane.chat');
          const composer = document.querySelector('.chat-input');
          const send = document.querySelector('.chat-toolbar-queue');
          const attachments = document.querySelector('.img-attachments');
          const tall = document.createElement('div');
          tall.style.height = '6000px';
          tall.style.flex = '0 0 6000px';
          body.appendChild(tall);
          attachments.style.display = 'flex';
          for (let i = 0; i < 40; i += 1) {
            const chip = document.createElement('div');
            chip.className = 'doc-chip';
            chip.textContent = 'attachment-' + i;
            attachments.appendChild(chip);
          }
          const rect = el => {
            const r = el.getBoundingClientRect();
            return {top: r.top, bottom: r.bottom, height: r.height};
          };
          return {pane: rect(pane), composer: rect(composer), send: rect(send),
                  attachments: rect(attachments)};
        }"""
    )
    assert metrics["composer"]["top"] >= metrics["pane"]["top"]
    assert metrics["composer"]["bottom"] <= metrics["pane"]["bottom"] + 1
    assert metrics["send"]["top"] >= metrics["composer"]["top"]
    assert metrics["send"]["bottom"] <= metrics["composer"]["bottom"]
    assert metrics["attachments"]["height"] <= 132


def test_repeated_enter_while_background_busy_submits_one_draft(
        page: Page, backend_url, auth_token):
    """Rapid and repeated Enter events share one composer submission claim."""
    _login(page, backend_url, auth_token)
    textarea = page.locator(".chat-input-textarea")
    textarea.fill("ONLY_ONCE")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'ONLY_ONCE'")

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          st.backgroundActive = true;
          st.streaming = false;
          st.compacting = false;
          app._awaitRuntimeSettingPatches = async () => true;
          window.__busyProbeCalls = 0;
          window.__busyGate = new Promise(resolve => { window.__releaseBusy = resolve; });
          app._confirmSessionBusy = async () => {
            window.__busyProbeCalls += 1;
            await window.__busyGate;
            return true;
          };
          window.__queuedDrafts = [];
          app._enqueueMessage = async (_sid, item) => {
            window.__queuedDrafts.push({text: item.text, displayText: item.displayText});
            return true;
          };
          app._handoffBackgroundSession = () => new Promise(
            resolve => { window.__releaseHandoff = resolve; });
          app._attachToServerTurn = () => {};

          const target = document.querySelector('.chat-input-textarea');
          for (let i = 0; i < 5; i += 1) {
            target.dispatchEvent(new KeyboardEvent('keydown', {
              key: 'Enter', code: 'Enter', bubbles: true, cancelable: true,
              repeat: i >= 2,
            }));
          }
        }"""
    )
    page.wait_for_function("() => window.__busyProbeCalls === 1")
    assert page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0]"
        ".tabState[document.querySelector('#app')._x_dataStack[0].currentId]"
        "._composerSubmitToken") is not None
    expect(page.locator(".chat-toolbar-queue")).to_be_disabled()

    page.evaluate("() => window.__releaseBusy()")
    page.wait_for_function("() => window.__queuedDrafts.length === 1")
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[app.currentId]._composerSubmitToken === null;
        }"""
    )
    # The handoff is intentionally fire-and-forget after queue commit. Join
    # its observable start before releasing the test-owned promise.
    page.wait_for_function(
        "() => typeof window.__releaseHandoff === 'function'")
    result = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          window.__releaseHandoff(null);
          return {
            queued: window.__queuedDrafts,
            input: app.input,
            draft: st.draft.input,
          };
        }"""
    )
    assert result == {
        "queued": [{"text": "ONLY_ONCE", "displayText": "ONLY_ONCE"}],
        "input": "",
        "draft": "",
    }


def test_server_busy_admission_never_borrows_running_footer(
        page: Page, backend_url, auth_token):
    """A 409 admission race shows Queueing without a one-frame Running lie."""
    _login(page, backend_url, auth_token)
    page.locator(".chat-input-textarea").fill("QUEUE_ON_409")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'QUEUE_ON_409'")
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          const st = app._ensureTabState(sid);
          app.lang = 'zh';
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          app._modelsLoaded = true;
          st.streaming = false;
          st.backgroundActive = false;
          st.compacting = false;
          app._awaitRuntimeSettingPatches = async () => true;
          app._confirmSessionBusy = async () => false;
          app._ensureChatMux = async () => true;
          window.__turnStartCalls = 0;
          window.__queueCalls = 0;
          window.__queueGate = new Promise(resolve => {
            window.__releaseQueuePost = resolve;
          });
          window.__acceptedQueueItem = {
            id: 'q-admission-race',
            text: 'QUEUE_ON_409',
            display_text: 'QUEUE_ON_409',
            image_ids: '',
            selection_quotes: [],
            enqueued_at: Date.now(),
          };
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (url, init = {}) => {
            const value = String(url);
            const method = (init.method || 'GET').toUpperCase();
            if (value === '/api/chat/turns/start') {
              window.__turnStartCalls += 1;
              return new Response('{}', {
                status: 409,
                headers: {'Content-Type': 'application/json'},
              });
            }
            if (value.includes('/api/chat/sessions/') && value.endsWith('/queue')) {
              if (method === 'POST') {
                window.__queueCalls += 1;
                await window.__queueGate;
                return new Response(JSON.stringify({
                  item: window.__acceptedQueueItem,
                  queue: {revision: 11},
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
              }
              return new Response(JSON.stringify({
                revision: 11,
                paused: false,
                items: [window.__acceptedQueueItem],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return originalFetch(url, init);
          };
          window.__admissionRaceSend = app.send();
        }"""
    )
    page.wait_for_function("() => window.__queueCalls === 1")
    # Admission now occupies the same queue-tail slot as the durable row so
    # accepting the POST cannot move or duplicate the bubble.  Assert the
    # visible contract and the temporary ownership state independently.
    expect(page.locator(".msg.user.queued")).to_be_visible()
    expect(page.locator(".msg.user.queued .queued-label")).to_contain_text("正在提交")
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          return st._queueAdmission?.displayText === 'QUEUE_ON_409'
            && st.pendingQueue.length === 0;
        }"""
    )
    expect(page.locator(".turn-pending-footer")).to_be_hidden()
    assert page.evaluate("() => window.__turnStartCalls") == 1

    page.evaluate("() => window.__releaseQueuePost()")
    assert page.evaluate("() => window.__admissionRaceSend") is True
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[app.currentId].pendingQueue.length === 1;
        }"""
    )
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.tabState[app.currentId]._queueAdmission === null;
        }"""
    )
    expect(page.locator(".msg.user.queued")).to_have_count(1)
    expect(page.locator(".msg.user.queued .queued-label")).to_contain_text("排队中")
    assert page.evaluate("() => window.__queueCalls") == 1


def test_image_generation_submit_timeout_and_close_cancel_are_local(
        page: Page, backend_url, auth_token):
    """A wedged image submit times out; closing cancels without stale errors."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.lang = 'zh';
          app.imageGen.prompt = 'timeout probe';
          app.IMAGE_GEN_SUBMIT_DEADLINE_MS = 25;
          const toasts = [];
          app.toast = (...args) => { toasts.push(args); };
          app.api = async (_path, opts = {}) => await new Promise(resolve => {
            opts.signal.addEventListener('abort', () => resolve({
              ok: false, status: 0, error: 'aborted',
            }), {once: true});
          });
          await app.runImageGen();
          const timeout = {
            loading: app.imageGen.loading,
            controller: app.imageGen.submitController,
            error: app.imageGen.error,
            errorToasts: toasts.filter(row => row[1] === 'error').length,
          };

          let closeAborted = false;
          app.imageGen.show = true;
          app.imageGen.prompt = 'cancel probe';
          app.IMAGE_GEN_SUBMIT_DEADLINE_MS = 1000;
          app.api = async (_path, opts = {}) => await new Promise(resolve => {
            opts.signal.addEventListener('abort', () => {
              closeAborted = true;
              resolve({ok: false, status: 0, error: 'aborted'});
            }, {once: true});
          });
          const beforeCloseToastCount = toasts.length;
          const pending = app.runImageGen();
          app.closeImageGen();
          await pending;
          return {
            timeout,
            close: {
              aborted: closeAborted,
              show: app.imageGen.show,
              loading: app.imageGen.loading,
              controller: app.imageGen.submitController,
              error: app.imageGen.error,
              newToasts: toasts.length - beforeCloseToastCount,
            },
          };
        }"""
    )
    assert result == {
        "timeout": {
            "loading": False,
            "controller": None,
            "error": "提交确认超时，请先刷新历史记录，确认后再重试",
            "errorToasts": 1,
        },
        "close": {
            "aborted": True,
            "show": False,
            "loading": False,
            "controller": None,
            "error": "",
            "newToasts": 0,
        },
    }


def test_background_send_resolves_before_runtime_handoff(
        page: Page, backend_url, auth_token):
    """Queue commit is synchronous; the expensive runtime fork is not."""
    _login(page, backend_url, auth_token)
    page.locator(".chat-input-textarea").fill("QUEUE_FIRST")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'QUEUE_FIRST'")
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app._ensureTabState(app.currentId);
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          st.backgroundActive = true;
          st.streaming = false;
          st.compacting = false;
          app._awaitRuntimeSettingPatches = async () => true;
          app._confirmSessionBusy = async () => true;
          window.__sendOrder = [];
          window.__queueGate = new Promise(resolve => { window.__releaseQueue = resolve; });
          window.__handoffGate = new Promise(resolve => { window.__releaseHandoff = resolve; });
          app._enqueueMessage = async () => {
            window.__sendOrder.push('enqueue:start');
            await window.__queueGate;
            window.__sendOrder.push('enqueue:committed');
            return true;
          };
          app._handoffBackgroundSession = async () => {
            window.__sendOrder.push('handoff:start');
            await window.__handoffGate;
            return null;
          };
          app._attachToServerTurn = () => {};
          window.__backgroundSend = app.send();
        }"""
    )
    page.wait_for_function("() => window.__sendOrder[0] === 'enqueue:start'")
    assert page.evaluate("() => window.__sendOrder") == ["enqueue:start"]

    resolved = page.evaluate(
        """async () => {
          window.__releaseQueue();
          return await Promise.race([
            window.__backgroundSend,
            new Promise(resolve => setTimeout(() => resolve('timed-out'), 300)),
          ]);
        }"""
    )
    assert resolved is True
    page.wait_for_function(
        "() => window.__sendOrder.includes('handoff:start')")
    state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            order: window.__sendOrder,
            input: app.input,
            submitting: !!app.tabState[app.currentId]._composerSubmitToken,
          };
        }"""
    )
    assert state == {
        "order": ["enqueue:start", "enqueue:committed", "handoff:start"],
        "input": "",
        "submitting": False,
    }
    page.evaluate("() => window.__releaseHandoff()")


def test_background_handoff_during_queue_post_settles_successor_composer(
        page: Page, backend_url, auth_token):
    """A proactive rollover cannot leave a cloned draft locked or resendable."""
    _login(page, backend_url, auth_token)
    page.locator(".chat-input-textarea").fill("RACE_ONCE")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'RACE_ONCE'")

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sourceSid = app.currentId;
          const source = app._ensureTabState(sourceSid);
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          source.backgroundActive = true;
          source.streaming = false;
          source.compacting = false;
          app._awaitRuntimeSettingPatches = async () => true;
          window.__busyProbeCalls = 0;
          app._confirmSessionBusy = async () => {
            window.__busyProbeCalls += 1;
            return true;
          };
          window.__queueCalls = 0;
          window.__queueGate = new Promise(resolve => { window.__releaseQueue = resolve; });
          window.__acceptedQueueItem = {
            id: 'q-race', text: 'RACE_ONCE', display_text: 'RACE_ONCE',
            image_ids: '', selection_quotes: [], enqueued_at: Date.now(),
          };
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (url, init = {}) => {
            const value = String(url);
            if (value.includes('/api/chat/sessions/') && value.endsWith('/queue')) {
              if ((init.method || 'GET').toUpperCase() === 'POST') {
                window.__queueCalls += 1;
                await window.__queueGate;
                return new Response(JSON.stringify({
                  item: window.__acceptedQueueItem,
                  queue: {revision: 7},
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
              }
              return new Response(JSON.stringify({
                revision: 7, paused: false,
                items: [window.__acceptedQueueItem],
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return originalFetch(url, init);
          };
          app._handoffBackgroundSession = async (sid, st) => {
            const childSid = 'successor-race';
            const child = app._stateForDetachedSuccessor(sid, childSid, st, 1);
            st._backgroundSuccessorSid = childSid;
            app.tabState[childSid] = child;
            app.sessions = app.sessions
              .filter(row => row.id !== sid && row.id !== childSid)
              .concat([{id: childSid, name: 'successor', model: 'fake-model'}]);
            app.openTabIds = app.openTabIds.map(id => id === sid ? childSid : id);
            app.currentId = childSid;
            app._activateTabState(childSid);
            delete app.tabState[sid];
            return {sessionId: childSid, queuePending: true, rolledOver: true};
          };
          window.__attachSids = [];
          app._attachToServerTurn = sid => { window.__attachSids.push(sid); };
          window.__raceSend = app.send();
          window.__sourceSid = sourceSid;
        }"""
    )
    page.wait_for_function("() => window.__queueCalls === 1")
    page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app._handoffBackgroundSession(window.__sourceSid,
            app.tabState[window.__sourceSid]);
        }"""
    )
    before = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          return {input: app.input, draft: st.draft.input,
                  submitting: !!st._composerSubmitToken};
        }"""
    )
    assert before == {"input": "", "draft": "", "submitting": True}

    result = page.evaluate(
        """async () => {
          window.__releaseQueue();
          await window.__raceSend;
          await new Promise(resolve => setTimeout(resolve, 0));
          const app = document.querySelector('#app')._x_dataStack[0];
          const st = app.tabState[app.currentId];
          return {input: app.input, draft: st.draft.input,
                  submitting: !!st._composerSubmitToken,
                  token: st._composerSubmitToken, queueCalls: window.__queueCalls,
                  queueIds: st.pendingQueue.map(item => item.id),
                  attachSids: window.__attachSids};
        }"""
    )
    assert result == {
        "input": "", "draft": "", "submitting": False,
        "token": None, "queueCalls": 1, "queueIds": ["q-race"],
        "attachSids": ["successor-race"],
    }


def test_queue_edit_restores_original_tab_during_switch(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid_a = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    page.locator(SEL_TAB_NEW).click()
    sid_b = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    page.locator(".chat-input-textarea").fill("draft-b")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'draft-b'")
    _activate_chat_tab(page, sid_a)

    page.evaluate(
        """([sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.tabState[sid].pendingQueue = [{
            id: 'q1', text: 'queued-a',
            images: [{id: 'img-a', mime: 'image/png', src: 'data:image/png;base64,AA=='}],
            docs: [{id: 'doc-a', name: 'a.txt', kind: 'text'}],
          }];
          const originalFetch = window.fetch.bind(window);
          window.fetch = (url, init) => String(url).includes('/queue/q1')
            ? Promise.resolve(new Response('{}', {status: 200}))
            : originalFetch(url, init);
          window.__queueResolvers = [];
          app._syncQueueFromServer = () => new Promise(
            resolve => { window.__queueResolvers.push(resolve); });
          app.editPendingQueueItem(sid, 0);
        }""",
        arg=[sid_a],
    )
    page.wait_for_function("() => window.__queueResolvers?.length >= 1")
    _activate_chat_tab(page, sid_b)
    page.evaluate("() => window.__queueResolvers[0]()")
    page.wait_for_function(
        """([sid]) => document.querySelector('#app')._x_dataStack[0]
          .tabState[sid]?.draft.input === 'queued-a'""",
        arg=[sid_a],
    )
    state = page.evaluate(
        """([a, b]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            current: app.currentId,
            visibleInput: app.input,
            aInput: app.tabState[a].draft.input,
            aImages: app.tabState[a].draft.pendingImages.map(x => x.id),
            aDocs: app.tabState[a].draft.pendingDocs.map(x => x.id),
            bInput: app.tabState[b].draft.input,
          };
        }""",
        arg=[sid_a, sid_b],
    )
    assert state == {
        "current": sid_b,
        "visibleInput": "draft-b",
        "aInput": "queued-a",
        "aImages": ["img-a"],
        "aDocs": ["doc-a"],
        "bInput": "draft-b",
    }


def test_workspace_picker_switches_files_preview_and_conversation_together(
        page: Page, backend_url, auth_token, tmp_path):
    """A workspace switch moves chat, file tree, and preview as one surface."""
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    primary_id = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentId")
    other = Path(primary) / ("workspace-two-" + tmp_path.name)
    other.mkdir()
    (other / "WORKSPACE_ONLY.md").write_text(
        "# second workspace\n\nworkspace-isolated-preview\n", encoding="utf-8")

    page.locator('.filelist li[data-path="README.md"]').click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'README.md' && app.rawText.includes('muselab e2e');
        }""")

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-add").click()
    modal = page.locator(".workspace-browser-modal")
    expect(modal).to_be_visible()
    row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{other}"]')
    expect(row).to_be_visible(timeout=5000)
    row.locator(".workspace-browser-entry").click()
    page.locator(".workspace-browser-confirm").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const session = app.sessions.find(item => item.id === app.currentId);
          return app.currentWorkspacePath() === path && !app.workspaceSwitching
            && session?.cwd === path;
        }""",
        arg=[str(other)],
        timeout=15000,
    )

    state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            visible: app.visible.map(item => item.path),
            selected: app.selected,
            currentId: app.currentId,
            tabCwds: app.workspaceOpenTabIds().map(id =>
              app.sessions.find(item => item.id === id)?.cwd),
          };
        }""")
    assert "WORKSPACE_ONLY.md" in state["visible"]
    assert "README.md" not in state["visible"]
    assert state["selected"] == ""
    assert state["tabCwds"] and set(state["tabCwds"]) == {str(other)}
    secondary_id = state["currentId"]

    workspace_file = page.locator('.filelist li[data-path="WORKSPACE_ONLY.md"]')
    if not workspace_file.is_visible():
        page.locator(".mobile-tab-bar button").first.click()
        expect(workspace_file).to_be_visible()
    workspace_file.click()
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview');
        }""")

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-row").filter(
        has=page.get_by_text(primary, exact=True)).locator(
        ".workspace-picker-select").click()
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'README.md' && app.rawText.includes('muselab e2e')
            && !app.workspaceSwitching;
        }""",
        arg=[primary, primary_id],
        timeout=15000,
    )

    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-row").filter(
        has=page.get_by_text(str(other), exact=True)).locator(
        ".workspace-picker-select").click()
    page.wait_for_function(
        """([path, sid]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return app.currentWorkspacePath() === path && app.currentId === sid
            && app.selected === 'WORKSPACE_ONLY.md'
            && app.rawText.includes('workspace-isolated-preview')
            && !app.workspaceSwitching;
        }""",
        arg=[str(other), secondary_id],
        timeout=15000,
    )

    # Remove the registry entry through the UI; project files remain untouched.
    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-row").filter(
        has=page.get_by_text(primary, exact=True)).locator(
        ".workspace-picker-select").click()
    page.wait_for_function(
        "([path]) => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath() === path",
        arg=[primary],
    )
    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-row").filter(
        has=page.get_by_text(str(other), exact=True)).locator(
        ".workspace-picker-remove").click()
    expect(page.locator(".confirm-modal")).to_be_visible()
    page.locator(".confirm-modal .btn-danger").click()
    page.wait_for_function(
        """([path]) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return !app.workspaceSwitching
            && !app.sessionWorkspaces.some(item => item.path === path);
        }""",
        arg=[str(other)],
    )


def test_workspace_registry_and_order_requests_do_not_disable_chat(
        page: Page, backend_url, auth_token):
    """Registry/order persistence owns its controls, never the composer."""
    _login(page, backend_url, auth_token)
    page.locator(".chat-input-textarea").fill("CHAT_STAYS_READY")
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].input === 'CHAT_STAYS_READY'")
    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.availableModels = [{model: 'fake-model', group: 'test'}];
          app._modelsLoaded = true;
          app.fetchSessionWorkspaces = async () => app.sessionWorkspaces;
          app._refreshSessionsAfterWorkspaceRegistryChange = async () => true;
          window.__registryGate = new Promise(resolve => {
            window.__releaseRegistry = resolve;
          });
          window.__orderGate = new Promise(resolve => {
            window.__releaseOrder = resolve;
          });
          window.__registryCalls = 0;
          window.__orderCalls = 0;
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (url, init = {}) => {
            const value = String(url);
            const method = (init.method || 'GET').toUpperCase();
            if (value === '/api/chat/workspaces' && method === 'POST') {
              window.__registryCalls += 1;
              await window.__registryGate;
              return new Response(JSON.stringify({
                path: '/registry-only', name: 'registry-only', primary: false,
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            if (value === '/api/chat/workspaces/order' && method === 'PUT') {
              window.__orderCalls += 1;
              await window.__orderGate;
              return new Response(JSON.stringify({
                workspaces: app.sessionWorkspaces,
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return originalFetch(url, init);
          };
          window.__registryPromise = app._registerWorkspacePath('/registry-only');
        }"""
    )
    page.wait_for_function("() => window.__registryCalls === 1")
    registry_state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            registryBusy: app.workspaceRegistryBusy,
            switching: app.workspaceSwitching,
            reason: app.composerStatusReason(app.currentId),
            sendDisabled: document.querySelector('.chat-toolbar-send').disabled,
          };
        }"""
    )
    assert registry_state == {
        "registryBusy": True,
        "switching": False,
        "reason": "",
        "sendDisabled": False,
    }
    page.evaluate("() => window.__releaseRegistry()")
    page.evaluate("() => window.__registryPromise")

    page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const fake = {path: '/order-only', name: 'order-only', primary: false};
          app.sessionWorkspaces = [...app.sessionWorkspaces, fake];
          const current = app.sessionWorkspaces.map(row => row.path);
          app.workspaceDrag = {
            path: current[0],
            overPath: current[0],
            pointerId: null,
            originalPaths: [...current].reverse(),
          };
          window.__orderPromise = app.finishWorkspaceDrag();
        }"""
    )
    page.wait_for_function("() => window.__orderCalls === 1")
    order_state = page.evaluate(
        """() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {
            orderSaving: app.workspaceOrderSaving,
            switching: app.workspaceSwitching,
            reason: app.composerStatusReason(app.currentId),
            sendDisabled: document.querySelector('.chat-toolbar-send').disabled,
          };
        }"""
    )
    assert order_state == {
        "orderSaving": True,
        "switching": False,
        "reason": "",
        "sendDisabled": False,
    }
    page.evaluate("() => window.__releaseOrder()")
    page.evaluate("() => window.__orderPromise")


def test_workspace_switch_overlaps_tree_sessions_and_transcript_without_early_activation(
        page: Page, backend_url, auth_token):
    """Cold switch latency is max(tree, sessions+transcript), not their sum."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const originalCurrent = app.currentId;
          const targetPath = `/parallel-switch-${Date.now()}-${Math.random()}`;
          const targetId = `parallel-session-${Date.now()}`;
          const staleId = `deleted-session-${Date.now()}`;
          const targetMeta = {
            id: targetId, name: 'parallel target', cwd: targetPath,
            created_at: Date.now() / 1000, updated_at: Date.now() / 1000,
            model: app.model, active: false, message_count: 1,
          };
          const staleMeta = {
            ...targetMeta, id: staleId, name: 'deleted target',
            updated_at: targetMeta.updated_at - 10,
          };
          const originals = {
            loadRoot: app.loadRoot,
            loadTrash: app.loadTrash,
            fetchContextInfo: app.fetchContextInfo,
            fetchTerminals: app.fetchTerminals,
            pullWorkspaceSessions: app._pullWorkspaceSessions,
            ensureSessionLoaded: app._ensureSessionLoaded,
            openTab: app.openTab,
            newSession: app.newSession,
            startFileEvents: app._startFileEvents,
            stopFileEvents: app._stopFileEvents,
            savePrefs: app.savePrefs,
            persist: app._scheduleWorkspaceTreePersist,
            toast: app.toast,
            openActivityCenter: app.openActivityCenter,
          };
          const events = {};
          const opened = [];
          let newCount = 0;
          app.sessionWorkspaces = [
            ...app.sessionWorkspaces,
            {path: targetPath, name: 'parallel', primary: false},
          ];
          app.sessions = [staleMeta, ...app.sessions];
          app.workspaceLastSession = {
            ...app.workspaceLastSession, [targetPath]: staleId,
          };
          app.workspaceSurfaces[targetPath] = {previewSurface: 'file'};
          app.loadRoot = async () => {
            events.treeStart = performance.now();
            await new Promise(resolve => setTimeout(resolve, 240));
            events.treeEnd = performance.now();
            return true;
          };
          app.loadTrash = async () => true;
          app.fetchContextInfo = async () => true;
          app.fetchTerminals = async () => true;
          app._pullWorkspaceSessions = async path => {
            events.sessionsStart = performance.now();
            await new Promise(resolve => setTimeout(resolve, 70));
            const sessions = app._mergeWorkspaceSessionList([targetMeta], path);
            events.sessionsEnd = performance.now();
            return {ok: true, sessions};
          };
          app._ensureSessionLoaded = async sid => {
            events.preloadStart = performance.now();
            events.preloadedSid = sid;
            events.currentAtPreloadStart = app.currentId;
            events.shieldDuringPreload = app.workspaceSurfaceTransition
              && getComputedStyle(document.querySelector('.workspace-switch-shield')).display !== 'none';
            await new Promise(resolve => setTimeout(resolve, 150));
            events.preloadEnd = performance.now();
            events.currentAtPreloadEnd = app.currentId;
            return sid === targetId;
          };
          app.openTab = async (sid, makeCurrent = true, options = {}) => {
            events.currentBeforeOpen = app.currentId;
            events.openOptions = {...options};
            opened.push(sid);
            if (makeCurrent) app.currentId = sid;
            const loading = app._ensureSessionLoaded(sid);
            if (options.deferLoad) void loading;
            else await loading;
          };
          app.newSession = () => { newCount += 1; };
          app._startFileEvents = () => {};
          app._stopFileEvents = () => {};
          app.savePrefs = () => {};
          app._scheduleWorkspaceTreePersist = () => {};
          app.toast = () => {};
          try {
            const started = performance.now();
            await app.switchWorkspace(targetPath);
            const elapsed = performance.now() - started;
            events.switchEnd = performance.now();
            events.activityClicks = 0;
            app.openActivityCenter = () => { events.activityClicks += 1; };
            document.querySelector('.activity-center-btn').click();
            await Promise.resolve();
            return {
              elapsed, events, originalCurrent, targetId,
              current: app.currentId, opened, newCount,
              stalePresent: app.sessions.some(row => row.id === staleId),
              switching: app.workspaceSwitching,
              surfaceTransition: app.workspaceSurfaceTransition,
            };
          } finally {
            app.loadRoot = originals.loadRoot;
            app.loadTrash = originals.loadTrash;
            app.fetchContextInfo = originals.fetchContextInfo;
            app.fetchTerminals = originals.fetchTerminals;
            app._pullWorkspaceSessions = originals.pullWorkspaceSessions;
            app._ensureSessionLoaded = originals.ensureSessionLoaded;
            app.openTab = originals.openTab;
            app.newSession = originals.newSession;
            app._startFileEvents = originals.startFileEvents;
            app._stopFileEvents = originals.stopFileEvents;
            app.savePrefs = originals.savePrefs;
            app._scheduleWorkspaceTreePersist = originals.persist;
            app.toast = originals.toast;
            app.openActivityCenter = originals.openActivityCenter;
          }
        }"""
    )
    # Alpine schedules the declared 120 ms leave transition across animation
    # frames. Wait for its observable DOM end state instead of assuming a
    # fixed wall-clock delay is enough on a loaded headless CI runner.
    page.wait_for_function(
        """() => getComputedStyle(document.querySelector(
          '.workspace-switch-shield'
        )).display === 'none'""",
        timeout=2000,
    )
    events = result["events"]
    assert abs(events["treeStart"] - events["sessionsStart"]) < 75
    # Tree bootstrap and transcript loading continue behind pane-local state.
    # Neither may extend the global shield/composer-disabled transition.
    if "treeEnd" in events:
        assert events["switchEnd"] < events["treeEnd"]
    if "preloadEnd" in events:
        assert events["switchEnd"] < events["preloadEnd"]
    assert events["currentAtPreloadStart"] == result["targetId"]
    assert events["shieldDuringPreload"] is True
    assert events["activityClicks"] == 1
    assert events["openOptions"]["deferLoad"] is True
    assert events["currentBeforeOpen"] == result["originalCurrent"]
    assert events["preloadedSid"] == result["targetId"]
    assert result["stalePresent"] is False
    assert result["opened"] == [result["targetId"]]
    assert result["current"] == result["targetId"]
    assert result["newCount"] == 0
    assert result["switching"] is False
    assert result["surfaceTransition"] is False


def test_concurrent_session_list_does_not_advance_an_older_transcript_revision(
        page: Page, backend_url, auth_token):
    """A U2 list queued behind a U1 transcript must run a canonical retry."""
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          const sid = app.currentId;
          if (app._sessionsSyncTimer) clearInterval(app._sessionsSyncTimer);
          app._sessionsSyncTimer = null;
          // Let the login-time list owner settle before installing synthetic
          // revisions; otherwise its late response can overwrite this fixture.
          if (app._sessionListPullPromise) {
            try { await app._sessionListPullPromise; } catch (_) {}
          }
          const meta = app.sessions.find(row => row.id === sid);
          const st = app._ensureTabState(sid);
          const settleDeadline = performance.now() + 3000;
          while ((st.messagesLoading || st.sessionSync.inFlight
                  || Object.keys(st.sessionSync.pending || {}).length)
                 && performance.now() < settleDeadline) {
            await new Promise(resolve => setTimeout(resolve, 20));
          }
          if (st.messagesLoading || st.sessionSync.inFlight
              || Object.keys(st.sessionSync.pending || {}).length) {
            throw new Error(
              "initial session synchronization did not settle");
          }
          app._disposeSessionSync(st);
          const originals = {
            loadSession: app.loadSession,
            updatedAt: meta.updated_at,
            active: meta.active,
            turnActive: meta.turn_active,
            backgroundActive: meta.background_active,
            seen: st._seenUpdated,
            target: st._reconcileTargetUpdated,
            pending: st._pendingExternalUpdate,
            loaded: st._loaded,
          };
          let loadCalls = 0;
          let releaseOlder;
          let markOlderStarted;
          const olderGate = new Promise(resolve => { releaseOlder = resolve; });
          const olderStarted = new Promise(resolve => { markOlderStarted = resolve; });
          meta.updated_at = 20;
          meta.active = false;
          meta.turn_active = false;
          meta.background_active = false;
          st._seenUpdated = 10;
          st._reconcileTargetUpdated = 10;
          st._pendingExternalUpdate = false;
          st._loaded = true;
          st.streaming = false;
          st.es = null;
          app.loadSession = async requested => {
            loadCalls += 1;
            if (loadCalls === 1) {
              markOlderStarted();
              await olderGate;
              st._seenUpdated = 11;
              return requested === sid;
            }
            if (requested === sid) st._seenUpdated = 20;
            return requested === sid;
          };
          try {
            const olderTranscript = app._requestSessionSync(sid, 'history_load');
            await olderStarted;
            app._reconcileOpenSession([meta]);
            const queuedBeforeRelease =
              !!st.sessionSync.pending.history_revision;
            releaseOlder();
            await olderTranscript;
            const afterFirst = {
              seen: st._seenUpdated,
              pending: st._pendingExternalUpdate,
              retryQueued: !!st.sessionSync.pending.history_revision,
            };
            const deadline = performance.now() + 1500;
            while ((loadCalls < 2 || st.sessionSync.inFlight
                    || st.sessionSync.pending.history_revision)
                   && performance.now() < deadline) {
              await new Promise(resolve => setTimeout(resolve, 20));
            }
            return {
              queuedBeforeRelease,
              afterFirst,
              finalSeen: st._seenUpdated,
              finalPending: st._pendingExternalUpdate,
              loadCalls,
            };
          } finally {
            releaseOlder();
            app._disposeSessionSync(st);
            app.loadSession = originals.loadSession;
            meta.updated_at = originals.updatedAt;
            meta.active = originals.active;
            meta.turn_active = originals.turnActive;
            meta.background_active = originals.backgroundActive;
            st._seenUpdated = originals.seen;
            st._reconcileTargetUpdated = originals.target;
            st._pendingExternalUpdate = originals.pending;
            st._loaded = originals.loaded;
          }
        }"""
    )
    assert result["queuedBeforeRelease"] is True
    assert result["afterFirst"] == {
        "seen": 11,
        "pending": False,
        "retryQueued": True,
    }
    assert result["loadCalls"] == 2
    assert result["finalSeen"] == 20
    assert result["finalPending"] is False

def test_workspace_folder_browser_is_fullscreen_and_navigable_on_mobile(
        page: Page, backend_url, auth_token, tmp_path):
    page.set_viewport_size({"width": 390, "height": 844})
    _login(page, backend_url, auth_token)
    primary = page.evaluate(
        "() => document.querySelector('#app')._x_dataStack[0].currentWorkspacePath()")
    parent = Path(primary) / ("mobile-picker-" + tmp_path.name)
    child = parent / "nested-project"
    child.mkdir(parents=True)
    (child / "package.json").write_text('{"name":"nested"}\n', encoding="utf-8")

    page.locator(".mobile-tab-bar button").first.click()
    expect(page.locator(".workspace-picker-btn")).to_be_visible()
    page.locator(".workspace-picker-btn").click()
    page.locator(".workspace-picker-add").click()
    modal = page.locator(".workspace-browser-modal")
    expect(modal).to_be_visible()
    page.wait_for_timeout(250)
    box = modal.bounding_box()
    assert box is not None
    assert box["x"] == 0
    assert box["y"] == 0
    assert box["width"] >= 389
    assert box["height"] >= 843

    parent_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{parent}"]')
    expect(parent_row).to_be_visible(timeout=5000)
    parent_row.locator(".workspace-browser-open").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[str(parent)],
    )
    child_row = page.locator(
        f'.workspace-browser-row[data-workspace-path="{child}"]')
    expect(child_row).to_be_visible()
    expect(child_row).to_contain_text("Node.js")

    page.locator(".workspace-browser-up").click()
    page.wait_for_function(
        """([path]) => document.querySelector('#app')._x_dataStack[0]
          .workspaceBrowser.path === path""",
        arg=[primary],
    )
    page.locator(".workspace-browser-modal .modal-close").click()
    expect(modal).to_be_hidden()


# Note: drag-and-drop tab reorder and right-click context menu are harder
# to drive reliably with Playwright's HTML5 drag emulation across browsers.
# Left as manual smoke for now.
