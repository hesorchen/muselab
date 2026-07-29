"""Bounded startup-task and subsystem shutdown orchestration."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from typing import Any


async def _bounded(label: str, operation: Awaitable[Any], timeout: float) -> None:
    try:
        await asyncio.wait_for(operation, timeout=timeout)
    except asyncio.TimeoutError:
        sys.stderr.write(
            f"[muselab] {label} shutdown timed out after {timeout:.1f}s\n"
        )
    except Exception as exc:
        sys.stderr.write(f"[muselab] {label} shutdown failed: {exc}\n")


async def shutdown_runtime(
    background_tasks: set[asyncio.Task],
    *,
    scheduler: Any,
    memory: Any,
    terminal: Any,
    file_watcher: Any,
) -> None:
    """Stop producers first, then drain independent runtime consumers."""
    tasks = tuple(background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    background_tasks.clear()

    await _bounded("scheduler", scheduler.stop_scheduler(), 2.0)

    # Import lazily so lifecycle helpers stay independent of chat's heavy SDK
    # module during static analysis and isolated tests.
    from . import chat

    await asyncio.gather(
        _bounded("file watcher", file_watcher.shutdown(), 8.0),
        _bounded("chat runtime", chat.shutdown_runtime(), 8.0),
        _bounded("terminal", terminal.shutdown(), 8.0),
        _bounded("memory", memory.aclose(), 8.0),
    )
