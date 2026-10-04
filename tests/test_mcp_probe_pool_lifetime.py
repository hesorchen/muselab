"""Completed control pools must not keep their closed event loop alive."""
import asyncio
import gc
import weakref
from types import SimpleNamespace

import pytest

from backend import mcp_probes


@pytest.mark.parametrize("contended", [False, True])
def test_completed_probe_pool_releases_closed_loop(
    monkeypatch, record_property, contended,
):
    monkeypatch.setattr(mcp_probes, "CONCURRENCY", 1)
    monkeypatch.setattr(mcp_probes, "DEADLINE_S", 0.01)
    loop_ref = pool_ref = None

    async def run():
        nonlocal loop_ref, pool_ref
        release = asyncio.Event()
        calls = 0

        async def control():
            nonlocal calls
            calls += 1
            await release.wait()
            return {"mcpServers": []}

        count = 2 if contended else 1
        live = [(key, SimpleNamespace(get_mcp_status=control)) for key in range(count)]
        rows = await mcp_probes.probe_clients(live, "get_mcp_status")
        pool = mcp_probes._pools[asyncio.get_running_loop()]
        loop_ref = weakref.ref(asyncio.get_running_loop())
        pool_ref = weakref.ref(pool)
        try:
            assert all(row["pending"] for row in rows)
            # One SDK call owns the slot; the second, when present, genuinely
            # waits on the pool's semaphore until the first owner completes.
            assert calls == 1
            assert len(pool.tasks) == count
        finally:
            release.set()
            await asyncio.gather(*list(pool.tasks.values()))
        assert calls == count
        assert not pool.tasks

    asyncio.run(run())
    gc.collect()
    outcome = {
        "contended": contended,
        "closed_loop_retained": loop_ref() is not None,
        "completed_pool_retained": pool_ref() is not None,
    }
    for name, value in outcome.items():
        record_property(name, value)
    assert loop_ref() is None, outcome
    assert pool_ref() is None, outcome


def test_later_pool_preserves_results_singleflight_and_concurrency(monkeypatch):
    monkeypatch.setattr(mcp_probes, "CONCURRENCY", 1)

    async def run():
        real_wait = asyncio.wait
        completed = []

        for _ in range(2):
            release = asyncio.Event()
            full_observer_entered = asyncio.Event()
            calls = [0, 0, 0]
            active = peak = wait_calls = 0

            async def observed_wait(tasks, **kwargs):
                nonlocal wait_calls
                wait_calls += 1
                if wait_calls == 3:
                    full_observer_entered.set()
                return await real_wait(tasks, **kwargs)

            monkeypatch.setattr(mcp_probes.asyncio, "wait", observed_wait)
            live = []
            for key in range(3):
                async def control(index=key):
                    nonlocal active, peak
                    calls[index] += 1
                    active += 1
                    peak = max(peak, active)
                    try:
                        await release.wait()
                        return {"mcpServers": []}
                    finally:
                        active -= 1
                live.append((key, SimpleNamespace(get_mcp_status=control)))

            monkeypatch.setattr(mcp_probes, "DEADLINE_S", 0.01)
            first = await mcp_probes.probe_clients(live, "get_mcp_status")
            pool = mcp_probes._pools[asyncio.get_running_loop()]
            owners = tuple(pool.tasks.values())
            full_observer = None
            try:
                second = await mcp_probes.probe_clients(live, "get_mcp_status")
                assert all(row["pending"] for row in first + second)
                assert tuple(pool.tasks.values()) == owners
                assert calls == [1, 0, 0]
                # Older observers can still consume their completed task
                # results while this loop's subsequent controls are pending.
                assert all(task.result() == {"result": {"mcpServers": []}}
                           for task in completed)
                monkeypatch.setattr(mcp_probes, "DEADLINE_S", 30.0)
                full_observer = asyncio.create_task(
                    mcp_probes.probe_clients(live, "get_mcp_status"))
                await asyncio.wait_for(full_observer_entered.wait(), 10)
                release.set()
                result = await full_observer
                assert result == [{"key": key, "result": {"mcpServers": []}}
                                  for key in range(3)]
                assert peak == 1 and active == 0
                assert calls == [1, 1, 1]
                assert not pool.tasks
                completed.extend(owners)
            finally:
                release.set()
                await asyncio.gather(*owners,
                                     *([full_observer] if full_observer else []),
                                     return_exceptions=True)

    asyncio.run(run())
