"""Bounded best-effort diagnostics; never use this for user-data commits."""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable


class DiagnosticWorker:
    def __init__(self, name: str, capacity: int = 512):
        self._queue: queue.Queue = queue.Queue(maxsize=capacity)
        self._closed = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, callback: Callable, *args, **kwargs) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait((callback, args, kwargs))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        while True:
            try:
                callback, args, kwargs = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed:
                    return
                continue
            try:
                callback(*args, **kwargs)
            except Exception:
                # Diagnostics must not affect the authoritative stream. Avoid
                # exception payloads here: they may contain private hook data.
                self.dropped += 1
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 2.0):
        self._closed = True
        self._thread.join(timeout=timeout)
        # A wedged disk/sink cannot hold shutdown or retain a full payload queue.
        if self._thread.is_alive():
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.dropped += 1
                except queue.Empty:
                    break
