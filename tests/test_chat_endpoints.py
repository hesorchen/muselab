"""Endpoint tests for chat control routes: reset / interrupt / probe.

These hit the FastAPI routes through TestClient with the pool pre-seeded
with fake clients, so the route logic (3-tuple key handling, disconnect
fan-out, response shape) runs for real without spawning a CLI.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

from tests.conftest import TEST_TOKEN


class _FakeSDKClient:
    def __init__(self):
        self.disconnected = False
        self.interrupted = False
        self._raise_on_interrupt = False

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        if self._raise_on_interrupt:
            raise RuntimeError("interrupt boom")
        self.interrupted = True


@pytest.fixture()
def chat_mod(app_module):
    from backend import chat as chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._bypass_state.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._pending_interrupts.clear()
    yield chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._bypass_state.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._pending_interrupts.clear()


def _seed(chat_mod, key, client=None):
    client = client or _FakeSDKClient()
    chat_mod._clients[key] = client
    chat_mod._client_permission[key] = "bypassPermissions"
    chat_mod._bypass_state[key] = {"bypass": True}
    chat_mod._client_lru.append(key)
    return client


# ====== reset ======

def test_reset_single_session(chat_mod, client):
    """reset?session_id=X disconnects that session and returns [X]."""
    c = _seed(chat_mod, ("sid-A", "claude-sonnet-4-6", ""))
    r = client.post(f"/api/chat/reset?session_id=sid-A&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["reset"] == ["sid-A"]
    assert c.disconnected is True
    assert ("sid-A", "claude-sonnet-4-6", "") not in chat_mod._clients


def test_reset_all_with_multiple_three_tuple_keys(chat_mod, client):
    """L183 regression: reset() with NO session_id iterates every pooled
    client. The cache keys are 3-tuples (sid, model, effort); the response
    builder must index key[0]/key[1] (NOT unpack into 2 vars) or it raises
    'too many values to unpack'. Must return ['sid@model', ...]."""
    c1 = _seed(chat_mod, ("sidX", "claude-sonnet-4-6", ""))
    c2 = _seed(chat_mod, ("sidY", "claude-haiku-4-5", "high"))
    c3 = _seed(chat_mod, ("sidX", "deepseek-v4-pro", ""))

    r = client.post(f"/api/chat/reset?token={TEST_TOKEN}")
    assert r.status_code == 200, r.text   # would be 500 if unpack regressed
    body = r.json()
    assert body["ok"] is True
    assert set(body["reset"]) == {
        "sidX@claude-sonnet-4-6",
        "sidY@claude-haiku-4-5",
        "sidX@deepseek-v4-pro",
    }
    # Every client disconnected + pool fully cleared.
    assert all(c.disconnected for c in (c1, c2, c3))
    assert chat_mod._clients == {}
    assert chat_mod._client_lru == []
    assert chat_mod._bypass_state == {}
    assert chat_mod._client_permission == {}


def test_reset_all_empty_pool(chat_mod, client):
    """No live clients → reset returns an empty list, not an error."""
    r = client.post(f"/api/chat/reset?token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "reset": []}


# ====== interrupt ======

def test_interrupt_no_live_client(chat_mod, client):
    """interrupt on a session with no client returns the no-op note,
    NOT an error."""
    r = client.post(f"/api/chat/interrupt?session_id=ghost&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == []
    assert body.get("note") == "no live client"
    # No bogus pending-interrupt flag left behind.
    assert "ghost" not in chat_mod._pending_interrupts


def test_interrupt_calls_sdk_and_marks_pending(chat_mod, client):
    """interrupt must call client.interrupt(), record 'sid@model', and set
    the pending-interrupt flag (used to suppress the turn-done push)."""
    c = _seed(chat_mod, ("sid-int", "claude-sonnet-4-6", ""))
    r = client.post(f"/api/chat/interrupt?session_id=sid-int&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == ["sid-int@claude-sonnet-4-6"]
    assert c.interrupted is True
    assert "sid-int" in chat_mod._pending_interrupts


def test_interrupt_swallows_sdk_error_but_still_marks_pending(chat_mod, client):
    """If client.interrupt() raises, the route must not 500 — it logs and
    returns ok with that client omitted from `interrupted`. Pending flag
    is set BEFORE the SDK call, so it stays set (better early than late)."""
    c = _FakeSDKClient()
    c._raise_on_interrupt = True
    _seed(chat_mod, ("sid-boom", "claude-sonnet-4-6", ""), client=c)
    r = client.post(f"/api/chat/interrupt?session_id=sid-boom&token={TEST_TOKEN}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["interrupted"] == []   # failing client omitted
    assert "sid-boom" in chat_mod._pending_interrupts


def test_interrupt_pauses_nonempty_queue_before_sdk_call(chat_mod, client):
    """The current turn may finish while interrupt() awaits the SDK. Queue
    state must already be paused then, otherwise its finally block can dequeue
    and start the next turn after the user pressed Stop."""
    from backend import sessions as sess

    sid = "sid-queued-stop"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", ""))
    sess.enqueue_message(sid, "do not auto-run")
    observed = []

    async def inspect_interrupt():
        observed.append(sess.get_queue(sid))
        c.interrupted = True

    c.interrupt = inspect_interrupt
    response = client.post(
        f"/api/chat/interrupt?session_id={sid}&token={TEST_TOKEN}")

    assert response.status_code == 200, response.text
    assert observed and observed[0]["paused"] is True
    assert observed[0]["items"][0]["text"] == "do not auto-run"


# ====== force-stop watchdog (interrupt that the SDK refuses to honor) ======

@pytest.mark.asyncio
async def test_session_runtime_cleanup_invalidates_continuation_owner(chat_mod):
    sid = "sid-delete-continuation"
    watcher = asyncio.create_task(asyncio.sleep(60))
    pump = asyncio.create_task(asyncio.sleep(60))
    broadcast = chat_mod.TurnBroadcast(sid)
    broadcast.task = pump
    chat_mod._task_watchers[sid] = watcher
    chat_mod._continuation_generations[sid] = 3
    chat_mod._active_turns[sid] = broadcast
    chat_mod._recent_turns[sid] = broadcast
    chat_mod._sessions_with_inflight_tasks[sid] = {"task-1"}
    chat_mod._bg_task_descriptions["task-1"] = "background work"

    chat_mod._clear_session_runtime_state(sid)
    await asyncio.gather(watcher, pump, return_exceptions=True)

    assert chat_mod._continuation_generations[sid] == 4
    assert sid not in chat_mod._task_watchers
    assert sid not in chat_mod._active_turns
    assert sid not in chat_mod._recent_turns
    assert sid not in chat_mod._sessions_with_inflight_tasks
    assert "task-1" not in chat_mod._bg_task_descriptions
    assert broadcast.cancelled is True
    assert broadcast.done is True


@pytest.mark.asyncio
async def test_force_stop_tears_down_stuck_turn(chat_mod):
    """The SDK's client.interrupt() is best-effort; for an agentic turn the CLI
    may keep running, pinning the slot in _active_turns and bouncing every
    subsequent send with 'previous turn still running'. The force-stop watchdog
    must, after the grace window, kill the client and free the slot itself."""
    sid = "sid-stuck"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", ""))
    bc = chat_mod.TurnBroadcast(session_id=sid, model="claude-sonnet-4-6")
    chat_mod._active_turns[sid] = bc
    try:
        # Tiny grace; the (absent) pump never frees the slot, so the watchdog
        # must force teardown: disconnect the client + free the slot by hand.
        await chat_mod._force_stop_after_grace(sid, bc, grace=0.01)
        assert c.disconnected is True            # CLI killed
        assert sid not in chat_mod._active_turns  # slot freed → next send works
        assert bc.cancelled is True
        assert bc.done is True                    # subscribers get the sentinel
    finally:
        chat_mod._active_turns.pop(sid, None)


@pytest.mark.asyncio
async def test_force_stop_noop_when_turn_drained_naturally(chat_mod):
    """If the SDK interrupt DID drain the turn within the grace window, the
    watchdog must not tear down the (now warm) client — that would needlessly
    drop the CLI subprocess on every successful interrupt."""
    sid = "sid-drained"
    c = _seed(chat_mod, (sid, "claude-sonnet-4-6", ""))
    bc = chat_mod.TurnBroadcast(session_id=sid, model="claude-sonnet-4-6")
    bc.finish()   # turn ended naturally before grace elapsed
    # _active_turns no longer holds it (the pump's finally popped it).
    await chat_mod._force_stop_after_grace(sid, bc, grace=0.01)
    assert c.disconnected is False


# ====== probe_provider ======

def test_probe_unknown_model(client, auth):
    """probe/{model} for an unknown model returns ok=False with a reason,
    not a 500."""
    r = client.get("/api/chat/probe/totally-made-up-model", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "unknown model" in body["reason"]


def test_probe_third_party_without_key(client, auth, monkeypatch):
    """probe for a real third-party model with NO configured API key returns
    ok=False pointing at Settings — no network call made."""
    # conftest already clears DEEPSEEK_API_KEY; be explicit.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = client.get("/api/chat/probe/deepseek-v4-pro", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "not configured" in body["reason"]


def test_probe_hits_vendor_endpoint_with_fake_httpx(client, auth, monkeypatch, chat_mod):
    """With a key set, probe POSTs to the vendor's /v1/messages and echoes
    the vendor status back. We inject a fake httpx.AsyncClient so no real
    network call happens, and assert the body carries the vendor status +
    masked key hint."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-abcd1234efgh5678")

    posted = {}

    class _FakeResp:
        status_code = 200
        text = '{"id":"msg_1","content":[{"type":"text","text":"pong"}]}'

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["json"] = json
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    r = client.get("/api/chat/probe/deepseek-v4-pro", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == 200
    assert body["vendor"]   # display name present
    assert body["url"].endswith("/v1/messages")
    # The key is masked, never echoed in full.
    assert "sk-deepseek-abcd1234efgh5678" not in str(body)
    assert body["key_hint"].startswith("sk-d")
    # The request carried the api key header + ping body.
    assert posted["headers"]["x-api-key"] == "sk-deepseek-abcd1234efgh5678"
    assert posted["json"]["messages"][0]["content"] == "ping"


class _FakeCompactClient:
    def __init__(self, result, totals=(190_000, 190_000)):
        self.result = result
        self.totals = iter(totals)
        self.queries = []
        self.restored_permission = None

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield self.result

    async def get_context_usage(self):
        return {"totalTokens": next(self.totals), "maxTokens": 200_000}

    async def set_permission_mode(self, permission):
        self.restored_permission = permission


def _make_compact_session(client):
    r = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "compact endpoint", "model": "claude-sonnet-4-6"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_native_compact_rejects_in_band_context_error(chat_mod, client, monkeypatch):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id=sid,
        result="Your input exceeds the context window of this model",
        api_error_status=400,
    )
    fake = _FakeCompactClient(result, totals=(190_000,))

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    key = (sid, "claude-sonnet-4-6", "")
    chat_mod._client_permission[key] = "default"

    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 409, r.text
    assert "context window" in r.json()["detail"]
    assert fake.queries == ["/compact"]
    assert fake.restored_permission == "default"


