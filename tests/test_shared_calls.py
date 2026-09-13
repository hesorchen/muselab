import asyncio

import pytest

from backend.shared_calls import SharedCalls


def test_one_cancelled_waiter_does_not_cancel_shared_work():
    async def run():
        calls = SharedCalls()
        started, release = asyncio.Event(), asyncio.Event()
        invocations = 0
        async def work():
            nonlocal invocations
            invocations += 1
            started.set()
            await release.wait()
            return 42
        first = asyncio.create_task(calls.run("fixture", work))
        second = asyncio.create_task(calls.run("fixture", work))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        assert await second == 42 and invocations == 1
        assert not calls._calls
    asyncio.run(run())


def test_last_cancelled_waiter_joins_producer_and_failures_are_not_cached():
    async def run():
        calls = SharedCalls()
        started, stopped = asyncio.Event(), asyncio.Event()
        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        caller = asyncio.create_task(calls.run("fixture", work))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert stopped.is_set() and not calls._calls
        async def broken():
            raise ValueError("fixture")
        with pytest.raises(ValueError):
            await calls.run("fixture", broken)
        async def healthy():
            return "new"
        assert await calls.run("fixture", healthy) == "new"
    asyncio.run(run())
