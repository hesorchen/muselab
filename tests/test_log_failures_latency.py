"""Regressions for startup failures and slow storage in supplied diagnostics."""
import asyncio

import pytest


@pytest.mark.parametrize("via_symlink", [False, True])
def test_ducc_memory_generation_uses_its_own_workspace(app_module, tmp_path, monkeypatch, via_symlink):
    import claude_agent_sdk
    from claude_agent_sdk.types import ResultMessage
    from backend import settings
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationProvider

    memory_root = tmp_path / "memory-fixture"
    configured = memory_root
    if via_symlink:
        memory_root.mkdir()
        configured = tmp_path / "memory-alias"
        configured.symlink_to(memory_root, target_is_directory=True)
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(configured))
    monkeypatch.setenv("PWD", "/")
    executable = tmp_path / "ducc-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setattr(settings, "locate_ducc_executable", lambda: str(executable))
    seen = []

    async def query(*, prompt, options):
        seen.append(options)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="fixture",
                            result='{"memories": []}')

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    provider = GenerationProvider(MemoryConfig(generation_model="ducc:deepseek-v4-pro"))
    assert asyncio.run(provider.complete("fixture-system", "fixture-prompt")) == '{"memories": []}'
    options = seen[0]
    assert options.cwd == (memory_root / "generator").resolve()
    assert options.env["MUSELAB_DUCC_WORKSPACE"] == str(options.cwd)
    assert "PWD" not in options.env
    assert options.tools == []
    assert options.setting_sources == []
    assert options.skills == []
    assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "3000"


def test_generation_setup_failure_identifies_phase_without_private_detail(tmp_path, monkeypatch, capsys):
    import json
    import pytest
    import claude_agent_sdk
    from backend import observability
    from backend.memory_config import MemoryConfig
    from backend.memory_providers import GenerationError, GenerationProvider

    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setattr(observability, "_perf_writer", None)
    provider = GenerationProvider(MemoryConfig(generation_model="claude-sonnet-4-6"))
    monkeypatch.setattr(provider, "_route", lambda: None)

    def broken_options(**kwargs):
        raise TypeError("fixture-private-argument-detail")

    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", broken_options)
    with pytest.raises(GenerationError):
        asyncio.run(provider.complete("fixture-private-system", "fixture-private-prompt"))
    output = capsys.readouterr().err
    event = next(json.loads(line.split("[perf] ")[1]) for line in output.splitlines()
                 if '"event":"memory.generation"' in line)
    assert event["cause_kind"] == "TypeError"
    assert event["phase"] == "sdk_options"
    assert "first_event_ms" not in event
    assert "fixture-private" not in output


def test_unchanged_workspace_reconcile_does_not_wait_for_another_writer(app_module, temp_root):
    from concurrent.futures import ThreadPoolExecutor
    from backend.workspace_store import WorkspaceStore, compact_scan_rows, scan_workspace

    store = WorkspaceStore(temp_root)
    store.reconcile("fixture-workspace", temp_root, "fixture")
    cursor = store.current_cursor("fixture-workspace")
    snapshot = compact_scan_rows(scan_workspace(temp_root))
    before = store.bootstrap("fixture-workspace")
    with store._connect() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute("BEGIN IMMEDIATE")
        future = pool.submit(store.apply_reconcile_snapshot,
                             "fixture-workspace", temp_root, "fixture", snapshot, {},
                             expected_cursor=cursor)
        try:
            result = future.result(timeout=1.0)
            assert result == {"cursor": cursor, "changes": [], "resync": False}
        finally:
            writer.rollback()
    assert store.bootstrap("fixture-workspace") == before


def test_session_list_composition_uses_registered_paths_without_disk_resolution(app_module, monkeypatch):
    from backend import sessions

    rows = [{"id": f"fixture-{n}", "cwd": str(sessions.ROOT), "updated_at": n}
            for n in range(200)]

    def disk_resolution(value):
        raise AssertionError("session list repeated filesystem resolution")

    monkeypatch.setattr(sessions.workspace_registry, "contains", disk_resolution)
    sessions._apply_index_snapshot(rows)
    with sessions._LIST_CACHE_LOCK:
        result = list(sessions._LIST_CACHE["data"])
    assert [row["id"] for row in result] == [f"fixture-{n}" for n in reversed(range(200))]


