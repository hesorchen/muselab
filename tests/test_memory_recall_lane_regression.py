"""Recall lane regressions: real actors, event-controlled I/O, no database/network.

The dense provider cannot finish until lexical is executing on its actor.
This closes the immediate-dense-mock ordering gap in test_memory_engine.py.
Run on the designated test host; the deadline case intentionally takes 5s.
"""
import asyncio
from contextlib import contextmanager
import threading
import time
from types import SimpleNamespace
import uuid

import pytest


@pytest.fixture
def lane_case(tmp_path, monkeypatch):
    from backend import memory_engine as module
    from backend.memory_config import MemoryConfig

    cfg = MemoryConfig.model_validate({
        "mode": "active",
        "generation_model": "fake-model",
        "embedding": {"base_url": "http://unused/v1", "model": "fake", "dimensions": 3},
        "vector": {"provider": "qdrant", "url": "http://unused:6333", "collection": "fake"},
        "retrieval": {"soft_timeout_ms": 5000, "dense_candidates": 60,
                      "lexical_candidates": 60, "final_limit": 15},
        "rerank": {"enabled": False},
    })
    case = SimpleNamespace(
        cfg=cfg, lexical_entered=threading.Event(), release=threading.Event(),
        lexical_exited=threading.Event(), hydrated=threading.Event(),
        dense_ready=asyncio.Event(), calls=[], budgets=[], stores=[],
        lexical_store=None, hydrate_stores=[], lexical_thread=None, hydrate_threads=[],
        hydrated_ids=[],
    )
    case.memories = {
        f"{channel}-{index}": {
            "id": f"{channel}-{index}", "kind": "preference", "status": "active",
            "authority": "confirmed", "confidence": 1.0,
            "content": f"{channel}-{index}:" + "完整正文不可截断。" * 600 + "正文尾标记",
        }
        for channel in ("dense", "lexical") for index in range(60)
    }

    class Store:
        # Every resolver gets its own fake store, just as with a real read lane.
        new_recall_id = staticmethod(lambda: uuid.uuid4().hex)

        def __init__(self, path, *, read_only=False):
            self.path = path
            self.read_only = read_only
            self.local = threading.local()
            case.stores.append(self)

        @contextmanager
        def read_budget(self, deadline, *, cancel_event):
            self.local.deadline = deadline
            self.local.cancel_event = cancel_event
            case.budgets.append((deadline, cancel_event))
            try:
                yield
            finally:
                del self.local.deadline
                del self.local.cancel_event

        def recent_evidence(self, *args, **kwargs):
            return []

        def lexical_candidates(self, owner_id, query, *, limit):
            case.calls.append(("lexical", limit))
            case.lexical_store = self
            case.lexical_thread = threading.get_ident()
            case.lexical_entered.set()
            try:
                if not case.release.is_set():
                    # Cancellation/deadline, not sleep duration, releases this
                    # synthetic running read. Eight seconds is only a leak guard.
                    cancelled = self.local.cancel_event.wait(8)
                    if cancelled:
                        raise TimeoutError("synthetic lexical read interrupted")
                    raise AssertionError("lexical read was neither cancelled nor released")
                return [{"id": f"lexical-{i}", "channel": "lexical"} for i in range(limit)]
            finally:
                case.lexical_exited.set()

        def memories_with_stats_by_ids(self, owner_id, ids):
            case.hydrated_ids.extend(ids)
            case.hydrate_stores.append(self)
            case.hydrate_threads.append(threading.get_ident())
            case.hydrated.set()
            return [dict(case.memories[item_id]) for item_id in ids]

    class Embedding:
        def __init__(self, config):
            pass

        async def embed(self, texts):
            return [[1.0, 0.0, 0.0]]

    class Vector:
        async def search(self, vector, *, owner_id, limit):
            # An explicit hand-off, not a sleep racing executor scheduling.
            assert await asyncio.to_thread(case.lexical_entered.wait, 2)
            case.calls.append(("dense", limit))
            case.dense_ready.set()
            return [{"id": f"dense-{i}", "channel": "dense"} for i in range(limit)]

    monkeypatch.setattr(module, "MemoryStore", Store)
    monkeypatch.setattr(module, "EmbeddingProvider", Embedding)
    monkeypatch.setattr(module, "vector_store", lambda config: Vector())
    monkeypatch.setattr(module, "perf_event", lambda *args, **kwargs: None)
    instance = module.MemoryEngine(Store(tmp_path / "never-created.sqlite3"))

    async def config():
        return cfg

    monkeypatch.setattr(instance, "_config_async", config)
    monkeypatch.setattr(instance, "_schedule_recall_telemetry", lambda **kwargs: None)
    case.instance = instance
    return case