def test_vendor_fork_uses_vendor_session_store_and_restores_env(
    chat_mod, client, monkeypatch, tmp_path,
):
    from backend import endpoints

    r = client.post(
        "/api/chat/sessions",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"name": "vendor fork", "model": "codex:gpt-5.6-sol"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # Test fixture availability filtering can canonicalize the requested model
    # to Claude; pin the metadata to the vendor model this regression targets.
    chat_mod.sess.update_model(sid, "codex:gpt-5.6-sol")
    vendor_dir = tmp_path / "vendor-config"
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", vendor_dir)
    observed = {}

    def fake_fork(*_args, **_kwargs):
        observed["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR")
        return SimpleNamespace(session_id="11111111-2222-4333-8444-555555555555")

    monkeypatch.setattr(chat_mod, "sdk_fork_session", fake_fork)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "original-config")
    response = client.post(
        f"/api/chat/sessions/{sid}/fork",
        headers={"X-Auth-Token": TEST_TOKEN, "Content-Type": "application/json"},
        json={"title": "vendor recovery"},
    )

    assert response.status_code == 200, response.text
    assert observed["config_dir"] == str(vendor_dir)
    assert os.environ["CLAUDE_CONFIG_DIR"] == "original-config"


def test_native_compact_rejects_success_without_token_drop(chat_mod, client, monkeypatch):
    sid = _make_compact_session(client)
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id=sid,
    )
    fake = _FakeCompactClient(result, totals=(190_000, 190_000))

    async def fake_get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat_mod, "get_client", fake_get_client)
    r = client.post(
        f"/api/chat/sessions/{sid}/native-compact",
        headers={"X-Auth-Token": TEST_TOKEN},
    )
    assert r.status_code == 500, r.text
    assert "did not decrease" in r.json()["detail"]
