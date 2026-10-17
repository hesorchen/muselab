"""Agent-autonomous memory: tools, evidence traceback, free editing, recall modes."""
import asyncio
import json
import uuid

import pytest

from tests.test_memory_engine import _config


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def agent_case(app_module, tmp_path, monkeypatch):
    from backend import memory_agent
    from backend import memory_engine as module

    instance = module.MemoryEngine(module.MemoryStore(tmp_path / "registry.sqlite3"))
    cfg = _config()
    cfg.retrieval.soft_timeout_ms = 2000
    monkeypatch.setattr(instance, "config", lambda: cfg)
    monkeypatch.setattr(module, "engine", instance)
    monkeypatch.setattr(memory_agent, "_tool_state", {})

    class Embedding:
        def __init__(self, config):
            pass

        async def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    class Vector:
        def __init__(self):
            self.hits: list[str] = []
            self.deleted: list[str] = []

        async def search(self, *args, **kwargs):
            return [{"id": memory_id, "channel": "dense"} for memory_id in self.hits]

        async def delete(self, memory_id):
            self.deleted.append(memory_id)

        async def upsert(self, *args, **kwargs):
            return None

    vector = Vector()
    monkeypatch.setattr(module, "EmbeddingProvider", Embedding)
    monkeypatch.setattr(module, "vector_store", lambda config: vector)
    return instance, cfg, vector


def _audit_actions(store, target_id):
    with store._lock, store._connect() as conn:
        return [row["action"] for row in conn.execute(
            "SELECT action FROM audit WHERE target_id=? ORDER BY rowid",
            (target_id,))]


def test_turn_evidence_resolves_to_exact_message(tmp_path):
    from backend.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "registry.sqlite3")
    session, message = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = store.add_evidence(
        "default", session, "assistant", "答复", source_ref=f"{session}:{message}:assistant")
    tool = store.add_evidence(
        "default", session, "tool", "out", event_type="tool_result",
        source_ref="toolu_123")
    memory = store.create_memory("default", "fact", "x", sources=[
        {"source_type": "evidence", "source_id": tool},
        {"source_type": "evidence", "source_id": evidence},
    ])
    sites = store.memory_traceback("default", memory["id"])
    assert sites[0]["can_jump_to_message"] is True
    assert sites[0]["message_id"] == message
    assert MemoryStore._evidence_message_id(session, "other:" + message + ":user") is None
    assert MemoryStore._evidence_message_id(session, f"{session}:not-a-uuid:user") is None

    by_session = store.create_memory("default", "fact", "y", sources=[
        {"source_type": "session", "source_id": session, "relation": "written_during"}])
    assert store.memory_traceback("default", by_session["id"])[0]["session_id"] == session


def test_evidence_context_is_owner_and_session_scoped(tmp_path):
    from backend.memory_store import MemoryStore
    store = MemoryStore(tmp_path / "registry.sqlite3")
    ids = [store.add_evidence("default", "s1", "user", f"m{i}", created_at=100 + i)
           for i in range(5)]
    store.add_evidence("default", "s2", "user", "other session", created_at=102)
    store.add_evidence("someone", "s1", "user", "other owner", created_at=102)
    found = store.evidence_context("default", ids[2], before=1, after=5)
    assert [row["content"] for row in found["before"]] == ["m1"]
    assert found["target"]["content"] == "m2"
    assert [row["content"] for row in found["after"]] == ["m3", "m4"]
    assert store.evidence_context("someone", ids[2]) is None
    near = store.evidence_near("default", "s1", 102.5, before=1, after=1)
    assert [row["content"] for row in near] == ["m2", "m3"]


def test_agent_search_does_not_replace_hook_trace(agent_case):
    from backend import memory_agent
    instance, cfg, vector = agent_case
    pref = instance.store.create_memory(
        "default", "preference", "prefers concise answers", authority="confirmed")
    fact = instance.store.create_memory("default", "fact", "project uses sqlite")
    vector.hits = [pref["id"], fact["id"]]

    async def scenario():
        await instance.recall("hook query", "sid")
        hook_trace = instance.peek_recall_trace("sid")
        found = await memory_agent.search("sid", "answers", kind="preference")
        assert instance.peek_recall_trace("sid") is hook_trace
        return found

    found = _run(scenario())
    assert [item["id"] for item in found["results"]] == [pref["id"]]
    assert found["recall_id"]
    assert found["results"][0]["sources"] == []
    assert "untrusted" in found["note"]


