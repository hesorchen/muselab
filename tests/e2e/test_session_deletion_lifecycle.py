"""Acknowledged deletion remains authoritative through failed or stale catalog reads."""
import re

import pytest
from playwright.sync_api import expect

from .test_multi_tab import _login

APP = "document.querySelector('#app')._x_dataStack[0]"


def _prepare(page, backend_url, auth_token, *, close_survivor=False):
    _login(page, backend_url, auth_token)
    survivor = page.evaluate(APP + '.currentId')
    page.locator('.chat-input-textarea').fill('survivor recovery draft')
    page.locator('.chat-tab-new').click()
    deleted = page.evaluate(APP + '.currentId')
    page.locator('.chat-input-textarea').fill('target recovery draft')
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      if (app._sessionsSyncTimer) clearInterval(app._sessionsSyncTimer);
      app._sessionsSyncTimer = null;
      if (app._sessionListPullPromise) await app._sessionListPullPromise;
      await Promise.all(Object.values(app._sessionRegistrationPromises));
      app._captureComposerState();
    }""")
    if close_survivor:
        page.locator(f'.chat-tab[data-tid="{survivor}"] .chat-tab-close').click()
    return deleted, survivor


def _install_controlled_catalog(page, deleted, *, delete="ok", catalog="failure"):
    page.evaluate("""({sid,deletion,catalog}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.sessions = [...app.sessions.filter(row => row.id === sid), ...app.sessions.filter(row => row.id !== sid)];
      window.__canonicalRows = app.sessions.map(row => ({...row}));
      window.__targetState = app.tabState[sid];
      window.__deleteDone = false;
      window.__deleteCalls = 0;
      window.__listCalls = 0;
      window.__listRequests = [];
      const deleteById = app.deleteSessionById.bind(app);
      app.deleteSessionById = async (...args) => {
        window.__deleteResult = await deleteById(...args);
        window.__deleteDone = true;
        return window.__deleteResult;
      };
      const original = window.fetch;
      const acknowledge = () => {
        window.__canonicalRows = window.__canonicalRows.filter(row => row.id !== sid);
        window.__deleteAcknowledged = true;
        return new Response(JSON.stringify({ok:true}), {status:200});
      };
      window.fetch = (url, options = {}) => {
        const path = new URL(String(url), location.origin).pathname;
        if (path === '/api/chat/sessions/' + sid && options.method === 'DELETE') {
          window.__deleteCalls++;
          if (deletion === 'network') return Promise.reject(new TypeError('synthetic deletion interruption'));
          if (deletion === 'http') return Promise.resolve(new Response('synthetic deletion unavailable', {status:503}));
          if (deletion === 'hold') return new Promise(resolve => {
            window.__releaseDelete = () => resolve(acknowledge());
          });
          return Promise.resolve(acknowledge());
        }
        if (path === '/api/chat/sessions' && (!options.method || options.method === 'GET')) {
          window.__listCalls++;
          if (catalog === 'failure') return Promise.resolve(new Response('synthetic catalog unavailable', {status:503}));
          const rows = window.__canonicalRows.map(row => ({...row}));
          if (catalog === 'hold') {
            let release;
            const body = new Promise(resolve => { release = resolve; });
            window.__listRequests.push({rows, release});
            return Promise.resolve({status:200, ok:true,
              headers:new Headers({etag:'"catalog-'+window.__listCalls+'"'}), json:() => body});
          }
          return Promise.resolve(new Response(JSON.stringify({sessions:rows})));
        }
        if (path === '/api/chat/sessions/' + sid && window.__deleteAcknowledged) {
          return Promise.resolve(new Response('synthetic session deleted', {status:404}));
        }
        return original(url, options);
      };
      window.__releaseList = (index, rows) => {
        const request = window.__listRequests[index];
        request.release({sessions:rows || request.rows, session_redirects:{}});
      };
    }""", {"sid": deleted, "deletion": delete, "catalog": catalog})


def _delete_from_menu(page, sid):
    page.locator(f'.chat-tab[data-tid="{sid}"]').click(button='right')
    page.locator('button').filter(has_text=re.compile(r'^(删除会话|Delete session)$')).click()
    page.locator('.modal-foot .btn-danger:visible').click()


def _state(page, deleted, survivor):
    return page.evaluate("""([deleted,survivor]) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return {result:window.__deleteResult, deleteCalls:window.__deleteCalls,
        listCalls:window.__listCalls, current:app.currentId === deleted ? 'target'
          : app.currentId === survivor ? 'survivor' : 'other',
        targetListed:app.sessions.some(row => row.id === deleted),
        targetOpen:app.openTabIds.includes(deleted),
        targetRuntime:!!app.tabState[deleted],
        sameTarget:app.tabState[deleted] === window.__targetState,
        targetDraft:app._chatDraftRecord(deleted).text,
        survivorDraft:app._chatDraftRecord(survivor).text};
    }""", [deleted, survivor])


@pytest.mark.parametrize("failure", ["http", "network"])
def test_failed_deletion_preserves_session_runtime_and_recoverable_draft(
        page, backend_url, auth_token, failure):
    deleted, survivor = _prepare(page, backend_url, auth_token)
    _install_controlled_catalog(page, deleted, delete=failure)
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const st = app.tabState[app.currentId];
      window.__runtimeCloses = 0;
      st.streaming = true;
      st.streamPhase = 'streaming';
      st.es = {readyState:1, close:() => { window.__runtimeCloses++; }};
      window.__runningStream = st.es;
    }""")
    _delete_from_menu(page, deleted)
    page.wait_for_function('window.__deleteDone')
    state = _state(page, deleted, survivor)
    assert state == {"result": False, "deleteCalls": 1, "listCalls": 0,
                     "current": "target", "targetListed": True, "targetOpen": True,
                     "targetRuntime": True, "sameTarget": True,
                     "targetDraft": "target recovery draft", "survivorDraft": "survivor recovery draft"}
    expect(page.locator('.chat-input-textarea')).to_have_value('target recovery draft')
    running = page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const st = app.tabState[app.currentId];
      return {streaming:st.streaming, sameStream:st.es === window.__runningStream,
        closed:window.__runtimeCloses};
    }""")
    assert running == {"streaming": True, "sameStream": True, "closed": 0}


def test_acknowledged_deletion_does_not_reopen_target_after_catalog_failure(
        page, backend_url, auth_token):
    deleted, survivor = _prepare(page, backend_url, auth_token, close_survivor=True)
    _install_controlled_catalog(page, deleted)
    _delete_from_menu(page, deleted)
    page.wait_for_function('window.__deleteDone')
    state = _state(page, deleted, survivor)
    assert state == {"result": True, "deleteCalls": 1, "listCalls": 1,
                     "current": "survivor", "targetListed": False, "targetOpen": False,
                     "targetRuntime": False, "sameTarget": False,
                     "targetDraft": "", "survivorDraft": "survivor recovery draft"}
    expect(page.locator('.chat-input-textarea')).to_have_value('survivor recovery draft')


def test_predelete_catalog_cannot_reinject_target_or_release_new_catalog_owner(
        page, backend_url, auth_token):
    deleted, survivor = _prepare(page, backend_url, auth_token)
    _install_controlled_catalog(page, deleted, catalog="hold")
    page.evaluate("() => { window.__oldCatalog = document.querySelector('#app')._x_dataStack[0]._syncSessionListQuiet(); }")
    page.wait_for_function('window.__listRequests.length === 1')
    _delete_from_menu(page, deleted)
    page.wait_for_function('window.__deleteAcknowledged')
    page.wait_for_function('window.__listRequests.length === 2', timeout=3000)
    reads_after_ack = page.evaluate('window.__listCalls')
    page.evaluate("() => { window.__freshCatalog = " + APP + "._sessionListPullPromise; }")
    old = page.evaluate("""async sid => {
      window.__releaseList(0);
      await window.__oldCatalog;
      const app = document.querySelector('#app')._x_dataStack[0];
      return {targetListed:app.sessions.some(row => row.id === sid),
        freshOwner:!!window.__freshCatalog && app._sessionListPullPromise === window.__freshCatalog,
        etag:app._sessionsEtag};
    }""", deleted)
    if reads_after_ack >= 2:
        page.evaluate('window.__releaseList(1)')
    page.wait_for_function('window.__deleteDone')
    assert reads_after_ack == 2
    assert old == {"targetListed": False, "freshOwner": True, "etag": ""}
    state = _state(page, deleted, survivor)
    assert state['current'] == 'survivor' and not state['targetListed'] and not state['targetRuntime']
    # A later canonical restore of the same id is valid; no permanent tombstone.
    restored = page.evaluate("""async sid => {
      const app = document.querySelector('#app')._x_dataStack[0];
      window.__canonicalRows.unshift({...window.__listRequests[0].rows.find(row => row.id === sid), name:'synthetic restored session'});
      const pull = app.refreshSessions();
      window.__releaseList(2);
      await pull;
      return app.sessions.some(row => row.id === sid && row.name === 'synthetic restored session');
    }""", deleted)
    assert restored is True


@pytest.mark.parametrize("wait_phase", ["delete", "catalog"])
def test_late_deletion_keeps_newly_selected_session_and_its_draft(
        page, backend_url, auth_token, wait_phase):
    deleted, survivor = _prepare(page, backend_url, auth_token)
    _install_controlled_catalog(page, deleted,
                                delete="hold" if wait_phase == "delete" else "ok",
                                catalog="hold" if wait_phase == "catalog" else "ok")
    _delete_from_menu(page, deleted)
    page.wait_for_function('!!window.__releaseDelete' if wait_phase == 'delete' else 'window.__listRequests.length === 1')
    page.locator(f'.chat-tab[data-tid="{survivor}"] .chat-tab-name').click()
    page.locator('.chat-input-textarea').fill('new survivor draft')
    page.evaluate("window.__releaseDelete()" if wait_phase == 'delete' else 'window.__releaseList(0)')
    page.wait_for_function('window.__deleteDone')
    expect(page.locator('.chat-input-textarea')).to_have_value('new survivor draft')
    page.wait_for_function("sid => document.querySelector('#app')._x_dataStack[0]._chatDraftRecord(sid).text === 'new survivor draft'", arg=survivor)
    state = _state(page, deleted, survivor)
    assert state['result'] is True and state['current'] == 'survivor'
    assert not state['targetListed'] and not state['targetOpen'] and not state['targetRuntime']
    assert state['targetDraft'] == '' and state['survivorDraft'] == 'new survivor draft'
    expect(page.locator('.chat-input-textarea')).to_have_value('new survivor draft')