async def _cancel_and_close(case, task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await case.instance.stop()


def _assert_complete(case, rows, session):
    assert len(rows) == case.cfg.retrieval.final_limit == 15
    for row in rows:
        assert row["content"] == case.memories[row["id"]]["content"]
        assert len(row["content"]) > 3000
        assert row["content"].endswith("正文尾标记")
    trace = case.instance.peek_recall_trace(session)
    assert trace["count"] == 15
    assert [item["content"] for item in trace["items"]] == [row["content"] for row in rows]
    return trace


@pytest.mark.parametrize("pinned", [False, True])
def test_recall_lanes_reconfigure_stop_and_restart(lane_case, monkeypatch, tmp_path, pinned):
    from backend import memory_engine as module

    instance = lane_case.instance if pinned else module.MemoryEngine()
    path = tmp_path / "first.sqlite3"
    monkeypatch.setattr(module, "database_path", lambda: path)
    monkeypatch.setenv("MUSELAB_MEMORY_WORKER_DISABLED", "1")

    async def config():
        return lane_case.cfg

    monkeypatch.setattr(instance, "_config_async", config)

    async def inspect():
        lanes = [await instance._recall_store_call(
            lambda store: (store, threading.get_ident()), stage=stage)
            for stage in ("recent", "lexical", "hydrate")]
        assert lanes[0] == lanes[2]
        assert lanes[0][0] is not lanes[1][0]
        assert lanes[0][1] != lanes[1][1]
        assert all(store.read_only for store, _ in lanes)
        expected = instance._store.path if pinned else path
        assert all(store.path == expected for store, _ in lanes)
        return lanes

    async def scenario():
        nonlocal path
        try:
            instance.start(config=lane_case.cfg)
            before = await inspect()
            executors = (instance._recall_store_actor._executor,
                         instance._lexical_store_actor._executor)
            path = tmp_path / "second.sqlite3"
            await instance.reconfigure()
            after = await inspect()
            assert executors == (instance._recall_store_actor._executor,
                                 instance._lexical_store_actor._executor)
            for old, new in zip(before, after):
                assert (old[0] is new[0]) == pinned
            await instance.stop()
            assert instance._recall_store_actor._executor is None
            assert instance._lexical_store_actor._executor is None
            assert not any(thread.ident in {row[1] for row in after}
                           for thread in threading.enumerate())
            for stage in ("recent", "lexical", "hydrate"):
                with pytest.raises(RuntimeError, match="closed"):
                    await instance._recall_store_call(lambda store: None, stage=stage)
            instance.start(config=lane_case.cfg)
            await inspect()
        finally:
            await instance.stop()

    asyncio.run(scenario())


def test_lexical_lane_stop_drains_accepted_reads(lane_case):
    instance = lane_case.instance
    entered, release = threading.Event(), threading.Event()
    finished = []

    def blocked(store):
        entered.set()
        assert release.wait(3)
        finished.append(True)

    async def scenario():
        read = asyncio.create_task(instance._recall_store_call(blocked, stage="lexical"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            stop = asyncio.create_task(instance.stop())
            await asyncio.sleep(0.02)
            assert not stop.done()
            release.set()
            await asyncio.wait_for(stop, 1)
            await read
            assert finished == [True]
            assert instance._lexical_store_actor._executor is None
        finally:
            release.set()
            await asyncio.gather(read, return_exceptions=True)
            await instance.stop()

    asyncio.run(scenario())


def test_running_lexical_does_not_block_dense_hydration(lane_case):
    """Old single-lane implementation fails while lexical still owns its thread."""
    case = lane_case

    async def scenario():
        task = asyncio.create_task(case.instance.recall("query", "independent-lanes"))
        try:
            await asyncio.wait_for(case.dense_ready.wait(), 3)
            assert not case.lexical_exited.is_set()
            assert await asyncio.to_thread(case.hydrated.wait, 1), (
                "dense results queued behind an already-running lexical read")
            assert not case.lexical_exited.is_set()
            assert case.hydrate_stores[0] is not case.lexical_store, (
                "independent lanes must not share mutable store/read-budget state")
            assert case.hydrate_threads[0] != case.lexical_thread
            assert sorted(case.calls) == [("dense", 60), ("lexical", 60)]
            assert len({deadline for deadline, _ in case.budgets}) == 1
        finally:
            await _cancel_and_close(case, task)

    asyncio.run(scenario())


def test_five_second_budget_keeps_healthy_dense_full_bodies(lane_case):
    """A late dense result must survive lexical consuming the entire 5s budget."""
    case = lane_case

    async def scenario():
        started = time.perf_counter()
        task = asyncio.create_task(case.instance.recall("query", "lexical-timeout"))
        try:
            rows = await asyncio.wait_for(asyncio.shield(task), 7)
            trace = _assert_complete(case, rows, "lexical-timeout")
            assert all(row["id"].startswith("dense-") for row in rows)
            assert set(case.hydrated_ids) == {f"dense-{i}" for i in range(60)}
            assert trace["status"] == "partial"
            assert trace["dense_status"] == trace["hydrate_status"] == "ok"
            assert trace["lexical_status"] == "timeout"
            assert sorted(case.calls) == [("dense", 60), ("lexical", 60)]
            assert case.cfg.retrieval.soft_timeout_ms == 5000
            deadlines = {deadline for deadline, _ in case.budgets}
            assert len(deadlines) == 1
            assert started + 4.9 <= deadlines.pop() <= started + 5.1
            assert trace["latency_ms"] >= 4900  # No hidden per-channel sub-budget.
            assert await asyncio.to_thread(case.lexical_exited.wait, 1)
        finally:
            await _cancel_and_close(case, task)

    asyncio.run(scenario())


def test_cancelled_recall_releases_read_and_next_request_recovers(lane_case):
    case = lane_case

    async def scenario():
        task = asyncio.create_task(case.instance.recall("query", "cancelled"))
        try:
            await asyncio.wait_for(case.dense_ready.wait(), 3)
            assert case.lexical_entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert case.instance.peek_recall_trace("cancelled")["status"] == "cancelled"
            assert await asyncio.to_thread(case.lexical_exited.wait, 1)
            assert all(cancel_event.is_set() for _, cancel_event in case.budgets)

            case.release.set()
            case.calls.clear()
            case.budgets.clear()
            case.hydrated_ids.clear()
            rows = await asyncio.wait_for(case.instance.recall("query", "recovered"), 3)
            trace = _assert_complete(case, rows, "recovered")
            assert set(case.hydrated_ids) == set(case.memories)
            assert len(case.hydrated_ids) == 120
            assert trace["status"] == "ok"
            assert all(trace[f"{stage}_status"] == "ok" for stage in
                       ("recent", "dense", "lexical", "hydrate"))
            assert sorted(case.calls) == [("dense", 60), ("lexical", 60)]
            assert all(cancel_event.is_set() for _, cancel_event in case.budgets)
        finally:
            await _cancel_and_close(case, task)

    asyncio.run(scenario())
