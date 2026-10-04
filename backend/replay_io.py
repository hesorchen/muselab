"""Ordered replay I/O off the event loop, with bounded pending writes.

The queue is not a best-effort diagnostic sink: errors propagate to flush/read
and subsequent writes. A reader never observes a record before its write ends.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial

_WRITE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="muselab-replay-write")
_READ_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="muselab-replay-read")
_budget_lock = threading.Lock()
_pending_bytes = _pending_jobs = 0
MAX_PENDING_BYTES = 64 * 1024 * 1024
MAX_PENDING_JOBS = 8192


class ReplayBacklogExceeded(RuntimeError):
    def __init__(self):
        super().__init__("replay write backlog exceeded its memory budget")


def in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def read_io(callback, *args):
    return await asyncio.get_running_loop().run_in_executor(
        _READ_POOL, partial(callback, *args))


class ReplayIO:
    """One FIFO per file; no worker waits for another worker's future."""

    def __init__(self):
        self._queue = deque()
        self._lock = threading.Lock()
        self._running = False
        self._error: BaseException | None = None
        self._tail: Future | None = None

    def check(self):
        if self._error is not None:
            raise OSError("replay storage unavailable") from self._error

    def submit(self, callback, *, size=0, cleanup=False):
        global _pending_bytes, _pending_jobs
        if not cleanup:
            self.check()
        future = Future()
        with _budget_lock:
            if not cleanup and (_pending_jobs >= MAX_PENDING_JOBS
                                or _pending_bytes + size > MAX_PENDING_BYTES):
                raise ReplayBacklogExceeded()
            _pending_bytes += size
            _pending_jobs += 1
        with self._lock:
            self._queue.append((callback, size, future, cleanup))
            self._tail = future
            if not self._running:
                self._running = True
                _WRITE_POOL.submit(self._drain)
        return future

    def _drain(self):
        global _pending_bytes, _pending_jobs
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                callback, size, future, cleanup = self._queue.popleft()
            try:
                if not cleanup:
                    self.check()
                result = callback()
            except BaseException as exc:
                # Do not retain exception frames carrying private event blobs.
                if self._error is None:
                    self._error = OSError(getattr(exc, "errno", None) or 5,
                                          "replay storage unavailable")
                future.set_exception(OSError("replay storage unavailable"))
            else:
                future.set_result(result)
            finally:
                with _budget_lock:
                    _pending_bytes -= size
                    _pending_jobs -= 1

    def flush(self):
        future = self._tail
        if future is not None:
            future.result()
        self.check()

    async def flush_async(self):
        future = self._tail
        if future is not None:
            # Cancelling an HTTP reader must not cancel the accepted disk write.
            await asyncio.shield(asyncio.wrap_future(future))
        self.check()


class ReplayReader:
    """Lazy descriptor: subscribe/seek/close never wait on filesystem I/O."""

    def __init__(self, path, writer: ReplayIO, committed_size, offset=0):
        self._path, self._writer = path, writer
        self._committed_size = committed_size
        self._limit = 0
        self._offset = offset
        self._reader = None
        self._lock = threading.Lock()
        self._closed = False

    def tell(self):
        return self._offset

    def seek(self, offset):
        # Only called before the first read, to skip records covered by a cursor.
        if self._reader is not None:
            raise RuntimeError("cannot reposition an active replay reader")
        self._offset = offset

    def readline(self):
        with self._lock:
            if self._closed:
                return b""
            if self._reader is None:
                self._reader = self._path.open("rb")
                self._reader.seek(self._offset)
            # A later write may be in progress after ready() completed. Never
            # mistake its partial trailing record for a corrupt committed line.
            remaining = self._limit - self._offset
            if remaining <= 0:
                return b""
            line = self._reader.readline(remaining)
            self._offset = self._reader.tell()
            return line

    async def ready(self):
        await self._writer.flush_async()
        self._limit = self._committed_size()

    def at_end(self):
        """Whether the cursor exhausted the committed snapshot from ready()."""
        return self._offset >= self._limit

    def close(self):
        self._closed = True
        if in_event_loop():
            _READ_POOL.submit(self._close)
        else:
            self._close()

    def _close(self):
        with self._lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