def test_agent_write_dedup_update_delete_and_audit(agent_case):
    from backend import memory_agent
    instance, cfg, vector = agent_case
    session = str(uuid.uuid4())
    confirmed = instance.store.create_memory(
        "default", "fact", "deploy port is 8765", authority="confirmed", confidence=1.0)

    created = _run(memory_agent.write(
        session, "  user  prefers dark   mode ", kind="preference", tags=["ui"]))
    assert created["action"] == "created"
    memory = instance.store.memory(created["memory"]["id"])
    assert memory["content"] == "user prefers dark mode"
    assert memory["authority"] == "inferred" and memory["status"] == "active"
    assert memory["attributes"]["written_by"] == "agent"
    assert {"source_type": "session", "source_id": session,
            "relation": "written_during"} in memory["sources"]
    assert "agent_write" in _audit_actions(instance.store, memory["id"])

    again = _run(memory_agent.write(session, "user prefers dark mode"))
    assert again["action"] == "noop"
    assert again["memory"]["id"] == memory["id"]

    # The Agent may freely rewrite even confirmed memories.
    updated = _run(memory_agent.update(
        session, confirmed["id"], content="deploy port is 8766", reason="moved"))
    assert updated["action"] == "updated"
    new = instance.store.memory(updated["memory"]["id"])
    assert new["authority"] == "inferred"
    assert new["attributes"]["edited_by"] == "agent"
    assert new["attributes"]["previous_authority"] == "confirmed"
    assert instance.store.memory(confirmed["id"])["status"] == "superseded"
    assert confirmed["id"] in vector.deleted
    assert "agent_update" in _audit_actions(instance.store, new["id"])
    with pytest.raises(memory_agent.MemoryToolError):
        _run(memory_agent.update(session, confirmed["id"], content="z", reason="r"))

    removed = _run(memory_agent.delete(session, new["id"], reason="obsolete"))
    assert removed["action"] == "deleted"
    assert instance.store.memory(new["id"])["status"] == "deleted"
    assert "agent_delete" in _audit_actions(instance.store, new["id"])
    assert _run(memory_agent.delete(session, new["id"], reason="x"))["action"] == "noop"


def test_get_evidence_traces_memory_to_original_messages(agent_case):
    from backend import memory_agent
    instance, cfg, _ = agent_case
    session, message = str(uuid.uuid4()), str(uuid.uuid4())
    user = instance.store.add_evidence(
        "default", session, "user", "以后回答都用中文", created_at=100,
        source_ref=f"{session}:{message}:user")
    long_text = "a" * 1200
    reply = instance.store.add_evidence(
        "default", session, "assistant", long_text, created_at=101,
        source_ref=f"{session}:{message}:assistant")
    memory = instance.store.create_memory("default", "preference", "用户偏好中文回答",
                                          sources=[{"source_type": "evidence",
                                                    "source_id": user}])
    result = _run(memory_agent.get_evidence(memory["id"], max_chars=500))
    assert result["type"] == "memory"
    assert [row["id"] for row in result["evidence"]] == [user, reply]
    assert result["evidence"][0]["message_id"] == message
    assert result["evidence"][1]["next_offset"] == 500
    assert result["sites"][0]["can_jump_to_message"] is True

    page = _run(memory_agent.get_evidence(reply, offset=1000, max_chars=500, context=0))
    assert page["evidence"]["content"] == "a" * 200
    assert "next_offset" not in page["evidence"]

    with pytest.raises(memory_agent.MemoryToolError):
        _run(memory_agent.get_evidence("mem_missing"))


def test_tools_refuse_when_memory_not_active(agent_case):
    from backend import memory_agent
    _, cfg, _ = agent_case
    cfg.mode = "shadow"
    text, is_error = _run(memory_agent.run_tool_safely(
        "memory_search", {"query": "x"}, session_id="s"))
    assert is_error and "not active" in json.loads(text)["error"]
    assert memory_agent.session_server("s") is None
    assert memory_agent.tools_attached("s") is False


