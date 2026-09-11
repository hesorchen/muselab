"""A timed-out UI reader must own its eventual database failure."""
import asyncio
import gc
import sqlite3
import threading

import pytest

from backend.memory_engine import _MemoryStoreActor


@pytest.mark.parametrize('cancel_pending', [False, True])
def test_cancelled_actor_waiter_observes_late_error(cancel_pending):
    started = threading.Event()
    release = threading.Event()

    def fail_after_cancel(_store):
        started.set()
        assert release.wait(2)
        raise sqlite3.OperationalError('interrupted')

    async def scenario():
        actor = _MemoryStoreActor(lambda: object(), cancel_pending=cancel_pending)
        contexts = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        try:
            waiter = asyncio.create_task(actor.call(fail_after_cancel))
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            # Let shield detach before the worker raises, as a timed-out SQL
            # progress handler does after the HTTP request has already left.
            await asyncio.sleep(0)
            release.set()
            assert await actor.call(lambda _store: 42) == 42
            await actor.close()
            del waiter
            gc.collect()
            await asyncio.sleep(0)
            assert not contexts, [context['message'] for context in contexts]
        finally:
            release.set()
            await actor.close()
            loop.set_exception_handler(old_handler)

    asyncio.run(scenario())


def test_active_actor_waiter_still_receives_failure():
    async def scenario():
        actor = _MemoryStoreActor(lambda: object())
        def fail(_store):
            raise sqlite3.OperationalError('database is locked')
        try:
            with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                await actor.call(fail)
        finally:
            await actor.close()
    asyncio.run(scenario())
