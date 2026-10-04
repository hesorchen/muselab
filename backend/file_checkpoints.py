"""Guarded previews for the SDK's public, non-CAS ``rewind_files`` operation.

Python SDK 0.2.149 returns None and exposes no dry-run/file list. This module
lists only main-agent writes observed by our public hooks. Unknown, incomplete,
external, large or unsafe paths fail closed; no SDK response is invented.
"""

from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any

from . import sessions as sess
from .private_storage import ensure_private_regular_file, write_private_bytes
from .runtime_identity import directory_identity
from .task_delivery import lock, relative_path

_MAX_FILE = 4 * 1024 * 1024
_MAX_OPS = 2000
_MAX_SCOPE_FILES = 200
_MAX_BACKUP_BYTES = 16 * 1024 * 1024
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_LOCK = threading.Lock()
_TOOLS = {"Write", "Edit", "NotebookEdit"}


class _PreviewWatch:
    """Linux directory notifications close the same-timestamp file ABA gap."""

    def __init__(self, cwd: Path, rows: list[dict]):
        import ctypes

        self.fd = -1
        self.names: dict[int, set[bytes]] = {}
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError("file observation unavailable")
        try:
            for row in rows:
                target = cwd / row["path"]
                wd = libc.inotify_add_watch(self.fd, os.fsencode(target.parent), 0x00000FCE)
                if wd < 0:
                    raise OSError("file observation unavailable")
                self.names.setdefault(wd, set()).add(os.fsencode(target.name))
        except BaseException:
            self.close()
            raise

    def changed(self) -> bool:
        import struct

        while True:
            try:
                body = os.read(self.fd, 65536)
            except BlockingIOError:
                return False
            if not body:
                return True
            offset = 0
            while offset + 16 <= len(body):
                wd, mask, _cookie, size = struct.unpack_from("iIII", body, offset)
                name = body[offset + 16 : offset + 16 + size].split(b"\0", 1)[0]
                offset += 16 + size
                if mask & (
                    0x00004000 | 0x00000800 | 0x00000400 | 0x00008000
                ) or name in self.names.get(wd, set()):
                    return True

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _path(sid: str) -> Path:
    from .task_delivery import path

    path(sid)  # validate the session component
    return Path(sess.SESS_DIR) / "checkpoints" / f"{sid}.json"


def _load(sid: str) -> dict:
    p = _path(sid)
    if not ensure_private_regular_file(p):
        return {
            "schema": 1,
            "generation": secrets.token_hex(16),
            "turns": {},
            "checkpoints": [],
            "ops": [],
        }
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("invalid checkpoint data")
    return data


def _save(sid: str, data: dict) -> None:
    if sess.session_is_deleting(sid):
        return
    write_private_bytes(_path(sid), json.dumps(data).encode())


def fingerprint(cwd: Path, relative: str) -> dict[str, Any]:
    root = cwd.resolve(strict=True)
    target = root / relative
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError("path outside workspace")
    parents = []
    current = root
    for part in Path(relative).parts[:-1]:
        current = current / part
        st = current.lstat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise ValueError("unsafe path parent")
        parents.append([st.st_dev, st.st_ino])
    try:
        st = target.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "parents": parents}
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
        raise ValueError("unsafe or foreign file")
    if st.st_size > _MAX_FILE:
        raise ValueError("file exceeds checkpoint preview limit")
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns) != (
            st.st_dev,
            st.st_ino,
            st.st_mtime_ns,
            st.st_ctime_ns,
        ):
            raise ValueError("file changed during inspection")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(_MAX_FILE + 1)
        after = os.fstat(fd)
        if (
            after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or len(body) > _MAX_FILE
        ):
            raise ValueError("file changed during inspection")
    finally:
        os.close(fd)
    return {
        "kind": "file",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "device": st.st_dev,
        "inode": st.st_ino,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "parents": parents,
    }


def begin(sid: str, turn_id: str, cwd: Path) -> None:
    identity = directory_identity(cwd)
    with lock(sid):
        data = _load(sid)
        if data.get("directory") and data["directory"] != identity:
            data["invalid"] = "workspace_identity_changed"
        data["directory"] = identity
        data["turns"].setdefault(
            turn_id, {"start": len(data["ops"]), "generation": data["generation"]}
        )
        _save(sid, data)


def invalidate(sid: str, reason: str) -> None:
    with lock(sid):
        data = _load(sid)
        data["invalid"] = reason
        _save(sid, data)


