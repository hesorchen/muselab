"""Command-palette searches belong to one visible opening of the panel."""
import pytest
from playwright.sync_api import expect

from .test_multi_tab import _login


def test_closing_palette_before_debounce_does_not_start_hidden_searches(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const original = window.fetch;
      window.__hiddenSearchCalls = [];
      window.fetch = (url, options = {}) => {
        const parsed = new URL(String(url), location.origin);
        if (parsed.pathname === '/api/files/search' || parsed.pathname === '/api/chat/search') {
          window.__hiddenSearchCalls.push({kind:parsed.pathname, shown:app.palette.show,
            signal:!!options.signal});
          return Promise.resolve(new Response(JSON.stringify(
            parsed.pathname === '/api/files/search'
              ? {entries:[{path:'synthetic-needle.txt',name:'synthetic-needle.txt',is_dir:false}]}
              : {hits:[{uuid:'synthetic-message',sid:app.currentId,snippet:'synthetic needle'}]})));
        }
        return original(url, options);
      };
    }""")
    page.keyboard.press('Control+k')
    page.locator('.cmd-palette-input').fill('synthetic needle')
    page.locator('.cmd-palette-input').press('Escape')
    page.wait_for_function("!document.querySelector('#app')._x_dataStack[0].palette.show")
    page.wait_for_timeout(450)
    state = page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return {shown:app.palette.show, calls:window.__hiddenSearchCalls,
        fileResults:app.palette.fileResults.map(row => row.path),
        messageResults:app.palette.messageResults.map(row => row.uuid),
        fileLoading:app.palette.fileLoading, messageLoading:app.palette.messageLoading};
    }""")
    assert state['calls'] == []
    assert state['fileResults'] == []
    assert state['messageResults'] == []



@pytest.mark.parametrize("release_stage", ["debounce", "request"])
def test_reopening_same_query_rejects_old_results_and_retains_new_request(
        page, backend_url, auth_token, release_stage):
    _login(page, backend_url, auth_token)
    page.evaluate("""() => {
      const original = window.fetch;
      window.__paletteRequests = [];
      window.fetch = (url, options = {}) => {
        const path = new URL(String(url), location.origin).pathname;
        if (path !== '/api/files/search' && path !== '/api/chat/search') return original(url, options);
        return new Promise(resolve => {
          window.__paletteRequests.push({path, signal:options.signal, resolve});
        });
      };
      window.__resolvePaletteRequests = (start, label) => {
        for (const request of window.__paletteRequests.slice(start, start+2)) {
          request.resolve(new Response(JSON.stringify(
            request.path === '/api/files/search'
              ? {entries:[{path:label+'.txt',name:label+'.txt',is_dir:false}]}
              : {hits:[{uuid:label,snippet:label}]})));
        }
      };
    }""")
    page.keyboard.press('Control+k')
    page.locator('.cmd-palette-input').fill('needle')
    page.wait_for_function("window.__paletteRequests.length === 2")
    page.locator('.cmd-palette-input').press('Escape')
    closed = page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return {shown:app.palette.show, fileLoading:app.palette.fileLoading,
        messageLoading:app.palette.messageLoading,
        aborted:window.__paletteRequests.map(request => !!request.signal?.aborted)};
    }""")
    page.keyboard.press('Control+k')
    page.locator('.cmd-palette-input').fill('needle')
    if release_stage == 'request':
        page.wait_for_function("window.__paletteRequests.length === 4")
    # Deliberately deliver a canceled response too: ownership must survive a
    # proxy finishing a response before the browser observes the cancellation.
    stale = page.evaluate("""async () => {
      window.__resolvePaletteRequests(0, 'old-needle');
      await new Promise(resolve => setTimeout(resolve, 0));
      const app = document.querySelector('#app')._x_dataStack[0];
      return {files:app.palette.fileResults.map(row => row.path),
        messages:app.palette.messageResults.map(row => row.uuid),
        fileLoading:app.palette.fileLoading, messageLoading:app.palette.messageLoading};
    }""")
    page.wait_for_function("window.__paletteRequests.length === 4")
    new_signals = page.evaluate("window.__paletteRequests.slice(2).map(request => !!request.signal?.aborted)")
    page.evaluate("window.__resolvePaletteRequests(2, 'new-needle')")
    page.wait_for_function("""() => {
      const palette = document.querySelector('#app')._x_dataStack[0].palette;
      return palette.fileResults[0]?.path === 'new-needle.txt'
        && palette.messageResults[0]?.uuid === 'new-needle'
        && !palette.fileLoading && !palette.messageLoading;
    }""")
    assert closed == {"shown": False, "fileLoading": False, "messageLoading": False,
                      "aborted": [True, True]}
    assert stale == {"files": [], "messages": [],
                     "fileLoading": release_stage == 'request',
                     "messageLoading": release_stage == 'request'}
    assert new_signals == [False, False]
    expect(page.locator('.cmd-palette-input')).to_have_value('needle')
