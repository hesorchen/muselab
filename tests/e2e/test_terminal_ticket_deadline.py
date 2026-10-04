"""Ticket deadlines cover headers/body and cancel the previous view's request."""
from __future__ import annotations

import pytest

from .test_terminal_mobile import _login
from .test_terminal_ticket_recovery import APP, _create_terminal, _close_terminal


def _stall_tickets(page, terminals, phase, *, persistent=False, deadline=120):
    page.evaluate("""({ids,phase,persistent,deadline}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app._terminalTicketTimeoutMs = deadline;
      window.__ticketAttempts = Object.fromEntries(ids.map(id => [id,[]]));
      const original = window.fetch;
      window.fetch = (url, options = {}) => {
        const path = new URL(String(url), location.origin).pathname;
        const id = ids.find(id => path === '/api/terminals/' + id + '/ticket');
        if (!id) return original(url, options);
        const attempts = window.__ticketAttempts[id];
        if (!persistent && attempts.length) return original(url, options);
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        const request = {signal:options.signal, aborted:false, release};
        attempts.push(request);
        options.signal?.addEventListener('abort', () => { request.aborted = true; }, {once:true});
        // Deliberately uncooperative body/header gate: the explicit deadline
        // race must finish even when a WebView/test response ignores abort.
        if (phase === 'headers') return gate.then(() => original(url, options));
        return original(url, options).then(response => ({ok:response.ok,
          status:response.status, statusText:response.statusText, headers:response.headers,
          json:async () => { const data = await response.json(); await gate; return data; }}));
      };
      window.__releaseTicket = id => window.__ticketAttempts[id][0].release();
    }""", {'ids':[terminal['id'] for terminal in terminals], 'phase':phase,
             'persistent':persistent, 'deadline':deadline})


def _ticket_stats(page, terminal):
    return page.evaluate("""id => ({stalled:window.__ticketAttempts[id].length,
      aborted:window.__ticketAttempts[id].filter(request => request.aborted).length})""", terminal['id'])


def _assert_real_shell(page):
    page.evaluate(r"""() => {
      document.querySelector('#app')._x_dataStack[0]._terminalSend("printf '\\nDEADLINE_RECOVERY_OK\\n'\n");
    }""")
    page.wait_for_function("""() => {
      const buffer = document.querySelector('#app')._x_dataStack[0]._terminal?.buffer?.active;
      if (!buffer) return false;
      for (let i=0;i<buffer.length;i++) {
        if (buffer.getLine(i)?.translateToString(true) === 'DEADLINE_RECOVERY_OK') return true;
      }
      return false;
    }""", timeout=3000)


@pytest.mark.parametrize('phase', ['headers', 'body'])
@pytest.mark.parametrize('mode', ['initial', 'reconnect'])
def test_stalled_ticket_recovers_same_real_pty(page, backend_url, auth_token, phase, mode):
    _login(page, backend_url, auth_token)
    creates = []
    tickets = []
    page.on('request', lambda req: creates.append(req.url)
            if req.method == 'POST' and req.url.endswith('/api/terminals') else None)
    terminal = _create_terminal(page)
    page.on('request', lambda req: tickets.append(req.url)
            if req.method == 'POST' and req.url.endswith('/' + terminal['id'] + '/ticket') else None)
    try:
        if mode == 'reconnect':
            page.evaluate('id => ' + APP + '.openTerminal(id)', terminal['id'])
            page.wait_for_function(APP + ".terminalConnection === 'connected'")
            tickets.clear()
        _stall_tickets(page, [terminal], phase)
        if mode == 'initial':
            page.evaluate('id => { window.__pendingConnect = ' + APP + '.openTerminal(id); }', terminal['id'])
        else:
            page.evaluate(APP + '._terminalSocket.close()')
            page.wait_for_function(APP + ".terminalConnection === 'reconnecting'")
        page.wait_for_function(APP + ".terminalConnection === 'connected'", timeout=3500)
        assert _ticket_stats(page, terminal) == {'stalled':1, 'aborted':1}
        assert len(tickets) == (1 if phase == 'headers' else 2)
        assert len(creates) == 1
        assert page.evaluate(APP + '.activeTerminalId') == terminal['id']
        _assert_real_shell(page)
        # Resolving an abandoned response after recovery cannot replace its view.
        page.evaluate('id => window.__releaseTicket(id)', terminal['id'])
        page.wait_for_timeout(80)
        assert page.evaluate(APP + '.terminalConnection') == 'connected'
        assert len(creates) == 1
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_ticket_deadline_retries_are_bounded(page, backend_url, auth_token, phase):
    _login(page, backend_url, auth_token)
    terminal = _create_terminal(page)
    try:
        _stall_tickets(page, [terminal], phase, persistent=True)
        page.evaluate('id => { window.__pendingConnect = ' + APP + '.openTerminal(id); }', terminal['id'])
        page.wait_for_function(APP + ".terminalConnection === 'error'", timeout=3000)
        assert _ticket_stats(page, terminal) == {'stalled':3, 'aborted':3}
        assert page.evaluate(APP + '._terminalTicketAbort') is None
        page.wait_for_timeout(400)
        assert _ticket_stats(page, terminal)['stalled'] == 3
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)


