"""Actor shutdown drains real SQLite writes before any caller can finish."""

import asyncio
import sqlite3
import threading

import pytest

from backend.memory_engine import _MemoryStoreActor
from backend.memory_store import MemoryStore


@pytest.mark.parametrize("cancellations", [0, 1, 2])
def test_close_joins_transaction_and_preserves_cancellation(tmp_path, monkeypatch, cancellations):
    asyncio.run(_close_during_transaction(tmp_path, monkeypatch, cancellations=cancellations))


def test_concurrent_close_joins_the_same_transaction(tmp_path, monkeypatch):
    asyncio.run(_close_during_transaction(tmp_path, monkeypatch, concurrent=True))


async def _close_during_transaction(tmp_path, monkeypatch, *, cancellations=0, concurrent=False):
    path = tmp_path / "registry.sqlite3"
    store = MemoryStore(path)
    job_id = store.enqueue("reindex_memory", {"memory_id": "synthetic-memory"})
    actor = _MemoryStoreActor(lambda: store)
    started = asyncio.Event()
    barrier_submitted = asyncio.Event()
    release = threading.Event()
    committed = threading.Event()
    loop = asyncio.get_running_loop()

    def write(_store):
        # This is an open, uncommitted database transaction, not a fake future.
        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        committed.set()

    operation = asyncio.create_task(actor.call(write))
    closers = []
    executor = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        executor = actor._executor
        real_submit = executor.submit

        def observe_submit(*args, **kwargs):
            future = real_submit(*args, **kwargs)
            barrier_submitted.set()
            return future

        monkeypatch.setattr(executor, "submit", observe_submit)
        first = asyncio.create_task(actor.close())
        closers.append(first)
        await asyncio.wait_for(barrier_submitted.wait(), 2)
        for index in range(cancellations):
            first.cancel("actor closing cancelled" if index == 0 else "repeated cancellation")
            # A scheduling checkpoint delivers each cancellation separately;
            # the disk transaction remains held by the explicit thread gate.
            await asyncio.sleep(0)
        if concurrent:
            entered = asyncio.Event()

            async def close_again():
                entered.set()
                await actor.close()

            second = asyncio.create_task(close_again())
            closers.append(second)
            await asyncio.wait_for(entered.wait(), 2)
        assert not committed.is_set()
        assert any(thread.is_alive() for thread in executor._threads)
        assert all(not closer.done() for closer in closers)
        with pytest.raises(RuntimeError, match="closed"):
            await actor.call(lambda _store: None)
        with pytest.raises(RuntimeError, match="closing"):
            actor.reopen()
        assert actor._executor is None
        assert actor._closed

        release.set()
        await asyncio.wait_for(operation, 2)
        if cancellations:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(first, 2)
            assert cancelled.value.args == ("actor closing cancelled",)
        else:
            await asyncio.wait_for(first, 2)
        if concurrent:
            await asyncio.wait_for(second, 2)
        assert committed.is_set()
        assert executor._shutdown
        assert all(not thread.is_alive() for thread in executor._threads)
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,),
            ).fetchone()[0] == "done"
        # Repeated completed shutdown is harmless; a fresh start owns a new lane.
        await actor.close()
        actor.reopen()
        assert await actor.call(lambda target: target.list_jobs()[0]["status"]) == "done"
        await actor.close()
    finally:
        release.set()
        await asyncio.gather(operation, *closers, return_exceptions=True)
        await actor.close()
        if executor is not None:
            # Baseline failure cleanup also joins its abandoned real thread.
            executor.shutdown(wait=True)