def test_sdk_server_exposes_all_tools(agent_case):
    from backend import memory_agent
    from backend.memory_tool_specs import QUALIFIED_TOOL_NAMES, SERVER_NAME
    server = memory_agent.session_server("s")
    assert server["type"] == "sdk" and server["name"] == SERVER_NAME
    assert memory_agent.tools_attached("s") is True
    assert QUALIFIED_TOOL_NAMES == [
        f"mcp__{SERVER_NAME}__{name}" for name in (
            "memory_search", "memory_get_evidence", "memory_write",
            "memory_update", "memory_delete")]
    memory_agent.detach_session("s")
    assert memory_agent.tools_attached("s") is False


@pytest.mark.parametrize("mode", ["inject", "hint", "tool-only"])
def test_recall_modes_and_inject_fallback(agent_case, monkeypatch, mode):
    from backend import memory_agent, memory_client
    instance, cfg, vector = agent_case
    cfg.retrieval.recall_mode = mode
    memory = instance.store.create_memory(
        "default", "fact", "release train leaves every Thursday " + "x" * 300)
    vector.hits = [memory["id"]]
    monkeypatch.setattr(memory_client, "native_enabled", lambda: True)

    # Without attached tools every mode falls back to full injection.
    block = _run(memory_client.search_context("release", "sid"))
    assert '{"facts":' in block

    memory_agent._set_tool_state("sid", True)
    block = _run(memory_client.search_context("release", "sid"))
    trace = instance.peek_recall_trace("sid")
    if mode == "inject":
        assert '{"facts":' in block
    elif mode == "hint":
        assert "<memory_hint" in block and memory["id"] in block
        assert '"truncated":true' in block and "memory_search" in block
        assert trace["recall_mode"] == "hint" and trace["count"] == 1
    else:
        assert "<memory_hint" in block and memory["id"] not in block
        assert trace is None


def test_agent_tool_http_endpoint(client, auth):
    assert client.post("/api/memory/agent/tools/memory_search",
                       json={"args": {"query": "x"}}).status_code == 401
    assert client.post("/api/memory/agent/tools/nope", headers=auth,
                       json={}).status_code == 404
    specs = client.get("/api/memory/agent/tools", headers=auth).json()
    assert set(specs["tools"]) >= {"memory_search", "memory_write"}
    response = client.post("/api/memory/agent/tools/memory_search", headers=auth,
                           json={"args": {"query": "x"}, "client": "cli"})
    assert response.status_code == 200
    body = response.json()
    assert body["is_error"] is True and "not active" in body["text"]


def test_stdio_bridge_lists_shared_specs():
    import importlib.util
    from pathlib import Path

    from backend.memory_tool_specs import TOOL_SPECS
    path = Path(__file__).resolve().parent.parent / "scripts" / "muselab_memory_mcp.py"
    spec = importlib.util.spec_from_file_location("muselab_memory_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.build_server()

    import mcp.types as types
    handler = server.request_handlers[types.ListToolsRequest]
    result = _run(handler(types.ListToolsRequest(method="tools/list")))
    assert [tool.name for tool in result.root.tools] == list(TOOL_SPECS)


def test_chat_client_attaches_memory_tools_only_when_active(
        app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints, memory_agent
    from backend import memory_engine as module
    from backend.memory_tool_specs import QUALIFIED_TOOL_NAMES, SERVER_NAME
    from tests.test_chat_options import _capture_build_options

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    monkeypatch.setattr(memory_agent, "_tool_state", {})
    cfg = _config("active")
    monkeypatch.setattr(module.engine, "config", lambda: cfg)

    captured = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-memory-tools", "deepseek-v4-pro", "default", ""))
    assert captured["mcp_servers"][SERVER_NAME]["type"] == "sdk"
    assert set(QUALIFIED_TOOL_NAMES) <= set(captured["allowed_tools"])
    assert memory_agent.tools_attached("sid-memory-tools")

    cfg.mode = "shadow"
    captured = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-memory-tools", "deepseek-v4-pro", "default", ""))
    assert SERVER_NAME not in captured["mcp_servers"]
    assert not memory_agent.tools_attached("sid-memory-tools")
