"""Bounded, single-flight SDK controls with a short HTTP observation deadline.

A deadline returns partial results without cancelling the SDK control request:
older SDK versions do not remove their pending-control entry on cancellation.
The SDK still owns its transport timeout; repeated HTTP calls reuse that task.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

CONCURRENCY = 4
DEADLINE_S = 3.0


@dataclass
class _Pool:
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(CONCURRENCY))
    tasks: dict[tuple, asyncio.Task] = field(default_factory=dict)


_pools: WeakKeyDictionary = WeakKeyDictionary()


async def probe_clients(live: list, operation: str, *args: Any) -> list[dict]:
    loop = asyncio.get_running_loop()
    pool = _pools.setdefault(loop, _Pool())
    scheduled = []
    for key, client in live:
        identity = (id(client), operation, args)
        task = pool.tasks.get(identity)
        if task is None:
            async def invoke(target=client):
                async with pool.semaphore:
                    try:
                        return {'result': await getattr(target, operation)(*args)}
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        return {'error': type(exc).__name__, 'pending': False}
            task = asyncio.create_task(invoke())
            pool.tasks[identity] = task
            def release(done, ident=identity):
                if pool.tasks.get(ident) is done:
                    pool.tasks.pop(ident, None)
                # A contended semaphore points back to its loop, so weak keys
                # alone cannot collect an empty pool after that loop closes.
                if not pool.tasks and _pools.get(loop) is pool:
                    _pools.pop(loop, None)
                if not done.cancelled():
                    done.exception()  # Always retrieve detached failures.
            task.add_done_callback(release)
        scheduled.append((key, task))
    if not scheduled:
        return []
    # asyncio.wait leaves pending controls running and does not cancel them if
    # this browser disconnects. No more than CONCURRENCY controls run per loop.
    await asyncio.wait([task for _key, task in scheduled], timeout=DEADLINE_S)
    return [
        {'key': key, **(task.result() if task.done() and not task.cancelled()
                        else {'error': 'control_timeout', 'pending': True})}
        for key, task in scheduled
    ]