@pytest.mark.parametrize('phase', ['headers', 'body'])
@pytest.mark.parametrize('replacement', ['file', 'terminal', 'close', 'workspace'])
def test_navigation_aborts_stalled_ticket_without_touching_new_owner(
        page, backend_url, auth_token, tmp_path, phase, replacement):
    _login(page, backend_url, auth_token)
    terminal = _create_terminal(page)
    other = _create_terminal(page) if replacement == 'terminal' else None
    target = tmp_path / 'deadline-workspace'
    if replacement == 'workspace':
        target.mkdir()
        (target / 'README.md').write_text('# Deadline fixture\n', encoding='utf-8')
        registered = page.request.post(backend_url + '/api/chat/workspaces',
                                       headers={'X-Auth-Token':auth_token}, data={'path':str(target)})
        assert registered.ok
        page.evaluate('() => ' + APP + '.fetchSessionWorkspaces()')
    closed = False
    try:
        _stall_tickets(page, [terminal] + ([other] if other else []), phase, deadline=3000)
        if other:
            # Hold the old continuation until the replacement owns its request;
            # old finally must release only its own AbortController.
            page.evaluate("""id => {
              const app = document.querySelector('#app')._x_dataStack[0];
              const request = app._requestTerminalTicket.bind(app);
              app._requestTerminalTicket = async (...args) => {
                const result = await request(...args);
                if (args[0] === id) await new Promise(resolve => { window.__releaseOldContinuation = resolve; });
                return result;
              };
            }""", terminal['id'])
        page.evaluate('id => { window.__pendingConnect = ' + APP + '.openTerminal(id); }', terminal['id'])
        page.wait_for_function('id => window.__ticketAttempts[id].length === 1', arg=terminal['id'])
        page.evaluate("""({replacement,target,otherId,id}) => {
          const app = document.querySelector('#app')._x_dataStack[0];
          window.__oldConnectSettled = false;
          window.__pendingConnect.finally(() => { window.__oldConnectSettled = true; });
          if (replacement === 'file') window.__navigation = app.openFile({path:'notes.md',name:'notes.md'}, {reveal:true});
          else if (replacement === 'terminal') window.__navigation = app.openTerminal(otherId);
          else if (replacement === 'close') window.__navigation = app.closeTerminal(id, {confirm:false});
          else window.__navigation = app._changeWorkspaceSurface(target);
        }""", {'replacement':replacement, 'target':str(target),
                  'otherId':other['id'] if other else '', 'id':terminal['id']})
        if other:
            page.wait_for_function('id => window.__ticketAttempts[id].length === 1', arg=other['id'])
            page.evaluate("() => { window.__newTicketOwner = " + APP + "._terminalTicketAbort; }")
            page.wait_for_function('!!window.__releaseOldContinuation')
            page.evaluate('window.__releaseOldContinuation()')
        if replacement == 'close':
            # PTY termination itself has a backend grace period. Measure view
            # cancellation after the confirmed DELETE, when teardown runs.
            page.evaluate('async () => { await window.__navigation; }')
            closed = True
        page.wait_for_function('window.__oldConnectSettled', timeout=1000)
        assert _ticket_stats(page, terminal) == {'stalled':1, 'aborted':1}
        if other:
            assert page.evaluate(APP + '._terminalTicketAbort === window.__newTicketOwner') is True
            assert _ticket_stats(page, other) == {'stalled':1, 'aborted':0}
            page.evaluate('id => window.__releaseTicket(id)', other['id'])
            page.wait_for_function(APP + ".terminalConnection === 'connected'")
            assert page.evaluate(APP + '.activeTerminalId') == other['id']
            _assert_real_shell(page)
        else:
            page.evaluate('async () => { await window.__navigation; }')
        page.evaluate('id => window.__releaseTicket(id)', terminal['id'])
        page.wait_for_timeout(300)
        assert _ticket_stats(page, terminal)['stalled'] == 1
        assert not page.locator('.toast').filter(has_text='request deadline exceeded').count()
    finally:
        if not closed:
            _close_terminal(page, backend_url, auth_token, terminal)
        if other:
            _close_terminal(page, backend_url, auth_token, other)
        if replacement == 'workspace':
            removed = page.request.delete(backend_url + '/api/chat/workspaces',
                                          headers={'X-Auth-Token':auth_token}, params={'path':str(target)})
            assert removed.ok


@pytest.mark.parametrize('status', [401, 403, 404])
def test_denied_ticket_does_not_wait_for_error_body_or_retry(page, backend_url, auth_token, status):
    _login(page, backend_url, auth_token)
    terminal = _create_terminal(page)
    try:
        page.evaluate("""({id,status}) => {
          const original = window.fetch;
          window.__deniedCalls = 0;
          window.__deniedBodyReads = 0;
          window.__deniedBodyCancels = 0;
          window.fetch = (url, options = {}) => {
            if (String(url).endsWith('/' + id + '/ticket')) {
              window.__deniedCalls++;
              return Promise.resolve({ok:false,status,statusText:'',
                body:{cancel:() => { window.__deniedBodyCancels++; return new Promise(() => {}); }},
                json:() => { window.__deniedBodyReads++; return new Promise(() => {}); }});
            }
            return original(url, options);
          };
        }""", {'id':terminal['id'], 'status':status})
        page.evaluate('id => { window.__pendingConnect = ' + APP + '.openTerminal(id); }', terminal['id'])
        page.wait_for_function(APP + ".terminalConnection === 'error'", timeout=1000)
        assert page.evaluate('window.__deniedCalls') == 1
        assert page.evaluate('window.__deniedBodyReads') == 0
        assert page.evaluate('window.__deniedBodyCancels') == 1
        assert page.locator('.toast').filter(has_text=f'HTTP {status}').count() == 1
    finally:
        _close_terminal(page, backend_url, auth_token, terminal)
