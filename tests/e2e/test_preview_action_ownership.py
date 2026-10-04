"""File actions keep their editor and workspace owner during navigation."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .test_files_preview import _login

APP = "document.querySelector('#app')._x_dataStack[0]"


def _dirty_editor(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      await app.openFile({path:'README.md', name:'README.md'});
      await app.openFile({path:'notes.md', name:'notes.md'});
      await app.toggleEdit();
    }""")
    page.wait_for_function("window.__muselab_cm && !" + APP + ".editorLoading")
    page.evaluate("window.__muselab_cm.setValue('unsaved editor fixture')")
    assert page.evaluate(APP + "._editorDirty()")


@pytest.mark.parametrize("discard", [False, True])
def test_close_dirty_file_confirms_once_and_preserves_adjacent_preview(
        page, backend_url, auth_token, discard):
    _dirty_editor(page, backend_url, auth_token)
    # Make the adjacent tab ephemeral. Closing another tab is not a pin gesture.
    page.evaluate(APP + ".tabs = " + APP + ".tabs.map(t => t.path === 'README.md' ? {...t, preview:true} : t)")
    dialogs = []

    def answer(dialog):
        dialogs.append(dialog.message)
        if discard and len(dialogs) == 1:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", answer)
    page.evaluate(APP + ".closeTab('notes.md')")
    assert len(dialogs) == 1
    if discard:
        page.wait_for_function(APP + ".selected === 'README.md' && !" + APP + ".editing")
        expect(page.locator('[x-ref=markdownPreview]')).to_contain_text('muselab e2e fixture')
        tabs = page.evaluate(APP + ".tabs.map(t => ({path:t.path, preview:t.preview}))")
        assert tabs == [{"path": "README.md", "preview": True}]
    else:
        assert page.evaluate(APP + ".selected") == "notes.md"
        assert page.evaluate("window.__muselab_cm.getValue()") == "unsaved editor fixture"
        assert page.evaluate(APP + ".tabs.some(t => t.path === 'notes.md')")


@pytest.mark.parametrize("action", ["workspace", "tab", "deeplink"])
def test_cancel_cross_workspace_navigation_keeps_dirty_editor(
        page, backend_url, auth_token, tmp_path, action):
    _dirty_editor(page, backend_url, auth_token)
    target = tmp_path / "other-workspace"
    target.mkdir()
    (target / "README.md").write_text("# Other workspace\n", encoding="utf-8")
    response = page.request.post(backend_url + "/api/chat/workspaces",
                                 headers={"X-Auth-Token": auth_token},
                                 data={"path": str(target)})
    assert response.ok
    try:
        page.evaluate("""async target => {
          const app = document.querySelector('#app')._x_dataStack[0];
          await app.fetchSessionWorkspaces();
          const session = {id:'editor-navigation-fixture',name:'Navigation fixture',cwd:target};
          app.sessions = [...app.sessions, session];
          app._pullWorkspaceSessions = async () => ({ok:true, sessions:[session]});
        }""", str(target))
        before = page.evaluate("""() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {workspace:app.activeWorkspace, session:app.currentId, tabs:app.tabs.map(t=>t.path)};
        }""")
        dialogs = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.evaluate("""async ({target, action}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          if (action === 'workspace') await app.switchWorkspace(target);
          else if (action === 'tab') await app.openTab('editor-navigation-fixture');
          else await app._openSessionFromDeeplink('editor-navigation-fixture', target);
        }""", {"target": str(target), "action": action})
        assert len(dialogs) == 1
        after = page.evaluate("""() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          return {workspace:app.activeWorkspace, session:app.currentId, tabs:app.tabs.map(t=>t.path)};
        }""")
        assert after == before
        assert page.evaluate(APP + ".editing && " + APP + "._editorDirty()")
        assert page.evaluate("window.__muselab_cm.getValue()") == "unsaved editor fixture"
        assert not page.evaluate(APP + ".workspaceSwitching || " + APP + ".workspaceSurfaceTransition")
    finally:
        page.request.delete(backend_url + "/api/chat/workspaces", headers={"X-Auth-Token": auth_token},
                            params={"path": str(target)})


