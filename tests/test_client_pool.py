"""Client-pool behavior for chat.get_client / disconnect_client.

These guard the long-lived state the rest of the chat surface depends on:
the (sid, model, effort, service_tier) -> ClaudeSDKClient cache plus its side registries
(_client_permission / _creation_locks / _client_lru).

Production code spawns a real CLI subprocess in _build_and_connect_client;
we monkeypatch THAT (not get_client) so the cache/LRU/eviction logic under
test runs for real against a fake connected client.
"""
import asyncio

import pytest


class _FakeSDKClient:
    """Stands in for ClaudeSDKClient and records disconnects."""

    def __init__(self, sid="s", model="m", effort="auto", service_tier=""):
        self.sid = sid
        self.model = model
        self.effort = effort
        self.service_tier = service_tier
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


class _FakeSessionStream:
    """Stable pump stand-in; pool tests exercise ownership, not SDK parsing."""

    def __init__(self, key, client):
        self.key = key
        self.client = client
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture()
def chat_mod(app_module):
    """The freshly-reloaded backend.chat, with all pool state cleared so a
    leftover entry from another test can't leak in."""
    from backend import chat as chat_mod
    from backend import chat_runtime
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._client_plan_return.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._session_streams.clear()
    chat_mod._session_runtime_locks.clear()
    chat_mod._pending_runtime_rebuilds.clear()
    chat_mod._sessions_with_inflight_tasks.clear()
    chat_runtime.SESSION_DISCONNECT_TASKS.clear()
    chat_runtime.SESSION_DISCONNECT_FAILED.clear()
    chat_runtime.SESSION_DISCONNECT_CLIENTS.clear()
    chat_runtime.CLIENT_DISCONNECT_OWNERS.clear()
    yield chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._client_plan_return.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._session_streams.clear()
    chat_mod._session_runtime_locks.clear()
    chat_mod._pending_runtime_rebuilds.clear()
    chat_mod._sessions_with_inflight_tasks.clear()
    chat_runtime.SESSION_DISCONNECT_TASKS.clear()
    chat_runtime.SESSION_DISCONNECT_FAILED.clear()
    chat_runtime.SESSION_DISCONNECT_CLIENTS.clear()
    chat_runtime.CLIENT_DISCONNECT_OWNERS.clear()


def _patch_builder(monkeypatch, chat_mod):
    """Replace the slow CLI-spawning path with a fake-client factory."""
    async def fake_build(
        session_id, model, permission, effort, service_tier="",
        plan_return_permission="",
    ):
        return _FakeSDKClient(session_id, model, effort, service_tier)

    def fake_ensure(key, client):
        stream = chat_mod._session_streams.get(key)
        if stream is None or stream.client is not client:
            stream = _FakeSessionStream(key, client)
            chat_mod._session_streams[key] = stream
        return stream

    monkeypatch.setattr(chat_mod, "_build_and_connect_client", fake_build)
    monkeypatch.setattr(chat_mod, "_ensure_session_stream", fake_ensure)


def test_cache_hit_reuses_same_client(chat_mod, monkeypatch):
    """Two get_client calls for the same (sid, model, effort) return the
    SAME object — no second subprocess spawn."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        c1 = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions")
        c2 = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions")
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1 is c2, "cache miss on identical key — pool not reusing client"
    # Exactly one entry in the pool + LRU.
    assert list(chat_mod._clients.keys()) == [
        ("sid-1", "claude-sonnet-4-6", "auto", "")]
    assert chat_mod._client_lru == [
        ("sid-1", "claude-sonnet-4-6", "auto", "")]


def test_different_key_builds_new_client(chat_mod, monkeypatch):
    """Switching model, effort, or service tier builds a distinct runtime."""
    _patch_builder(monkeypatch, chat_mod)
    monkeypatch.setattr(chat_mod, "_CLIENT_POOL_CAP", 8)

    async def run():
        a = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions")
        b = await chat_mod.get_client("sid-1", "claude-haiku-4-5", "bypassPermissions")
        c = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions", effort="high")
        d = await chat_mod.get_client(
            "sid-1", "claude-sonnet-4-6", "bypassPermissions",
            service_tier="fast")
        return a, b, c, d

    a, b, c, d = asyncio.run(run())
    assert len({id(a), id(b), id(c), id(d)}) == 4
    assert set(chat_mod._clients.keys()) == {
        ("sid-1", "claude-sonnet-4-6", "auto", ""),
        ("sid-1", "claude-haiku-4-5", "auto", ""),
        ("sid-1", "claude-sonnet-4-6", "high", ""),
        ("sid-1", "claude-sonnet-4-6", "auto", "fast"),
    }


def test_disconnect_client_evicts_entry_and_all_side_dicts(chat_mod, monkeypatch):
    """disconnect_client must remove the pool entry AND _client_permission,
    _creation_locks and _client_lru — leaving zero residue."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        c = await chat_mod.get_client("sid-evict", "claude-sonnet-4-6", "bypassPermissions")
        key = ("sid-evict", "claude-sonnet-4-6", "auto", "")
        # Ensure a creation lock got registered (get_client takes it on miss).
        assert key in chat_mod._creation_locks
        assert key in chat_mod._clients
        assert key in chat_mod._client_permission
        assert key in chat_mod._client_lru

        await chat_mod.disconnect_client("sid-evict")
        return c, key

    c, key = asyncio.run(run())
    assert c.disconnected is True, "evicted client never disconnected"
    assert key not in chat_mod._clients
    assert key not in chat_mod._client_permission
    assert key not in chat_mod._creation_locks
    assert key not in chat_mod._client_lru


