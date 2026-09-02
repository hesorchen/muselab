"""Privacy-bounded execution traces for SDK Hook lifecycle events.

The Claude SDK/CLI remains the lifecycle authority.  MuseLab persists only a
small rendering projection: event kind, terminal status, exit code and timing.
Raw commands, paths, stdout/stderr, HTTP bodies, headers and hook outputs never
enter this store or its SSE payloads.
"""

from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
import json
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from claude_agent_sdk import HookEventMessage, SystemMessage

from . import sessions as sess
from .private_storage import (
    ensure_private_regular_file,
    private_path_kind,
    write_private_bytes,
)


_SCHEMA_VERSION = 1
_MAX_TRACES = 256
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_EVENT = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_HOOK_SUBTYPES = frozenset({"hook_started", "hook_progress", "hook_response"})
_FAILURE_OUTCOMES = frozenset(
    {
        "error",
        "failed",
        "failure",
        "blocked",
        "cancelled",
        "canceled",
    }
)

# Hook progress can be very chatty and several sessions may stream it at once.
# Keep disk I/O isolated per session instead of serialising every SDK stream on
# one process-wide lock.  The small cache avoids reparsing the same bounded
# projection for polling reads and for every lifecycle message.
_TRACE_LOCKS = tuple(threading.RLock() for _ in range(64))
_TraceSignature = tuple[int, int, int, int]
_TRACE_CACHE: OrderedDict[str, tuple[_TraceSignature, tuple[dict[str, Any], ...]]] = OrderedDict()
_TRACE_CACHE_LOCK = threading.Lock()
_TRACE_CACHE_MAX = 128


def is_hook_message(message: Any) -> bool:
    return isinstance(message, HookEventMessage) or (
        isinstance(message, SystemMessage)
        and str(getattr(message, "subtype", "") or "") in _HOOK_SUBTYPES
    )


def _safe_session_id(session_id: str) -> str:
    value = str(session_id or "")
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError("invalid session id")
    return value


def _trace_path(session_id: str) -> Path:
    sid = _safe_session_id(session_id)
    return Path(sess.SESS_DIR) / "hook-traces" / f"{sid}.json"


def _opaque_id(value: Any) -> str:
    raw = str(value or "").strip()
    if _SAFE_COMPONENT.fullmatch(raw):
        return raw
    if raw:
        return "opaque-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return str(uuid.uuid4())


