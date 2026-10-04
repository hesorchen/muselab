"""Global Todo/Activity ticket reads recover and cancel with their live owner."""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .test_activity_center import _login

APP = "document.querySelector('#app')._x_dataStack[0]"
CHANNELS = {'todos':('_startTodoEvents', '_todoLive'),
            'activity':('_startActivityEvents', '_activityLive')}


def _install_gate(page, channel, phase, *, held_reads=1):
    page.evaluate("""({channel,phase,heldReads}) => {
      const original = window.fetch;
      window.__ticketRequests = [];
      window.__ticketCalls = 0;
      window.fetch = (url, options = {}) => {
        const path = new URL(String(url), location.origin).pathname;
        if (path !== '/api/' + channel + '/events-ticket') return original(url, options);
        window.__ticketCalls++;
        if (window.__ticketRequests.length >= heldReads) return original(url, options);
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        const request = {release, aborted:false, signal:options.signal};
        window.__ticketRequests.push(request);
        options.signal?.addEventListener('abort', () => { request.aborted = true; }, {once:true});
        // Ignore abort in this gate to verify the explicit deadline/cancel race.
        if (phase === 'headers') return gate.then(() => original(url, options));
        return original(url, options).then(response => ({ok:response.ok,status:response.status,
          json:async () => { const data = await response.json(); await gate; return data; }}));
      };
    }""", {'channel':channel, 'phase':phase, 'heldReads':held_reads})


def _prepare(page, backend_url, auth_token, channel, phase, *, held_reads=1):
    _login(page, backend_url, auth_token)
    page.wait_for_function("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return app._todoLiveSource?.readyState === EventSource.OPEN
        && app._activityLiveSource?.readyState === EventSource.OPEN;
    }""", timeout=5000)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app._stopTodoEvents(); app._stopActivityEvents();
      app._abortActivityFetches();
      app.REQUEST_DEADLINE_MS = 150;
      app._activityLiveFailures = 0;
      window.__liveReady = 0; window.__liveUpdates = 0;
      const NativeSource = window.EventSource;
      window.EventSource = class extends NativeSource {
        constructor(url, options) {
          super(url, options);
          this.addEventListener('ready', () => { window.__liveReady++; });
          this.addEventListener('update', () => { window.__liveUpdates++; });
        }
      };
    }""")
    # Prevent convenient HTTP snapshots from hiding a broken live update.
    if channel == 'activity':
        page.route('**/api/activity?*', lambda route: route.fulfill(status=304))
    _install_gate(page, channel, phase, held_reads=held_reads)


def _start(page, channel):
    method, _ = CHANNELS[channel]
    page.evaluate("""method => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__liveReadSettled = false;
      window.__liveRead = app[method]();
      window.__liveRead.finally(() => { window.__liveReadSettled = true; });
    }""", method)
    page.wait_for_function('window.__ticketRequests.length === 1')


def _assert_visible_update(page, backend_url, auth_token, channel):
    marker = 'Live recovery ' + uuid.uuid4().hex[:8]
    headers = {'X-Auth-Token':auth_token}
    if channel == 'todos':
        response = page.request.put(backend_url + '/api/todos', headers=headers,
                                    data={'items':[{'id':marker, 'text':marker, 'priority':'high'}]})
        assert response.ok
        page.locator('.session-todo-btn').click()
        expect(page.locator('.session-todo-item strong').filter(has_text=marker)).to_be_visible()
    else:
        page.locator('.activity-center-btn').click()
        page.evaluate(APP + ".setActivityView('groups')")
        response = page.request.post(backend_url + '/api/activity/groups', headers=headers,
                                     data={'name':marker, 'color':'blue'})
        assert response.ok
        expect(page.locator('.activity-modal strong').filter(has_text=marker)).to_be_visible()
        group_id = response.json()['group']['id']
        assert page.request.delete(backend_url + '/api/activity/groups/' + group_id, headers=headers).ok
    assert page.evaluate('window.__liveReady') >= 1
    assert page.evaluate('window.__liveUpdates') >= 1


@pytest.mark.parametrize('channel', ['todos', 'activity'])
@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_stalled_ticket_recovers_ready_and_visible_live_update(
        page, backend_url, auth_token, channel, phase):
    _prepare(page, backend_url, auth_token, channel, phase)
    _start(page, channel)
    _, prefix = CHANNELS[channel]
    page.wait_for_function(APP + f'.{prefix}Source?.readyState === EventSource.OPEN', timeout=4000)
    assert page.evaluate('window.__ticketCalls') == 2
    assert page.evaluate('window.__ticketRequests[0].aborted') is True
    _assert_visible_update(page, backend_url, auth_token, channel)
    page.evaluate('window.__ticketRequests[0].release()')
    page.wait_for_timeout(80)
    assert page.evaluate(APP + f'.{prefix}Source?.readyState === EventSource.OPEN') is True


