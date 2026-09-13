"""Status scales with metadata; cancellation releases the read lane."""
import asyncio
from contextlib import contextmanager
import sqlite3
import threading

import pytest

from backend.memory_config import MemoryConfig
from backend.memory_engine import MemoryEngine
from backend.memory_store import MemoryStore


def test_status_does_not_read_artifact_payloads_and_indexes_support_ordering(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    with sqlite3.connect(store.path) as db:
        db.executemany(
            "INSERT INTO artifacts (id,owner_id,kind,status,title,payload_json,source_episode_ids_json,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(f"a-{i}", "owner-a", "skill_candidate", "pending_review", "fixture",
              "x" * 262144, "[]", "fixture", i, i) for i in range(110)])
        db.execute("INSERT INTO artifacts (id,owner_id,kind,status,title,payload_json,source_episode_ids_json,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("other", "owner-b", "skill_candidate", "pending_review", "fixture",
             "{}", "[]", "fixture", 999, 999))
        db.executemany(
            "INSERT INTO jobs (id,owner_id,kind,status,payload_json,attempts,run_after,last_error,created_at,updated_at,operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(f"j-{i}", "owner-a", "consolidate_episode", "done", "{}", 1, 0, "", i, i, "") for i in range(5000)])
    engine = MemoryEngine(store)
    monkeypatch.setattr(engine, "config", lambda: MemoryConfig(owner_id="owner-a"))
    ui = engine._resolve_ui_store()
    connect = ui._connect
    queries = []
    @contextmanager
    def protected():
        with connect() as db:
            def authorize(action, table, column, *_rest):
                if action == sqlite3.SQLITE_READ and table == "artifacts" and column == "payload_json":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            db.set_trace_callback(queries.append)
            yield db
    monkeypatch.setattr(ui, "_connect", protected)
    async def run():
        try:
            result = await engine.status()
            assert result["pending_artifact_ids"] == [f"a-{i}" for i in range(109, 9, -1)]
            assert result["pending_artifacts"] == 110
            assert result["job_counts"] == {"done": 5000}
            assert result["recent_jobs"][0]["id"] == "j-4999"
        finally:
            await engine.stop()
    asyncio.run(run())
    with sqlite3.connect(store.path) as db:
        for sql in queries:
            if sql.lstrip().upper().startswith("SELECT") and "ORDER BY" in sql:
                plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
                assert not any("TEMP B-TREE" in row for row in plan), plan


def test_cancelled_sql_read_stops_before_next_status_request(tmp_path, monkeypatch):
    engine = MemoryEngine(MemoryStore(tmp_path / "memory.sqlite3"))
    monkeypatch.setattr(engine, "config", lambda: MemoryConfig(owner_id="fixture"))
    entered = threading.Event()
    def slow(store):
        with store._connect() as db:
            entered.set()
            return db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n").fetchone()
    async def run():
        try:
            caller = asyncio.create_task(engine._read_store_call(slow))
            assert await asyncio.to_thread(entered.wait, 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            result = await asyncio.wait_for(engine.status(), 2)
            assert result["pending_artifacts"] == 0
        finally:
            await engine.stop()
    asyncio.run(run())


def test_concurrent_status_reads_share_work_without_crossing_owners(tmp_path, monkeypatch):
    engine = MemoryEngine(MemoryStore(tmp_path / "memory.sqlite3"))
    owner = ["owner-a"]
    monkeypatch.setattr(engine, "config", lambda: MemoryConfig(owner_id=owner[0]))
    calls = []
    entered, release = threading.Event(), threading.Event()
    original = MemoryStore.stats
    def stats(self, owner_id):
        calls.append(owner_id)
        entered.set()
        assert release.wait(3)
        return original(self, owner_id)
    monkeypatch.setattr(MemoryStore, "stats", stats)
    async def run():
        try:
            a = [asyncio.create_task(engine.status()) for _ in range(10)]
            assert await asyncio.to_thread(entered.wait, 2)
            owner[0] = "owner-b"
            b = asyncio.create_task(engine.status())
            a[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await a[0]
            release.set()
            await asyncio.gather(*a[1:], b)
            assert calls == ["owner-a", "owner-b"]
        finally:
            release.set()
            await engine.stop()
    asyncio.run(run())