def _bounded_turn_id(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if _SAFE_COMPONENT.fullmatch(raw) else ""


def _message_data(message: Any) -> Mapping[str, Any]:
    data = getattr(message, "data", None)
    return data if isinstance(data, Mapping) else {}


def _hook_event_name(message: Any, data: Mapping[str, Any]) -> str:
    raw = (
        getattr(message, "hook_event_name", None)
        or data.get("hook_event")
        or data.get("hook_event_name")
        or ""
    )
    value = str(raw or "").strip()
    return value if _SAFE_EVENT.fullmatch(value) else "Unknown"


def _hook_identity(message: Any, data: Mapping[str, Any]) -> str:
    return _opaque_id(data.get("hook_id") or data.get("id") or getattr(message, "uuid", None))


def _exit_code(data: Mapping[str, Any]) -> int | None:
    value = data.get("exit_code")
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if -255 <= parsed <= 255 else None


def _response_status(data: Mapping[str, Any], exit_code: int | None) -> str:
    outcome = str(data.get("outcome") or "").strip().lower()
    if exit_code not in (None, 0):
        return "failed"
    if outcome in _FAILURE_OUTCOMES or data.get("is_error") is True:
        return "failed"
    return "succeeded"


def _trace_lock(session_id: str) -> threading.RLock:
    return _TRACE_LOCKS[hash(session_id) % len(_TRACE_LOCKS)]


def _trace_signature(path: Path) -> _TraceSignature | None:
    try:
        file_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    return (file_stat.st_dev, file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)


def _cache_key(session_id: str) -> str:
    # Tests and embedded deployments can relocate SESS_DIR while retaining a
    # session id.  Key by the concrete store path so those roots never alias.
    return str(_trace_path(session_id))


def _drop_cache(session_id: str) -> None:
    with _TRACE_CACHE_LOCK:
        _TRACE_CACHE.pop(_cache_key(session_id), None)


def _cached_traces(
    session_id: str,
    signature: _TraceSignature,
) -> list[dict[str, Any]] | None:
    key = _cache_key(session_id)
    with _TRACE_CACHE_LOCK:
        hit = _TRACE_CACHE.get(key)
        if hit is None or hit[0] != signature:
            if hit is not None:
                _TRACE_CACHE.pop(key, None)
            return None
        _TRACE_CACHE.move_to_end(key)
        return [copy.deepcopy(row) for row in hit[1]]


def _store_cache(
    session_id: str,
    signature: _TraceSignature | None,
    traces: list[dict[str, Any]],
) -> None:
    key = _cache_key(session_id)
    with _TRACE_CACHE_LOCK:
        if signature is None:
            _TRACE_CACHE.pop(key, None)
            return
        _TRACE_CACHE[key] = (
            signature,
            tuple(copy.deepcopy(row) for row in traces[-_MAX_TRACES:]),
        )
        _TRACE_CACHE.move_to_end(key)
        while len(_TRACE_CACHE) > _TRACE_CACHE_MAX:
            _TRACE_CACHE.popitem(last=False)


def _load_locked(session_id: str) -> list[dict[str, Any]]:
    path = _trace_path(session_id)
    if not ensure_private_regular_file(path):
        _drop_cache(session_id)
        return []
    signature = _trace_signature(path)
    if signature is None:
        _drop_cache(session_id)
        return []
    cached = _cached_traces(session_id, signature)
    if cached is not None:
        return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        return []
    traces = payload.get("traces")
    if not isinstance(traces, list):
        return []
    loaded = [copy.deepcopy(row) for row in traces[-_MAX_TRACES:] if isinstance(row, dict)]
    _store_cache(session_id, signature, loaded)
    return loaded


def _write_locked(session_id: str, traces: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "traces": traces[-_MAX_TRACES:],
    }
    write_private_bytes(
        _trace_path(session_id),
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    # Publish cache state only after the atomic durable write succeeds.
    _store_cache(
        session_id,
        _trace_signature(_trace_path(session_id)),
        traces,
    )


def observe(
    session_id: str,
    message: Any,
    *,
    turn_id: str = "",
    origin: str = "foreground",
    observed_at_ms: int | None = None,
) -> dict[str, Any] | None:
    """Project one SDK lifecycle message into a safe durable trace."""
    if not is_hook_message(message):
        return None
    subtype = str(getattr(message, "subtype", "") or "")
    if subtype not in _HOOK_SUBTYPES:
        return None
    data = _message_data(message)
    trace_id = _hook_identity(message, data)
    hook_event = _hook_event_name(message, data)
    now = max(0, int(observed_at_ms or time.time() * 1000))
    safe_origin = "background" if origin == "background" else "foreground"
    safe_session_id = _safe_session_id(session_id)

    with _trace_lock(safe_session_id):
        traces = _load_locked(safe_session_id)
        trace = next(
            (
                row
                for row in reversed(traces)
                if row.get("trace_id") == trace_id and row.get("status") == "running"
            ),
            None,
        )

        if subtype == "hook_started":
            trace = {
                "trace_id": trace_id,
                "hook_event": hook_event,
                "status": "running",
                "exit_code": None,
                "started_at_ms": now,
                "finished_at_ms": None,
                "duration_ms": None,
                "updated_at_ms": now,
                "turn_id": _bounded_turn_id(turn_id),
                "origin": safe_origin,
            }
            traces.append(trace)
        elif subtype == "hook_progress":
            # Progress output is intentionally discarded. Without a stable
            # hook id, do not guess which concurrent handler it belongs to.
            if trace is None:
                return None
            trace["updated_at_ms"] = now
        else:
            exit_code = _exit_code(data)
            if trace is None:
                trace = {
                    "trace_id": trace_id,
                    "hook_event": hook_event,
                    "status": "running",
                    "exit_code": None,
                    "started_at_ms": now,
                    "finished_at_ms": None,
                    "duration_ms": None,
                    "updated_at_ms": now,
                    "turn_id": _bounded_turn_id(turn_id),
                    "origin": safe_origin,
                }
                traces.append(trace)
            trace["hook_event"] = hook_event
            trace["status"] = _response_status(data, exit_code)
            trace["exit_code"] = exit_code
            trace["finished_at_ms"] = now
            trace["duration_ms"] = max(0, now - int(trace.get("started_at_ms") or now))
            trace["updated_at_ms"] = now
            if not trace.get("turn_id"):
                trace["turn_id"] = _bounded_turn_id(turn_id)
            trace["origin"] = safe_origin

        if subtype == "hook_progress":
            # Progress carries no persisted output and changes only a heartbeat
            # timestamp.  Keep it live in the bounded cache/SSE projection;
            # hook_started and hook_response remain the durable boundaries.
            # This turns an unbounded progress stream into zero disk writes.
            _store_cache(
                safe_session_id,
                _trace_signature(_trace_path(safe_session_id)),
                traces,
            )
        else:
            _write_locked(safe_session_id, traces)
        return copy.deepcopy(trace)


def list_traces(session_id: str, *, turn_id: str = "") -> list[dict[str, Any]]:
    safe_session_id = _safe_session_id(session_id)
    with _trace_lock(safe_session_id):
        traces = _load_locked(safe_session_id)
    if turn_id:
        safe_turn = _bounded_turn_id(turn_id)
        if not safe_turn:
            return []
        traces = [row for row in traces if row.get("turn_id") == safe_turn]
    return traces


def purge(session_id: str) -> bool:
    safe_session_id = _safe_session_id(session_id)
    path = _trace_path(safe_session_id)
    with _trace_lock(safe_session_id):
        kind = private_path_kind(path)
        if kind == "missing":
            _drop_cache(safe_session_id)
            return False
        if kind != "file":
            return False
        try:
            path.unlink()
        except OSError:
            return False
        _drop_cache(safe_session_id)
        return True