@pytest.mark.parametrize('channel', ['todos', 'activity'])
@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_pagehide_aborts_ticket_without_retry_or_late_source(
        page, backend_url, auth_token, channel, phase):
    _prepare(page, backend_url, auth_token, channel, phase)
    page.evaluate(APP + '.REQUEST_DEADLINE_MS = 10000')
    _start(page, channel)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    page.wait_for_function('window.__liveReadSettled', timeout=1000)
    assert page.evaluate('window.__ticketRequests[0].aborted') is True
    page.evaluate('window.__ticketRequests[0].release()')
    page.wait_for_timeout(1600)
    _, prefix = CHANNELS[channel]
    assert page.evaluate(APP + f'.{prefix}Source') is None
    assert page.evaluate(APP + f'.{prefix}Timer') is None
    assert page.evaluate('window.__ticketCalls') == 1


@pytest.mark.parametrize('channel', ['todos', 'activity'])
def test_first_ticket_is_already_owned_by_pagehide(page, backend_url, auth_token, channel):
    # No prior successful ticket: Todo must bind lifecycle listeners before
    # the first await, rather than after the EventSource has been constructed.
    page.add_init_script("""channel => {
      const original = window.fetch;
      window.__initialTicket = null;
      window.fetch = (url, options = {}) => {
        if (String(url).endsWith('/api/' + channel + '/events-ticket')) {
          window.__initialTicket = {aborted:false};
          options.signal?.addEventListener('abort', () => { window.__initialTicket.aborted = true; }, {once:true});
          return new Promise(() => {});
        }
        return original(url, options);
      };
    }""".replace('channel => {', '(() => { const channel = ' + repr(channel) + ';', 1) + ')();')
    _login(page, backend_url, auth_token)
    page.wait_for_function('!!window.__initialTicket')
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}))")
    page.wait_for_function('window.__initialTicket.aborted', timeout=1000)


@pytest.mark.parametrize('channel', ['todos', 'activity'])
def test_old_ticket_finally_cannot_release_replacement_controller(
        page, backend_url, auth_token, channel):
    _prepare(page, backend_url, auth_token, channel, 'body', held_reads=2)
    page.evaluate("""channel => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.REQUEST_DEADLINE_MS = 10000;
      const fetchWithDeadline = app._fetchWithDeadline.bind(app);
      let calls = 0;
      app._fetchWithDeadline = async (...args) => {
        const held = args[0] === '/api/' + channel + '/events-ticket' && ++calls === 1;
        try { return await fetchWithDeadline(...args); }
        finally { if (held) await new Promise(resolve => { window.__releaseOldContinuation = resolve; }); }
      };
    }""", channel)
    _start(page, channel)
    method, prefix = CHANNELS[channel]
    page.evaluate("""({method,prefix}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app[method.replace('_start', '_stop')]();
      window.__newLiveRead = app[method]();
      window.__newTicketOwner = app[prefix + 'Controller'];
    }""", {'method':method, 'prefix':prefix})
    page.wait_for_function('window.__ticketRequests.length === 2')
    page.wait_for_function('!!window.__releaseOldContinuation')
    page.evaluate('window.__releaseOldContinuation()')
    page.evaluate('async () => { await window.__liveRead; }')
    assert page.evaluate(APP + f'.{prefix}Controller === window.__newTicketOwner') is True
    assert page.evaluate('window.__ticketRequests[1].aborted') is False
    page.evaluate('window.__ticketRequests[0].release(); window.__ticketRequests[1].release()')
    page.wait_for_function(APP + f'.{prefix}Source?.readyState === EventSource.OPEN')
    assert page.evaluate('window.__ticketCalls') == 2


@pytest.mark.parametrize('channel', ['todos', 'activity'])
def test_workspace_switch_keeps_global_subscription_and_live_update(
        page, backend_url, auth_token, tmp_path, channel):
    _prepare(page, backend_url, auth_token, channel, 'body', held_reads=0)
    method, prefix = CHANNELS[channel]
    page.evaluate('method => ' + APP + '[method]()', method)
    page.wait_for_function(APP + f'.{prefix}Source?.readyState === EventSource.OPEN')
    page.evaluate('prefix => { window.__globalSource = ' + APP + '[prefix + "Source"]; }', prefix)
    target = tmp_path / 'live-workspace'
    target.mkdir()
    (target / 'README.md').write_text('# Global live fixture\n', encoding='utf-8')
    assert page.request.post(backend_url + '/api/chat/workspaces',
                             headers={'X-Auth-Token':auth_token}, data={'path':str(target)}).ok
    try:
        page.evaluate('() => ' + APP + '.fetchSessionWorkspaces()')
        page.evaluate('path => ' + APP + '._changeWorkspaceSurface(path)', str(target))
        assert page.evaluate(APP + f'.{prefix}Source === window.__globalSource') is True
        assert page.evaluate('window.__ticketCalls') == 1
        _assert_visible_update(page, backend_url, auth_token, channel)
    finally:
        assert page.request.delete(backend_url + '/api/chat/workspaces',
                                   headers={'X-Auth-Token':auth_token}, params={'path':str(target)}).ok
