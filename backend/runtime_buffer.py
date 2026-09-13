"""Nonblocking SDK handoff budgets with payload-free queue diagnostics.

Never wait for queue space in the sole SDK reader. Exhausting either budget
raises an explicit SDK error; SessionStream then disconnects the exact client
and exposes failure through its existing EOF/canonical-recovery path.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import dataclasses
import sys
import re
import time
import weakref
from typing import Any

from claude_agent_sdk import ClaudeSDKError

from . import observability as obs
from .settings import env_int

MAX_EVENTS = env_int('MUSELAB_RUNTIME_BUFFER_EVENTS', 8192, min_value=1)
MAX_BYTES = env_int('MUSELAB_RUNTIME_BUFFER_BYTES', 64 * 1024 * 1024, min_value=1024)
_NO_EOF = object()
_queues: weakref.WeakSet = weakref.WeakSet()
_overflows = _peak_depth = _peak_bytes = _max_wait_ms = 0


class RuntimeBufferExceeded(ClaudeSDKError):
    def __init__(self):
        super().__init__('runtime_buffer_exceeded: SDK output backlog exceeded the runtime budget; reload conversation history before retrying')


def estimated_size(value: Any, limit: int = MAX_BYTES) -> int:
    """Bound a traversal of parsed SDK objects without formatting their content."""
    seen: set[int] = set()
    pending = [value]
    size = 0
    while pending and size <= limit:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(item)
        if size > limit:
            break
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in dataclasses.fields(item))
        elif hasattr(item, '__dict__'):
            pending.append(vars(item))
    return size


def message_kind(item: Any) -> str:
    """Describe the SDK envelope only, never its prompt or output."""
    name = type(item).__name__
    subtype = getattr(item, "subtype", None)
    if isinstance(subtype, str) and re.fullmatch(r"[a-z_]{1,48}", subtype):
        return f"{name}:{subtype}"
    return name if re.fullmatch(r"[A-Za-z_]{1,64}", name) else "unknown"


def validate_message_size(item: Any, *, lane: str, session_id: str) -> None:
    """Direct consumers retain the same per-message byte guard as queues."""
    global _overflows
    size = estimated_size(item, MAX_BYTES)
    if size > MAX_BYTES:
        _overflows += 1
        obs.perf_event("chat.runtime_buffer_exceeded", lane=lane,
            depth=0, estimated_bytes=0, incoming_bytes=size, session=session_id[:8],
            incoming_kind=message_kind(item), oldest_ms=0)
        raise RuntimeBufferExceeded()


class RuntimeMessageQueue(asyncio.Queue):
    def __init__(self, *, lane: str, eof: Any = _NO_EOF,
                 max_events: int | None = None, max_bytes: int | None = None,
                 session_id: str = ""):
        super().__init__()
        self.lane = lane
        self.session_id = session_id[:8]
        self.message_kinds: Counter[str] = Counter()
        self.eof = eof
        self.max_events = MAX_EVENTS if max_events is None else max_events
        self.max_bytes = MAX_BYTES if max_bytes is None else max_bytes
        self.estimated_bytes = 0
        self.peak_depth = self.peak_bytes = self.max_wait_ms = self.overflows = 0
        _queues.add(self)

    def put_nowait(self, item: Any) -> None:
        global _overflows, _peak_depth, _peak_bytes
        terminal = item is self.eof
        size = 0 if terminal else estimated_size(item, self.max_bytes)
        if not terminal and (self.qsize() >= self.max_events
                             or self.estimated_bytes + size > self.max_bytes):
            self.overflows += 1
            _overflows += 1
            obs.perf_event('chat.runtime_buffer_exceeded', lane=self.lane,
                           depth=self.qsize(), estimated_bytes=self.estimated_bytes,
                           incoming_bytes=size, session=self.session_id,
                           incoming_kind=message_kind(item), oldest_ms=self.oldest_ms,
                           **{f"envelope_{index}": f"{kind}:{count}"
                              for index, (kind, count) in enumerate(
                                  self.message_kinds.most_common(12))})
            raise RuntimeBufferExceeded()
        super().put_nowait((item, size, time.monotonic()))
        self.message_kinds[message_kind(item)] += 1
        self.estimated_bytes += size
        self.peak_depth = max(self.peak_depth, self.qsize())
        self.peak_bytes = max(self.peak_bytes, self.estimated_bytes)
        _peak_depth = max(_peak_depth, self.peak_depth)
        _peak_bytes = max(_peak_bytes, self.peak_bytes)

    def get_nowait(self) -> Any:
        global _max_wait_ms
        item, size, enqueued = super().get_nowait()
        self.estimated_bytes -= size
        kind = message_kind(item)
        self.message_kinds[kind] -= 1
        if self.message_kinds[kind] <= 0:
            del self.message_kinds[kind]
        wait_ms = max(0, round((time.monotonic() - enqueued) * 1000))
        self.max_wait_ms = max(self.max_wait_ms, wait_ms)
        _max_wait_ms = max(_max_wait_ms, wait_ms)
        return item

    @property
    def oldest_ms(self) -> int:
        return max(0, round((time.monotonic() - self._queue[0][2]) * 1000)) if self._queue else 0


def diagnostics(*, streams: int = 0) -> dict[str, int]:
    queues = tuple(_queues)
    return {
        'streams': streams,
        'depth': sum(queue.qsize() for queue in queues),
        'estimated_bytes': sum(queue.estimated_bytes for queue in queues),
        'peak_depth': _peak_depth,
        'peak_estimated_bytes': _peak_bytes,
        'oldest_ms': max((queue.oldest_ms for queue in queues), default=0),
        'max_wait_ms': _max_wait_ms,
        'overflows': _overflows,
        'max_events_per_queue': MAX_EVENTS,
        'max_bytes_per_queue': MAX_BYTES,
    }