def test_failed_disconnect_retains_client_and_retries(chat_mod, monkeypatch):
    """A failed SDK close keeps the exact owner and the next DELETE retries."""
    from backend import chat_runtime

    class FlakyClient(_FakeSDKClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.attempts = 0

        async def disconnect(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("first close failed")
            self.disconnected = True

    async def fake_build(
        session_id, model, permission, effort, service_tier="",
        plan_return_permission="",
    ):
        return FlakyClient(session_id, model, effort, service_tier)

    monkeypatch.setattr(chat_mod, "_build_and_connect_client", fake_build)
    monkeypatch.setattr(
        chat_mod,
        "_ensure_session_stream",
        lambda key, client: _FakeSessionStream(key, client),
    )

    async def run():
        client = await chat_mod.get_client(
            "sid-retry", "claude-sonnet-4-6", "bypassPermissions"
        )
        with pytest.raises(chat_mod.RuntimeCleanupTimeout):
            await chat_mod.disconnect_client("sid-retry")
        assert client.attempts == 1
        assert chat_runtime.SESSION_DISCONNECT_CLIENTS["sid-retry"] == {
            id(client): client
        }
        await chat_mod.disconnect_client("sid-retry")
        assert client.attempts == 2
        assert client.disconnected is True
        assert "sid-retry" not in chat_runtime.SESSION_DISCONNECT_CLIENTS
        assert "sid-retry" not in chat_runtime.SESSION_DISCONNECT_FAILED

    asyncio.run(run())


def test_cancelled_unpooled_cleanup_remains_joinable(chat_mod):
    """Cancelling a caller cannot orphan a connected, never-pooled client."""
    from backend import chat_runtime

    class SlowClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.disconnected = False

        async def disconnect(self):
            self.started.set()
            await self.release.wait()
            self.disconnected = True

    async def run():
        client = SlowClient()
        owner = asyncio.create_task(
            chat_mod._disconnect_unpooled_client(client, "sid-unpooled")
        )
        await client.started.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert id(client) in chat_runtime.SESSION_DISCONNECT_CLIENTS[
            "sid-unpooled"
        ]
        client.release.set()
        assert await chat_mod._join_session_disconnects(
            "sid-unpooled", timeout=1.0
        )
        assert client.disconnected is True
        assert "sid-unpooled" not in chat_runtime.SESSION_DISCONNECT_CLIENTS

    asyncio.run(run())


def test_permission_switch_rebuilds_runtime(chat_mod, monkeypatch):
    """Permission is launch-sensitive, so every mode change replaces the
    runtime rather than risking a stale or partially-switched client."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        key = ("sid-flip", "claude-sonnet-4-6", "auto", "")
        c1 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "bypassPermissions")
        c2 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "default")
        assert c2 is not c1
        assert c1.disconnected is True
        assert chat_mod._client_permission[key] == "default"
        c3 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "bypassPermissions")
        assert c3 is not c2
        assert c2.disconnected is True
        assert chat_mod._client_permission[key] == "bypassPermissions"

    asyncio.run(run())


def test_plan_return_capability_participates_in_runtime_contract(
    chat_mod, monkeypatch,
):
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        key = ("sid-plan", "claude-sonnet-4-6", "auto", "")
        first = await chat_mod.get_client(
            "sid-plan",
            "claude-sonnet-4-6",
            "plan",
            plan_return_permission="default",
        )
        reused = await chat_mod.get_client(
            "sid-plan",
            "claude-sonnet-4-6",
            "plan",
            plan_return_permission="default",
        )
        assert reused is first

        elevated = await chat_mod.get_client(
            "sid-plan",
            "claude-sonnet-4-6",
            "plan",
            plan_return_permission="bypassPermissions",
        )
        assert elevated is not first
        assert first.disconnected is True
        assert chat_mod._client_permission[key] == "plan"
        assert chat_mod._client_plan_return[key] == "bypassPermissions"

    asyncio.run(run())


def test_eviction_at_pool_cap_drops_oldest_and_its_side_dicts(chat_mod, monkeypatch):
    """When the LRU exceeds _CLIENT_POOL_CAP, the oldest non-streaming entry
    is evicted and removed from every side registry."""
    _patch_builder(monkeypatch, chat_mod)
    monkeypatch.setattr(chat_mod, "_CLIENT_POOL_CAP", 2)

    async def run():
        a = await chat_mod.get_client("A", "claude-sonnet-4-6", "bypassPermissions")
        b = await chat_mod.get_client("B", "claude-sonnet-4-6", "bypassPermissions")
        # Third miss exceeds cap=2 → oldest (A) evicted.
        c = await chat_mod.get_client("C", "claude-sonnet-4-6", "bypassPermissions")
        return a, b, c

    a, b, c = asyncio.run(run())
    key_a = ("A", "claude-sonnet-4-6", "auto", "")
    assert a.disconnected is True, "oldest entry not disconnected on eviction"
    assert key_a not in chat_mod._clients
    assert key_a not in chat_mod._client_permission
    assert key_a not in chat_mod._client_lru
    # B and C survive.
    assert ("B", "claude-sonnet-4-6", "auto", "") in chat_mod._clients
    assert ("C", "claude-sonnet-4-6", "auto", "") in chat_mod._clients


def test_lru_eviction_closes_and_drops_session_stream(chat_mod, monkeypatch):
    _patch_builder(monkeypatch, chat_mod)
    monkeypatch.setattr(chat_mod, "_CLIENT_POOL_CAP", 2)
    created = {}

    class FakeStream:
        def __init__(self, key, client):
            self.key = key
            self.client = client
            self.closed = False

        async def aclose(self):
            self.closed = True

    def fake_ensure(key, client):
        stream = FakeStream(key, client)
        created[key] = stream
        chat_mod._session_streams[key] = stream
        return stream

    monkeypatch.setattr(chat_mod, "_ensure_session_stream", fake_ensure)

    async def run():
        await chat_mod.get_client(
            "A", "claude-sonnet-4-6", "bypassPermissions")
        await chat_mod.get_client(
            "B", "claude-sonnet-4-6", "bypassPermissions")
        await chat_mod.get_client(
            "C", "claude-sonnet-4-6", "bypassPermissions")

    asyncio.run(run())

    key_a = ("A", "claude-sonnet-4-6", "auto", "")
    assert created[key_a].closed is True
    assert key_a not in chat_mod._session_streams


def test_eviction_skips_session_with_inflight_background_task(chat_mod, monkeypatch):
    """A client whose session has an in-flight SDK background task is PINNED:
    LRU eviction must skip it (disconnect() kills the CLI subprocess, which
    would abort the running task + the watcher draining its notification). The
    next-oldest evictable client is dropped instead."""
    _patch_builder(monkeypatch, chat_mod)
    monkeypatch.setattr(chat_mod, "_CLIENT_POOL_CAP", 2)

    async def run():
        a = await chat_mod.get_client("A", "claude-sonnet-4-6", "bypassPermissions")
        b = await chat_mod.get_client("B", "claude-sonnet-4-6", "bypassPermissions")
        # Pin the OLDEST (A) as if it has a background task still running.
        chat_mod._sessions_with_inflight_tasks["A"] = {"task_x"}
        # Third miss exceeds cap=2. Oldest is A but it's pinned → B evicted.
        c = await chat_mod.get_client("C", "claude-sonnet-4-6", "bypassPermissions")
        return a, b, c

    a, b, c = asyncio.run(run())
    key_a = ("A", "claude-sonnet-4-6", "auto", "")
    key_b = ("B", "claude-sonnet-4-6", "auto", "")
    # A survives despite being oldest — the pin protected it.
    assert a.disconnected is False, "pinned client was wrongly disconnected"
    assert key_a in chat_mod._clients
    # B (next-oldest, unpinned) took the eviction instead.
    assert b.disconnected is True, "non-pinned oldest not evicted"
    assert key_b not in chat_mod._clients
    assert ("C", "claude-sonnet-4-6", "auto", "") in chat_mod._clients