def test_lexical_top_k_keeps_bm25_results_without_external_sort(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from backend.memory_store import MemoryStore

    store = MemoryStore(tmp_path / "fixture.sqlite3")
    for n in range(32):
        store.create_memory("fixture-a" if n % 5 else "fixture-b", "fact",
                            "needle " + "hay " * (n + 1),
                            status="active" if n % 7 else "deleted")
    with store._connect() as conn:
        expected = conn.execute(
            "SELECT m.id, bm25(memory_fts) AS score FROM memory_fts "
            "JOIN memories m ON m.id=memory_fts.memory_id "
            "WHERE memory_fts MATCH ? AND m.owner_id=? AND m.status=? "
            "ORDER BY score LIMIT ?", ('"needle"', "fixture-a", "active", 7)).fetchall()
    statements = []
    original = store._connect

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(store, "_connect", traced)
    actual = store.lexical_candidates("fixture-a", "needle", limit=7)
    assert [row["id"] for row in actual] == [row["id"] for row in expected]
    assert [row["score"] for row in actual] == [1 / (1 + abs(row["score"])) for row in expected]
    query = next(sql for sql in statements if "memory_fts MATCH" in sql)
    with original() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert not any("USE TEMP B-TREE FOR ORDER BY" in row["detail"] for row in plan)


def test_catalog_repeated_outage_backs_off_and_recovers(app_module, monkeypatch):
    import httpx
    from backend import chat

    requests = []
    broken = True
    real_client = httpx.AsyncClient
    route = {"ANTHROPIC_BASE_URL": "https://gateway.fixture", "ANTHROPIC_API_KEY": "fixture-key"}

    async def handler(request):
        requests.append(request)
        if broken:
            raise httpx.ReadTimeout("fixture", request=request)
        return httpx.Response(200, json={"models": [{"slug": "fixture-model", "context_window": 256000}]})

    monkeypatch.setattr(chat.endpoints, "routing_env", lambda _: dict(route))
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    model = "codex:fixture-model"
    key = chat._context_capability_key(route["ANTHROPIC_BASE_URL"], model, route["ANTHROPIC_API_KEY"])
    route_key = chat._context_route_key(key)
    base_ttl = chat._CONTEXT_CAPABILITY_FAILURE_TTL

    async def scenario():
        nonlocal broken
        assert await chat._detect_gateway_context_capability(model) is None
        chat._CONTEXT_CATALOG_FAILURES[route_key] -= base_ttl + 1
        chat._CONTEXT_CAPABILITY_FAILURES[key] -= base_ttl + 1
        assert await chat._detect_gateway_context_capability(model) is None
        assert len(requests) == 2
        chat._CONTEXT_CATALOG_FAILURES[route_key] -= base_ttl + 1
        chat._CONTEXT_CAPABILITY_FAILURES[key] -= base_ttl + 1
        assert await chat._detect_gateway_context_capability(model) is None
        assert len(requests) == 2  # A second outage gets a longer cooldown.
        broken = False
        chat._CONTEXT_CATALOG_FAILURES[route_key] -= 10000
        chat._CONTEXT_CAPABILITY_FAILURES[key] -= 10000
        result = await chat._detect_gateway_context_capability(model)
        assert result["context_limit"] == 243200
        assert len(requests) == 3
        assert route_key not in chat._CONTEXT_CATALOG_FAILURES
        assert route_key not in chat._CONTEXT_CATALOG_FAILURE_COUNTS

    asyncio.run(scenario())


def test_unchanged_scan_cancellation_does_not_publish_success(app_module, temp_root, monkeypatch):
    import threading
    from backend.workspace_store import WorkspaceScanCancelled, WorkspaceStore, compact_scan_rows, scan_workspace

    store = WorkspaceStore(temp_root)
    store.reconcile("fixture-workspace", temp_root, "fixture")
    cursor = store.current_cursor("fixture-workspace")
    snapshot = compact_scan_rows(scan_workspace(temp_root))
    cancelled = threading.Event()
    original = store._file_rows

    def cancelling_read(*args, **kwargs):
        rows = original(*args, **kwargs)
        cancelled.set()
        return rows

    monkeypatch.setattr(store, "_file_rows", cancelling_read)
    with pytest.raises(WorkspaceScanCancelled):
        store.apply_reconcile_snapshot("fixture-workspace", temp_root, "fixture", snapshot, {},
                                       expected_cursor=cursor, cancel_event=cancelled)


def test_session_list_legacy_paths_are_checked_once_and_removed_workspaces_stay_hidden(
        app_module, tmp_path, monkeypatch):
    from backend import sessions

    alias = tmp_path / "workspace-alias"
    alias.symlink_to(sessions.ROOT, target_is_directory=True)
    removed = str(tmp_path / "removed-workspace")
    rows = [{"id": f"fixture-{n}", "cwd": str(alias), "updated_at": n}
            for n in range(30)]
    rows.extend({"id": f"removed-{n}", "cwd": removed} for n in range(10))
    calls = []
    original = sessions.workspace_registry.contains

    def tracked(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(sessions.workspace_registry, "contains", tracked)
    sessions._apply_index_snapshot(rows)
    with sessions._LIST_CACHE_LOCK:
        result = list(sessions._LIST_CACHE["data"])
    assert len(result) == 30
    assert all(row["id"].startswith("fixture-") for row in result)
    assert calls == [str(alias), removed]
