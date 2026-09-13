"""Coalesce concurrent work without caching results or orphaning producers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Hashable, TypeVar

_T = TypeVar("_T")


@dataclass
class _Call:
    task: asyncio.Task
    waiters: int = 0


class SharedCalls:
    """One producer per key and event loop, owned by its live waiters."""

    def __init__(self):
        self._calls: dict[tuple[Any, Hashable], _Call] = {}

    async def run(self, key: Hashable, operation: Callable[[], Awaitable[_T]]) -> _T:
        scoped = (asyncio.get_running_loop(), key)
        call = self._calls.get(scoped)
        if call is None or call.task.done():
            call = _Call(asyncio.create_task(operation()))
            self._calls[scoped] = call
        call.waiters += 1
        try:
            return await asyncio.shield(call.task)
        finally:
            call.waiters -= 1
            if not call.waiters:
                if self._calls.get(scoped) is call:
                    self._calls.pop(scoped, None)
                if not call.task.done():
                    call.task.cancel()
                # Observe failure even when every caller was cancelled.
                await asyncio.gather(call.task, return_exceptions=True)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = [call.task for (owner, _), call in tuple(self._calls.items())
                 if owner is loop]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
