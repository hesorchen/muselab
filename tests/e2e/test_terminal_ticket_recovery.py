"""A transient ticket failure retries connection to the same fixture PTY."""
from __future__ import annotations

import time

import pytest

from .test_terminal_mobile import _login

APP = "document.querySelector('#app')._x_dataStack[0]"


def _create_terminal(page):
    return page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const response = await app.api('/api/terminals', {method:'POST',
        headers:app.fileHdr(), json:{rows:20,cols:80,profile_id:''}});
      if (!response.ok) throw new Error('fixture terminal creation failed');
      app.terminals = [...app.terminals, response.data];
      return {id:response.data.id,workspace:app.fileWorkspacePath()};
    }""")


def _close_terminal(page, backend_url, auth_token, terminal):
    from urllib.parse import quote

    response = page.request.delete(backend_url + '/api/terminals/' + terminal['id'],
                                   headers={'X-Auth-Token':auth_token,
                                            'X-Muselab-Workspace':quote(terminal['workspace'], safe='')})
    assert response.ok


@pytest.mark.parametrize('mode', ['initial', 'reconnect'])
@pytest.mark.parametrize('failure_status', [0, 502, 503, 504])
def test_one_proxy_failure_recovers_same_terminal(page, backend_url, auth_token, mode, failure_status):
    _login(page, backend_url, auth_token)
    creates = []
    page.on('request', lambda req: creates.append(req.url)
            if req.method == 'POST' and req.url.endswith('/api/terminals') else None)
    terminal = _create_terminal(page)
    attempts = []

    def fail_once(route):
        attempts.append(route.request.url)
        if len(attempts) == 1:
            if failure_status == 0:
                route.abort('connectionreset')
            else:
                route.fulfill(status=failure_status, content_type='text/html', body='<html>Gateway unavailable</html>')
        else:
            route.continue_()

    try:
        if mode == 'reconnect':
            page.evaluate('id => ' + APP + '.openTerminal(id)', terminal['id'])
            page.wait_for_function(APP + ".terminalConnection === 'connected'")
        page.route('**/api/terminals/' + terminal['id'] + '/ticket', fail_once)
        if mode == 'initial':
            page.evaluate('id => ' + APP + '.openTerminal(id)', terminal['id'])
        else:
            page.evaluate(APP + '._terminalSocket.close()')
            page.wait_for_function(APP + ".terminalConnection === 'reconnecting'")
        page.wait_for_function(APP + ".terminalConnection === 'connected'", timeout=5000)
        assert len(attempts) == 2
        assert len(creates) == 1
        assert page.evaluate(APP + '.activeTerminalId') == terminal['id']
        page.evaluate(r"""() => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app._terminalSend("printf '\\nTICKET_RECOVERY_OK\\n'\n");
        }""")
        page.wait_for_function("""() => {
          const term = document.querySelector('#app')._x_dataStack[0]._terminal;
          const buffer = term?.buffer?.active;
          if (!buffer) return false;
          for (let i=0; i<buffer.length; i++) {
            if (buffer.getLine(i)?.translateToString(true) === 'TICKET_RECOVERY_OK') return true;
          }
          return false;
        }""", timeout=5000)
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)


def test_non_json_ticket_failure_preserves_http_status(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const original = window.fetch;
      window.fetch = async () => new Response('<html>Bad Gateway</html>',
        {status:502, statusText:''});
      try {
        const response = await app.api('/api/terminals/synthetic-terminal/ticket', {method:'POST'});
        return {ok:response.ok, status:response.status, error:response.error};
      } finally { window.fetch = original; }
    }""")
    assert result == {"ok": False, "status": 502, "error": "HTTP 502"}


@pytest.mark.parametrize('failure_status, expected_attempts', [(401, 1), (403, 1), (404, 1), (502, 3)])
def test_ticket_failure_is_bounded_without_recreating_terminal(
        page, backend_url, auth_token, failure_status, expected_attempts):
    _login(page, backend_url, auth_token)
    creates = []
    page.on('request', lambda req: creates.append(req.url)
            if req.method == 'POST' and req.url.endswith('/api/terminals') else None)
    terminal = _create_terminal(page)
    attempts = []

    def fail_ticket(route):
        attempts.append(route.request.url)
        route.fulfill(status=failure_status, content_type='application/json',
                      json={'detail':'synthetic ticket failure'})

    page.route('**/api/terminals/' + terminal['id'] + '/ticket', fail_ticket)
    try:
        page.evaluate('id => ' + APP + '.openTerminal(id)', terminal['id'])
        assert page.evaluate(APP + '.terminalConnection') == 'error'
        assert len(attempts) == expected_attempts
        assert len(creates) == 1
        page.wait_for_timeout(1100)
        assert len(attempts) == expected_attempts
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)


@pytest.mark.parametrize('replacement', ['file', 'terminal', 'workspace', 'instance'])
def test_navigation_cancels_ticket_retry_for_previous_owner(
        page, backend_url, auth_token, tmp_path, replacement):
    _login(page, backend_url, auth_token)
    terminal = _create_terminal(page)
    other_terminal = _create_terminal(page) if replacement == 'terminal' else None
    target = tmp_path / 'retry-workspace'
    if replacement == 'workspace':
        target.mkdir()
        (target / 'README.md').write_text('# Retry workspace fixture\n', encoding='utf-8')
        registered = page.request.post(backend_url + '/api/chat/workspaces',
                                       headers={'X-Auth-Token':auth_token}, data={'path':str(target)})
        assert registered.ok
        page.evaluate('() => ' + APP + '.fetchSessionWorkspaces()')
    attempts = []

    def fail_ticket(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, content_type='application/json', json={'detail':'temporary gateway failure'})

    page.route('**/api/terminals/' + terminal['id'] + '/ticket', fail_ticket)
    try:
        page.evaluate('id => { window.pendingRetryOwner = ' + APP + '.openTerminal(id); }', terminal['id'])
        # Observe the first HTTP failure while the connection is in backoff.
        deadline = time.monotonic() + 5
        while not attempts and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert len(attempts) == 1
        page.evaluate("""async ({replacement, target, otherId}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          if (replacement === 'file') await app.openFile({path:'notes.md',name:'notes.md'}, {reveal:true});
          else if (replacement === 'terminal') await app.openTerminal(otherId);
          else if (replacement === 'workspace') await app._changeWorkspaceSurface(target);
          else {
            window.originalRetryTerminal = app._terminal;
            app._terminal = {dispose:() => {}};
          }
          await window.pendingRetryOwner;
          if (replacement === 'instance') {
            app._terminal = window.originalRetryTerminal;
            app._teardownTerminalView();
          }
        }""", {'replacement':replacement, 'target':str(target),
                  'otherId':other_terminal['id'] if other_terminal else ''})
        assert len(attempts) == 1
        if other_terminal:
            page.wait_for_function(APP + ".terminalConnection === 'connected'")
            assert page.evaluate(APP + '.activeTerminalId') == other_terminal['id']
        assert not page.locator('.toast').filter(has_text='temporary gateway failure').count()
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)
        if other_terminal:
            _close_terminal(page, backend_url, auth_token, other_terminal)
        if replacement == 'workspace':
            removed = page.request.delete(backend_url + '/api/chat/workspaces',
                                          headers={'X-Auth-Token':auth_token}, params={'path':str(target)})
            assert removed.ok
