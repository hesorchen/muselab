"""Synthetic regressions for slow storage, concurrent readers and format failures."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import threading

import pytest


def test_memory_status_config_stat_does_not_block_loop(tmp_path, monkeypatch):
    from backend import memory_config
    from backend.memory_engine import MemoryEngine
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path))
    memory_config.save_config(memory_config.MemoryConfig())
    entered, release = threading.Event(), threading.Event()
    original = Path.stat
    loop_thread = threading.get_ident()
    worker_threads = []

    def slow_stat(path, *args, **kwargs):
        if path == tmp_path / "config.json":
            worker_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(3), "config stat blocked the event loop"
        return original(path, *args, **kwargs)

    instance = MemoryEngine()
    async def read(*args, **kwargs):
        return {}
    monkeypatch.setattr(instance, "_read_store_call", read)
    monkeypatch.setattr(Path, "stat", slow_stat)

    async def scenario():
        pending = asyncio.create_task(instance.status())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # This coroutine must run while stat is still blocked.
            assert not pending.done()
            release.set()
            assert (await pending)["enabled"] is False
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await instance.stop()
    asyncio.run(scenario())
    assert worker_threads and all(t != loop_thread for t in worker_threads)


def test_memory_config_cache_includes_location(tmp_path, monkeypatch):
    from backend import memory_config as config
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        path = folder / "config.json"
        path.write_text(config.MemoryConfig(owner_id=name).model_dump_json())
        os.utime(path, ns=(123000000000, 123000000000))
    for name in ("first", "second", "first"):
        monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(tmp_path / name))
        assert config.load_config().owner_id == name


def test_hydration_uses_one_connection_and_filters_owner(tmp_path, monkeypatch):
    from backend.memory_store import MemoryStore
    writer = MemoryStore(tmp_path / "registry.sqlite3")
    one = writer.create_memory("owner-a", "fact", "synthetic first", sources=[
        {"source_type": "message", "source_id": "synthetic", "relation": "confirmed_from"}])
    two = writer.create_memory("owner-b", "fact", "synthetic second")
    reader = MemoryStore(writer.path, read_only=True)
    connections = []
    original = reader._connect
    @contextmanager
    def tracked():
        connections.append(True)
        with original() as connection:
            yield connection
    monkeypatch.setattr(reader, "_connect", tracked)
    rows = reader.memories_with_stats_by_ids("owner-a", [two["id"], one["id"], one["id"], "missing"])
    assert len(connections) == 1
    assert [row["id"] for row in rows] == [one["id"]]
    assert rows[0]["sources"] == [{"source_type": "message", "source_id": "synthetic", "relation": "confirmed_from"}]
    assert rows[0]["recall_stats"]["recall_count"] == 0


def test_active_reads_share_disk_work_but_merge_on_loop(monkeypatch, app_module):
    from backend import chat
    entered, release = threading.Event(), threading.Event()
    calls = []
    loop_thread = threading.get_ident()
    def snapshot(sids):
        calls.append(sids)
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return {"synthetic": ["synthetic"]}, {"synthetic": {"task"}}, {"synthetic": (frozenset(), "revision")}
    def merge(sid, **kwargs):
        assert threading.get_ident() == loop_thread
        assert kwargs["durable_runtime_task_ids"] == {"task"}
        return {"active": True}
    monkeypatch.setattr(chat, "_runtime_reconcile_snapshot", snapshot)
    monkeypatch.setattr(chat, "_session_active_status", merge)
    async def scenario():
        requests = [asyncio.create_task(chat.session_active_status("synthetic")) for _ in range(8)]
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            requests[0].cancel()
            await asyncio.gather(requests[0], return_exceptions=True)
            release.set()
            assert all(row["active"] for row in await asyncio.gather(*requests[1:]))
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
    asyncio.run(scenario())
    assert calls == [("synthetic",)]


def test_dashboard_concurrent_refresh_and_fresh_snapshot(monkeypatch, app_module):
    from backend import chat
    chat._dashboard_snapshots.clear()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def compute(*args):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return {"synthetic": True}
    monkeypatch.setattr(chat, "_compute_cost_dashboard", compute)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(chat.cost_dashboard, 30, 0) for _ in range(8)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        assert all(f.result() == {"synthetic": True} for f in futures)
    assert len(calls) == 1
    monkeypatch.setattr(chat, "_compute_cost_dashboard", lambda *a: pytest.fail("fresh cache touched disk"))
    assert chat.cost_dashboard(30, 0) == {"synthetic": True}
    chat._dashboard_snapshots.clear()


def test_dashboard_only_reparses_changed_file_and_cost(tmp_path, monkeypatch, app_module):
    from backend import chat
    root = tmp_path / "workspace"
    root.mkdir()
    projects = tmp_path / "projects"
    project = projects / chat._cli_encode_cwd(str(root))
    project.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    monkeypatch.setattr(chat.sess, "SESS_DIR", sessions)
    monkeypatch.setattr(chat, "_cli_project_roots", lambda: [projects])
    monkeypatch.setattr(chat.workspace_registry, "paths", lambda: [root])
    monkeypatch.setattr(chat, "_DASHBOARD_FRESH_SECONDS", 0)
    for cache in (chat._dashboard_cache, chat._dashboard_snapshots, chat._dashboard_file_cache, chat._dashboard_cost_cache):
        cache.clear()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    def write(name, value):
        (project / (name + ".jsonl")).write_text(json.dumps({
            "type": "assistant", "uuid": name, "timestamp": now,
            "message": {"model": "synthetic-model", "usage": {"input_tokens": value}, "stop_reason": "end_turn"}}) + "\n")
    write("first", 10)
    write("second", 20)
    reads = []
    original = Path.open
    def opened(path, *args, **kwargs):
        if path.suffix == ".jsonl" and (not args or args[0] == "r"):
            reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    assert chat.cost_dashboard(30, 0)["all_time"]["input_tokens"] == 30
    assert sorted(reads) == ["first.jsonl", "second.jsonl"]
    reads.clear()
    assert chat.cost_dashboard(30, 0)["all_time"]["input_tokens"] == 30
    assert reads == []
    # Equal-length atomic replacements must be visible even with preserved mtime.
    stamp = (project / "first.jsonl").stat().st_mtime_ns
    replacement = project / "replacement.tmp"
    replacement.write_text((project / "first.jsonl").read_text().replace('"input_tokens": 10', '"input_tokens": 11'))
    os.utime(replacement, ns=(stamp, stamp))
    replacement.replace(project / "first.jsonl")
    reads.clear()
    assert chat.cost_dashboard(30, 0)["all_time"]["input_tokens"] == 31
    assert reads == ["first.jsonl"]
    reads.clear()
    (sessions / "second.sidecar.json").write_text(json.dumps({"messages": {"second": {"cost": "$1.50"}}}))
    assert chat.cost_dashboard(30, 0)["all_time"]["cost"] == 1.5
    assert reads == ["second.jsonl"]
    (project / "first.jsonl").unlink()
    assert chat.cost_dashboard(30, 0)["all_time"]["input_tokens"] == 20


@pytest.mark.parametrize("text", [
    '{"memories": []}', '```JSON\n{"memories": []}\n```',
    'Result:\n{"memories": []}\nComplete.',
])
def test_generation_accepts_one_unambiguous_object(text):
    from backend.memory_providers import GenerationProvider
    assert GenerationProvider._json_value(text) == {"memories": []}


@pytest.mark.parametrize("text", [
    '{"broken": {"nested": true}', '{"first": 1} {"second": 2}',
    '```json\n{}\n```\n```json\n{}\n```',
    '[{"nested": true}] trailing',
])
def test_generation_does_not_salvage_ambiguous_or_truncated_objects(text):
    from backend.memory_providers import GenerationProvider
    with pytest.raises(ValueError):
        GenerationProvider._json_value(text)


def test_consuming_recall_receipt_never_reads_configuration(monkeypatch):
    from backend import memory_client as client
    from backend.memory_engine import engine
    monkeypatch.setattr(client, "native_enabled", lambda: pytest.fail("receipt read touched config"))
    monkeypatch.setattr(engine, "pop_recall_trace", lambda sid: {"id": "synthetic-native"})
    assert client.pop_recall_trace("synthetic") == {"id": "synthetic-native"}


def test_active_attachment_metadata_does_not_block_loop(monkeypatch, app_module):
    from backend import chat
    entered, release = threading.Event(), threading.Event()
    owner = chat.TurnBroadcast("synthetic-attachment")
    owner.staged_attachment_ids = ["synthetic"]
    monkeypatch.setitem(chat._active_turns, "synthetic-attachment", owner)
    monkeypatch.setattr(chat, "_runtime_reconcile_snapshot", lambda sids: ({}, {}, {}))
    def resolve(ids):
        entered.set()
        assert release.wait(3)
        return [], [{"kind": "pdf", "available": True}]
    def merge(sid, **kwargs):
        assert kwargs["hydrate_attachments"] is False
        return {"user_docs": owner.user_docs}
    monkeypatch.setattr(chat, "_resolve_staged_attachment_display", resolve)
    monkeypatch.setattr(chat, "_session_active_status", merge)
    async def scenario():
        task = asyncio.create_task(chat.session_active_status("synthetic-attachment"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not task.done()
            release.set()
            assert (await task)["user_docs"] == [{"kind": "pdf", "available": True}]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
