"""Small, privacy-bounded performance events for core MuseLab paths.

The service already has detailed user-facing errors.  This module is for the
opposite problem: reconstructing *where time went* without copying prompts,
paths, URLs, file names, tool payloads, or exception text into server logs.

Events are deliberately one-line JSON so journald can retain them without a
new file logger (and therefore without a third rotation/retention policy).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from typing import Any


_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_SENSITIVE_FIELD_PARTS = frozenset({
    "auth", "body", "command", "content", "cookie", "credential",
    "filename", "href", "key", "message", "path", "prompt", "query",
    "secret", "stack", "text", "token", "url",
})
_SAFE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def perf_enabled() -> bool:
    """Return whether compact performance events are enabled (default: on)."""
    return os.getenv("MUSELAB_PERF_LOG", "1").strip().lower() not in _FALSE_VALUES


def slow_request_ms() -> float:
    """Configured slow-request boundary, clamped to a useful safe range."""
    raw = os.getenv("MUSELAB_SLOW_REQUEST_MS", "500")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 500.0
    if not math.isfinite(value):
        value = 500.0
    return min(60_000.0, max(25.0, value))


def monotonic() -> float:
    return time.perf_counter()


def elapsed_ms(started: float, ended: float | None = None) -> int:
    """Monotonic elapsed milliseconds, never negative."""
    end = time.perf_counter() if ended is None else ended
    return max(0, round((end - started) * 1000))


def is_slow(duration_ms: int | float, *, threshold_ms: float | None = None) -> bool:
    threshold = slow_request_ms() if threshold_ms is None else threshold_ms
    return float(duration_ms) >= max(0.0, float(threshold))


def short_id(value: Any) -> str:
    """Return a correlation hint, never a full session/task identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    return cleaned[:8]


def _safe_field_name(name: str) -> bool:
    if not _SAFE_FIELD_RE.fullmatch(name):
        return False
    parts = frozenset(name.split("_"))
    return not bool(parts & _SENSITIVE_FIELD_PARTS)


def _safe_value(value: Any) -> bool | int | float | str | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else None
    # Call sites only pass bounded classifications and identifiers.  This final
    # guard prevents control-character log injection and accidental large rows.
    return _CONTROL_RE.sub(" ", str(value)).strip()[:160]


def perf_event(event: str, /, **fields: Any) -> None:
    """Write one privacy-bounded structured event to stderr.

    Sensitive-looking field names are rejected rather than heuristically
    redacted: a future caller cannot accidentally log `prompt=` or `path=` and
    assume a best-effort scrubber made arbitrary content safe.
    """
    if not perf_enabled():
        return
    if not _SAFE_EVENT_RE.fullmatch(str(event or "")):
        raise ValueError("invalid performance event name")
    unsafe = [name for name in fields if not _safe_field_name(name)]
    if unsafe:
        raise ValueError(
            "sensitive or invalid performance field(s): " + ", ".join(unsafe)
        )
    payload: dict[str, Any] = {"event": event}
    for name, value in fields.items():
        if value is not None:
            payload[name] = _safe_value(value)
    sys.stderr.write(
        "[perf] "
        + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        + "\n"
    )
    sys.stderr.flush()