@pytest.mark.parametrize("delayed", ["module", "ticket"])
def test_download_stays_bound_to_clicked_workspace(page, backend_url, auth_token, delayed):
    _login(page, backend_url, auth_token)
    result = page.evaluate("""async delayed => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const originalCapabilities = app._fileCapabilities;
      const originalWorkspace = app.fileWorkspacePath;
      let workspace = '/workspace/first', release, minted, downloaded;
      const helpers = {
        mintTicket: async (url, headers, body) => {
          minted = {workspace:decodeURIComponent(headers['X-Muselab-Workspace']), body};
          if (delayed === 'ticket') await new Promise(resolve => {release = resolve;});
          return 'synthetic-download-ticket';
        },
        triggerDownload: (url, name) => { downloaded = {url, name}; },
      };
      app.fileWorkspacePath = () => workspace;
      app._fileCapabilities = () => delayed === 'module'
        ? new Promise(resolve => {release = () => resolve(helpers);}) : Promise.resolve(helpers);
      try {
        const pending = app.downloadFile('notes.md');
        while (!release) await Promise.resolve();
        workspace = '/workspace/second';
        release();
        await pending;
        const url = new URL(downloaded.url, location.origin);
        return {minted, workspace:url.searchParams.get('workspace'),
          path:url.searchParams.get('path'), name:downloaded.name};
      } finally {
        app._fileCapabilities = originalCapabilities;
        app.fileWorkspacePath = originalWorkspace;
      }
    }""", delayed)
    assert result == {"minted": {"workspace": "/workspace/first", "body": {"path": "notes.md"}},
                      "workspace": "/workspace/first", "path": "notes.md", "name": "notes.md"}


@pytest.mark.parametrize("navigation", ["file", "workspace"])
def test_acknowledged_save_invalidates_origin_cache_after_navigation(
        page, backend_url, auth_token, tmp_path, navigation):
    _login(page, backend_url, auth_token)
    saved_text = "# Acknowledged save fixture - " + navigation
    original_notes = page.request.get(backend_url + "/api/files/read?path=notes.md",
                                      headers={"X-Auth-Token": auth_token}).text()
    target = tmp_path / "save-workspace"
    target.mkdir()
    (target / "notes.md").write_text("other workspace notes", encoding="utf-8")
    response = page.request.post(backend_url + "/api/chat/workspaces",
                                 headers={"X-Auth-Token": auth_token},
                                 data={"path": str(target)})
    assert response.ok
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.evaluate("""async target => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._stopFileEvents();
          app._startFileEvents = () => {};
          await app.fetchSessionWorkspaces();
          window.saveOriginWorkspace = app.fileWorkspacePath();
          await app._changeWorkspaceSurface(target);
          await app.openFile({path:'notes.md',name:'notes.md'});
          await app._changeWorkspaceSurface(window.saveOriginWorkspace);
          await app.openFile({path:'notes.md',name:'notes.md'});
          await app.toggleEdit();
        }""", str(target))
        page.wait_for_function("window.__muselab_cm && !" + APP + ".editorLoading")
        page.evaluate("""savedText => {
          const app = document.querySelector('#app')._x_dataStack[0];
          window.__muselab_cm.setValue(savedText);
          const fetchOriginal = window.fetch;
          window.fetch = (url, options) => String(url) === '/api/files/write'
            ? fetchOriginal(url, options).then(response => new Promise(resolve => {
                window.releaseAcknowledgedSave = () => resolve(response);
              })) : fetchOriginal(url, options);
          window.pendingAcknowledgedSave = app.saveEdit();
        }""", saved_text)
        page.wait_for_function("() => !!window.releaseAcknowledgedSave")
        page.evaluate("""async ({target, navigation}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          if (navigation === 'workspace') await app._changeWorkspaceSurface(target);
          else await app.openFile({path:'README.md',name:'README.md'});
          window.releaseAcknowledgedSave();
          await window.pendingAcknowledgedSave;
          if (navigation === 'workspace') {
            // The same relative path in the new workspace keeps its own cache.
            await app.openFile({path:'notes.md',name:'notes.md'});
            await app.$nextTick();
            await new Promise(resolve => requestAnimationFrame(resolve));
            window.otherWorkspaceAfterSave = app.rawText;
            await app._changeWorkspaceSurface(window.saveOriginWorkspace);
          }
          await app.openFile({path:'notes.md',name:'notes.md'});
        }""", {"target": str(target), "navigation": navigation})
        disk = page.request.get(backend_url + "/api/files/read?path=notes.md",
                                headers={"X-Auth-Token": auth_token,
                                         "X-Muselab-Workspace": page.evaluate("encodeURIComponent(window.saveOriginWorkspace)")})
        assert disk.ok and disk.text() == saved_text
        page.wait_for_function(APP + ".previewMode === 'md'")
        assert page.evaluate(APP + ".rawText") == saved_text
        if navigation == "workspace":
            assert page.evaluate("window.otherWorkspaceAfterSave") == "other workspace notes"
    finally:
        restored = page.request.put(backend_url + "/api/files/write",
                                    headers={"X-Auth-Token": auth_token},
                                    data={"path":"notes.md", "content":original_notes})
        assert restored.ok
        removed = page.request.delete(backend_url + "/api/chat/workspaces",
                                      headers={"X-Auth-Token": auth_token},
                                      params={"path": str(target)})
        assert removed.ok
