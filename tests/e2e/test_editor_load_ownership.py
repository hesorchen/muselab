"""An old read must not revive editing after the user navigates away and back."""
from __future__ import annotations

import pytest

from .test_files_preview import _login


@pytest.mark.parametrize("delayed", ["headers", "body"])
def test_pending_editor_read_does_not_reopen_after_file_roundtrip(
        page, backend_url, auth_token, delayed):
    _login(page, backend_url, auth_token)
    result = page.evaluate(
        """async delayed => {
          const app = document.querySelector("#app")._x_dataStack[0];
          const file = {path: "smooth-preview.html", name: "smooth-preview.html"};
          await app.openFile(file);
          const originalFetch = window.fetch;
          let finish;
          const staleBody = "<h1>old read result</h1>";
          window.fetch = (url, options) => {
            if (!String(url).includes("/api/files/read?path=smooth-preview.html")) {
              return originalFetch(url, options);
            }
            if (delayed === "headers") {
              return new Promise(resolve => { finish = () => resolve(new Response(staleBody)); });
            }
            return Promise.resolve({ok: true,
              text: () => new Promise(resolve => { finish = () => resolve(staleBody); })});
          };
          try {
            const pending = app.toggleEdit();
            while (!finish) await Promise.resolve();
            await app.openFile({path: "notes.md", name: "notes.md"});
            await app.openFile(file);
            const before = {selected: app.selected, editing: app.editing, mode: app.previewMode};
            finish();
            await pending;
            return {before, after: {selected: app.selected, editing: app.editing,
              mode: app.previewMode}, staleBodyApplied: app.rawText === staleBody};
          } finally {
            window.fetch = originalFetch;
          }
        }""",
        delayed,
    )
    assert result["before"] == {
        "selected": "smooth-preview.html", "editing": False, "mode": "html",
    }
    assert result["after"] == result["before"]
    assert result["staleBodyApplied"] is False