def record_id(sid: str, turn_id: str, checkpoint_id: str) -> None:
    import uuid

    try:
        if str(uuid.UUID(checkpoint_id)) != checkpoint_id:
            return
    except (ValueError, TypeError):
        return
    with lock(sid):
        data = _load(sid)
        start = data["turns"].get(turn_id)
        if not start or any(c["id"] == checkpoint_id for c in data["checkpoints"]):
            return
        data["checkpoints"].append(
            {"id": checkpoint_id, "turn_id": turn_id, **start, "created_at": time.time()}
        )
        _save(sid, data)


def observe(sid: str, turn_id: str, cwd: Path, event: str, payload: dict, tool_id: str) -> None:
    name = payload.get("tool_name")
    if name not in _TOOLS or payload.get("agent_id"):
        return
    inputs = payload.get("tool_input") or {}
    raw_path = inputs.get("file_path") or inputs.get("notebook_path")
    target = relative_path(cwd, raw_path)
    try:
        # Do not canonicalize a symlink into an apparently safe tracked path.
        lexical = Path(raw_path) if isinstance(raw_path, str) else Path("/")
        if not lexical.is_absolute():
            lexical = cwd / lexical
        relative = str(lexical.relative_to(cwd))
        if target is None or ".." in Path(relative).parts:
            raise ValueError("outside workspace")
        state = fingerprint(cwd, relative)
        problem = None
    except (OSError, ValueError, TypeError):
        relative, state, problem = target, None, "unsafe_or_unobserved_path"
    with lock(sid):
        data = _load(sid)
        if turn_id not in data["turns"]:
            data["invalid"] = "write_without_task_boundary"
        if len(data["ops"]) >= _MAX_OPS:
            data["invalid"] = "observation_limit_reached"
            _save(sid, data)
            return
        op = next((o for o in reversed(data["ops"]) if o["id"] == tool_id), None)
        if event == "PreToolUse":
            if op is None:
                data["ops"].append(
                    {
                        "id": tool_id,
                        "path": relative,
                        "before": state,
                        "after": None,
                        "problem": problem,
                        "tool": name,
                        "turn_id": turn_id,
                    }
                )
        elif op is None:
            data["invalid"] = "missing_pre_tool_observation"
        else:
            op["after"] = state
            op["problem"] = problem or op.get("problem")
        _save(sid, data)


def list_checkpoints(sid: str) -> list[dict]:
    with lock(sid):
        data = _load(sid)
        return [
            dict(c, valid=c["generation"] == data["generation"] and not data.get("invalid"))
            for c in reversed(data["checkpoints"])
        ]


def _scope(sid: str, checkpoint_id: str, cwd: Path) -> tuple[dict, list[dict], list[str]]:
    data = _load(sid)
    checkpoint = next((c for c in data["checkpoints"] if c["id"] == checkpoint_id), None)
    if not checkpoint:
        raise ValueError("checkpoint_not_found")
    issues = []
    if data.get("invalid"):
        issues.append(data["invalid"])
    if data.get("directory") != directory_identity(cwd):
        issues.append("workspace_identity_changed")
    if checkpoint["generation"] != data["generation"]:
        issues.append("checkpoint_already_invalidated")
    paths: dict[str, dict] = {}
    for op in data["ops"][checkpoint["start"] :]:
        if op.get("problem") or not op.get("path") or op.get("after") is None:
            issues.append(op.get("problem") or "incomplete_file_observation")
            continue
        p = op["path"]
        if p not in paths:
            paths[p] = {"path": p, "before": op["before"], "expected": op["after"]}
        else:
            # A different writer changed a tracked file between SDK edits.
            if paths[p]["expected"] != op["before"]:
                issues.append("external_edit_between_tools")
            paths[p]["expected"] = op["after"]
    for row in paths.values():
        try:
            actual = fingerprint(cwd, row["path"])
            if actual != row["expected"]:
                issues.append("file_changed_outside_observed_tools")
        except (OSError, ValueError):
            issues.append("unsafe_or_missing_file")
    if (
        len(paths) > _MAX_SCOPE_FILES
        or sum(r["expected"].get("size", 0) for r in paths.values()) > _MAX_BACKUP_BYTES
    ):
        issues.append("restore_scope_limit_reached")
    if not paths:
        issues.append("no_observed_restorable_files")
    return data, list(paths.values()), sorted(set(issues))


