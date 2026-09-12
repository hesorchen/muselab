"""Private native-Cron receipts. The CLI remains the only schedule executor.

Receipts restore identity and failure visibility, never authorize an extra fire.
Only bounded control fields and the task inspector's prompt preview are stored.
"""
from __future__ import annotations

import json
import os
import stat
import re
import threading
from pathlib import Path
from typing import Any

from . import sessions
from .private_storage import (
    UnsafePrivatePath, ensure_private_directory, private_path_kind, write_private_bytes,
)
from . import observability as obs

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOCKS = tuple(threading.RLock() for _ in range(64))
_MAX_FILE_BYTES = 2 * 1024 * 1024
_TEXT_FIELDS = {
    "cron": 128, "prompt": 4000, "prompt_sha256": 64,
    "model": 256, "effort": 32, "service_tier": 32,
    "runtime_state": 32, "last_status": 32, "last_error": 96, "last_execution_status": 32,
    "last_run_id": 128, "owner_session_id": 128,
}
_BOOL_FIELDS = {"recurring", "durable", "prompt_truncated", "record_saved"}
_INT_FIELDS = {
    "created_at_ms", "expires_at_ms", "disconnected_at_ms", "recovered_at_ms",
    "last_started_at_ms", "last_finished_at_ms", "last_success_at_ms",
    "tool_calls", "tool_results", "recovery_attempts",
}


def path(sid: str) -> Path:
    if not _ID.fullmatch(sid):
        raise ValueError("invalid session id")
    return Path(sessions.SESS_DIR) / "native-cron" / f"{sid}.json"


def bounded_jobs(jobs: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(jobs, dict):
        return result
    # Keep up to 50 runnable/uncertain jobs plus 50 recent terminal receipts.
    # Completed one-shots must never crowd a newly created active plan off disk.
    eligible = [(key, raw) for key, raw in jobs.items()
                if isinstance(key, str) and _ID.fullmatch(key) and isinstance(raw, dict)]
    eligible.sort(key=lambda item: item[1].get("created_at_ms", 0)
                  if isinstance(item[1].get("created_at_ms", 0), int) else 0, reverse=True)
    counts = {True: 0, False: 0}
    for job_id, raw in eligible:
        terminal = raw.get("runtime_state") in {"finished", "expired", "missing", "paused"}
        if counts[terminal] >= 50:
            continue
        counts[terminal] += 1
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id) or not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {}
        for key, limit in _TEXT_FIELDS.items():
            value = raw.get(key)
            if isinstance(value, str):
                row[key] = "".join(c if c.isprintable() or c in "\n\t" else "�"
                                   for c in value[:limit])
        for key in _BOOL_FIELDS:
            if isinstance(raw.get(key), bool):
                row[key] = raw[key]
        for key in _INT_FIELDS:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**16:
                row[key] = value
        result[job_id] = row
    return result


def _read(file: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(file, flags)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise UnsafePrivatePath("unsafe native schedule receipt")
        os.fchmod(handle.fileno(), 0o600)
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("native schedule receipt exceeds its bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("invalid native schedule receipt")
    return data


def save(sid: str, jobs: dict, revision: int) -> None:
    file = path(sid)
    with _LOCKS[hash(sid) % len(_LOCKS)]:
        ensure_private_directory(file.parent)
        old = _read(file)
        if isinstance(old.get("revision"), int) and old["revision"] > revision:
            return  # a delayed worker cannot overwrite a newer tool result
        value = {"schema": 1, "revision": revision, "jobs": bounded_jobs(jobs)}
        encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValueError("native schedule receipt exceeds its bound")
        write_private_bytes(file, encoded)


def load_all() -> dict[str, dict]:
    folder = Path(sessions.SESS_DIR) / "native-cron"
    if private_path_kind(folder) == "missing":
        return {}
    if private_path_kind(folder) != "directory":
        raise ValueError("unsafe native schedule directory")
    result = {}
    for file in folder.glob("*.json"):
        if not _ID.fullmatch(file.stem):
            continue
        try:
            with _LOCKS[hash(file.stem) % len(_LOCKS)]:
                jobs = bounded_jobs(_read(file).get("jobs"))
        except (OSError, ValueError, UnsafePrivatePath) as exc:
            obs.perf_event("chat.native_cron_receipt_invalid", session=obs.short_id(file.stem),
                           error_kind=type(exc).__name__)
            continue
        if jobs:
            result[file.stem] = jobs
    return result


def purge(sid: str) -> None:
    file = path(sid)
    with _LOCKS[hash(sid) % len(_LOCKS)]:
        kind = private_path_kind(file)
        if kind not in {"missing", "file"}:
            raise ValueError("unsafe native schedule receipt")
        file.unlink(missing_ok=True)
