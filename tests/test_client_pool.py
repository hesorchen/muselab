"""Client-pool behavior for chat.get_client / disconnect_client.

These guard the long-lived state the rest of the chat surface depends on:
the (sid, model, effort) -> ClaudeSDKClient cache plus its four side dicts
(_client_permission / _bypass_state / _creation_locks / _client_lru).

Production code spawns a real CLI subprocess in _build_and_connect_client;
we monkeypatch THAT (not get_client) so the cache/LRU/eviction logic under
test runs for real against a fake connected client.
"""
import asyncio

import pytest


class _FakeSDKClient:
    """Stands in for ClaudeSDKClient. Records permission-mode flips and
    disconnects so tests can assert the pool drove them."""

    def __init__(self, sid="s", model="m", effort=""):
        self.sid = sid
        self.model = model
        self.effort = effort
        self.permission_modes: list[str] = []
        self.disconnected = False

    async def set_permission_mode(self, mode):
        self.permission_modes.append(mode)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture()
def chat_mod(app_module):
    """The freshly-reloaded backend.chat, with all pool state cleared so a
    leftover entry from another test can't leak in."""
    from backend import chat as chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._bypass_state.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._sessions_with_inflight_tasks.clear()
    yield chat_mod
    chat_mod._clients.clear()
    chat_mod._client_permission.clear()
    chat_mod._bypass_state.clear()
    chat_mod._creation_locks.clear()
    chat_mod._client_lru.clear()
    chat_mod._sessions_with_inflight_tasks.clear()


def _patch_builder(monkeypatch, chat_mod):
    """Replace the slow CLI-spawning path with a fake-client factory that
    ALSO registers the bypass_state dict the same way production does (so
    the get_client permission-flip path has something to flip)."""
    async def fake_build(session_id, model, permission, effort):
        key = (session_id, model, effort)
        chat_mod._bypass_state[key] = {
            "bypass": permission == "bypassPermissions"}
        return _FakeSDKClient(session_id, model, effort)

    monkeypatch.setattr(chat_mod, "_build_and_connect_client", fake_build)


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
    assert list(chat_mod._clients.keys()) == [("sid-1", "claude-sonnet-4-6", "")]
    assert chat_mod._client_lru == [("sid-1", "claude-sonnet-4-6", "")]


def test_different_key_builds_new_client(chat_mod, monkeypatch):
    """Switching model OR effort yields a distinct client (different key)."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        a = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions")
        b = await chat_mod.get_client("sid-1", "claude-haiku-4-5", "bypassPermissions")
        c = await chat_mod.get_client("sid-1", "claude-sonnet-4-6", "bypassPermissions", effort="high")
        return a, b, c

    a, b, c = asyncio.run(run())
    assert a is not b and a is not c and b is not c
    assert set(chat_mod._clients.keys()) == {
        ("sid-1", "claude-sonnet-4-6", ""),
        ("sid-1", "claude-haiku-4-5", ""),
        ("sid-1", "claude-sonnet-4-6", "high"),
    }


def test_disconnect_client_evicts_entry_and_all_side_dicts(chat_mod, monkeypatch):
    """disconnect_client must remove the pool entry AND _client_permission,
    _bypass_state, _creation_locks, _client_lru — leaving zero residue."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        c = await chat_mod.get_client("sid-evict", "claude-sonnet-4-6", "bypassPermissions")
        key = ("sid-evict", "claude-sonnet-4-6", "")
        # Ensure a creation lock got registered (get_client takes it on miss).
        assert key in chat_mod._creation_locks
        assert key in chat_mod._clients
        assert key in chat_mod._client_permission
        assert key in chat_mod._bypass_state
        assert key in chat_mod._client_lru

        await chat_mod.disconnect_client("sid-evict")
        return c, key

    c, key = asyncio.run(run())
    assert c.disconnected is True, "evicted client never disconnected"
    assert key not in chat_mod._clients
    assert key not in chat_mod._client_permission
    assert key not in chat_mod._bypass_state
    assert key not in chat_mod._creation_locks
    assert key not in chat_mod._client_lru


def test_permission_switch_flips_shared_bypass_flag_live(chat_mod, monkeypatch):
    """L244 regression: switching permission mode on a CACHED client via
    get_client must flip the SAME _bypass_state[key] dict the can_use_tool
    closure captured by reference — so a bypass→default switch starts
    actually prompting, and default→bypass stops prompting, WITHOUT a
    client rebuild."""
    _patch_builder(monkeypatch, chat_mod)

    async def run():
        key = ("sid-flip", "claude-sonnet-4-6", "")
        # First call: bypassPermissions → bypass flag True.
        c1 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "bypassPermissions")
        st = chat_mod._bypass_state[key]
        assert st["bypass"] is True

        # Switch to 'default' on the cached client. Same object back.
        c2 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "default")
        assert c2 is c1, "permission switch must NOT rebuild the client"
        # The closure's captured dict was mutated in place.
        assert st["bypass"] is False, "bypass flag not flipped to False — closure stale"
        assert chat_mod._client_permission[key] == "default"
        # SDK set_permission_mode was actually called with the new mode.
        assert c1.permission_modes == ["default"]

        # Switch back to bypass — flag flips live again.
        c3 = await chat_mod.get_client("sid-flip", "claude-sonnet-4-6", "bypassPermissions")
        assert c3 is c1
        assert st["bypass"] is True
        assert c1.permission_modes == ["default", "bypassPermissions"]
        return st

    asyncio.run(run())


def test_eviction_at_pool_cap_drops_oldest_and_its_side_dicts(chat_mod, monkeypatch):
    """When the LRU exceeds _CLIENT_POOL_CAP, the oldest non-streaming entry
    is evicted: removed from _clients/_client_permission/_bypass_state/
    _client_lru and disconnected."""
    _patch_builder(monkeypatch, chat_mod)
    monkeypatch.setattr(chat_mod, "_CLIENT_POOL_CAP", 2)

    async def run():
        a = await chat_mod.get_client("A", "claude-sonnet-4-6", "bypassPermissions")
        b = await chat_mod.get_client("B", "claude-sonnet-4-6", "bypassPermissions")
        # Third miss exceeds cap=2 → oldest (A) evicted.
        c = await chat_mod.get_client("C", "claude-sonnet-4-6", "bypassPermissions")
        return a, b, c

    a, b, c = asyncio.run(run())
    key_a = ("A", "claude-sonnet-4-6", "")
    assert a.disconnected is True, "oldest entry not disconnected on eviction"
    assert key_a not in chat_mod._clients
    assert key_a not in chat_mod._client_permission
    assert key_a not in chat_mod._bypass_state
    assert key_a not in chat_mod._client_lru
    # B and C survive.
    assert ("B", "claude-sonnet-4-6", "") in chat_mod._clients
    assert ("C", "claude-sonnet-4-6", "") in chat_mod._clients


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
    key_a = ("A", "claude-sonnet-4-6", "")
    key_b = ("B", "claude-sonnet-4-6", "")
    # A survives despite being oldest — the pin protected it.
    assert a.disconnected is False, "pinned client was wrongly disconnected"
    assert key_a in chat_mod._clients
    # B (next-oldest, unpinned) took the eviction instead.
    assert b.disconnected is True, "non-pinned oldest not evicted"
    assert key_b not in chat_mod._clients
    assert ("C", "claude-sonnet-4-6", "") in chat_mod._clients