def preview(sid: str, checkpoint_id: str, cwd: Path, runtime_marker: str) -> dict:
    with lock(sid):
        data, rows, issues = _scope(sid, checkpoint_id, cwd)
    token = secrets.token_urlsafe(32)
    item = {
        "sid": sid,
        "checkpoint_id": checkpoint_id,
        "cwd": str(cwd),
        "generation": data["generation"],
        "runtime": runtime_marker,
        "rows": rows,
        "expires": time.monotonic() + 120,
    }
    with ExitStack() as cleanup:
        if not issues:
            try:
                item["watch"] = _PreviewWatch(cwd, rows)
                cleanup.callback(item["watch"].close)
                # Recheck after installing watches: no preview/install blind gap.
                with lock(sid):
                    _data, latest_rows, latest_issues = _scope(sid, checkpoint_id, cwd)
                if latest_issues or latest_rows != rows:
                    issues.append("file_changed_while_opening_preview")
            except (OSError, AttributeError):
                issues.append("file_change_observation_unavailable")
        if not issues:
            with _PREVIEW_LOCK:
                for old in list(_PREVIEWS):
                    if _PREVIEWS[old]["expires"] < time.monotonic():
                        _PREVIEWS.pop(old)["watch"].close()
                if len(_PREVIEWS) >= 128:
                    _PREVIEWS.pop(next(iter(_PREVIEWS)))["watch"].close()
                _PREVIEWS[token] = item
                # Only a published preview owns the watch beyond this call.
                cleanup.pop_all()
    return {
        "checkpoint_id": checkpoint_id,
        "token": token if not issues else None,
        "can_restore": not issues,
        "expires_in": 120,
        "issues": issues,
        "paths": [
            {
                "path": r["path"],
                "action": "remove_created_file"
                if r["before"]["kind"] == "missing"
                else "restore_file",
                "current_bytes": r["expected"].get("size", 0),
            }
            for r in rows
        ],
        "source": "observed_main_agent_tool_writes",
        "sdk_dry_run": False,
        "sdk_returns_file_list": False,
        "limitations": [
            "excludes_bash_and_general_subagent_edits",
            "conversation_is_preserved",
            "external_writers_must_be_stopped",
            "sdk_has_no_atomic_compare_and_swap",
        ],
    }


def prepare_restore(
    sid: str, checkpoint_id: str, token: str, cwd: Path, runtime_marker: str
) -> dict:
    with _PREVIEW_LOCK:
        item = _PREVIEWS.pop(token, None)
    watched_change = False
    if item is not None:
        try:
            watched_change = item["watch"].changed()
        finally:
            item["watch"].close()
    if (
        not item
        or item["sid"] != sid
        or item["checkpoint_id"] != checkpoint_id
        or item["expires"] < time.monotonic()
    ):
        raise ValueError("preview_expired_or_consumed")
    if item["runtime"] != runtime_marker or item["cwd"] != str(cwd):
        raise ValueError("runtime_changed")
    with lock(sid):
        data, rows, issues = _scope(sid, checkpoint_id, cwd)
        if (
            watched_change
            or issues
            or item["generation"] != data["generation"]
            or rows != item["rows"]
        ):
            raise ValueError("preview_state_changed")
        backup = []
        for row in rows:
            body = (cwd / row["path"]).read_bytes() if row["expected"]["kind"] == "file" else None
            if fingerprint(cwd, row["path"]) != row["expected"]:
                raise ValueError("file_changed_during_backup")
            backup.append(
                {
                    "path": row["path"],
                    "body": base64.b64encode(body).decode() if body is not None else None,
                }
            )
        recovery_id = secrets.token_hex(16)
        recovery = Path(sess.SESS_DIR) / "checkpoint-recovery" / sid / f"{recovery_id}.json"
        write_private_bytes(
            recovery,
            json.dumps(
                {"workspace": str(cwd), "checkpoint_id": checkpoint_id, "files": backup}
            ).encode(),
        )
        item["recovery_id"] = recovery_id
        # Consume every previous checkpoint before dispatch: timeouts are an
        # uncertain outcome and must never allow an unreviewed second rewind.
        data["generation"] = secrets.token_hex(16)
        _save(sid, data)
        return item


def verify_restore(cwd: Path, item: dict) -> dict:
    restored, not_restored = [], []
    for row in item["rows"]:
        try:
            actual = fingerprint(cwd, row["path"])
            before = row["before"]
            ok = actual["kind"] == before["kind"] and actual.get("sha256") == before.get("sha256")
        except (OSError, ValueError):
            ok = False
        (restored if ok else not_restored).append(row["path"])
    return {
        "restored": restored,
        "not_restored": not_restored,
        "verified": not not_restored,
        "recovery_id": item["recovery_id"],
        "sdk_reported_skips": None,
        "conversation_preserved": True,
    }
