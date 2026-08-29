"""SQLite-backed workspace file index and replayable filesystem event log."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .files import INTERNAL_DIR_NAME, TRASH_DIR_NAME


_DB_NAME = "workspace-state.sqlite3"
_EXCLUDED_DIRS = frozenset({INTERNAL_DIR_NAME, TRASH_DIR_NAME})
# Explicitly prune generated, dependency, VCS, cache, and bulk-data trees. The
# directory node stays addressable through the lazy `/list` API, but recursive
# reconciliation must never walk these common multi-gigabyte subtrees.
_IGNORED_SUBTREES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".idea",
    ".cache",
    ".local",
    ".codex",
    ".claude",
    ".npm",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".hypothesis",
    ".jumbo",
    ".jumbo.bak",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "coverage",
    "target",
    "tmp",
    "output",
    "pdc_space",
    "share_space",
    "content_agent_freshdoc",
})
# A pathological workspace must yield the worker back instead of monopolizing a
# thread indefinitely. Partial scans merge only observed rows and never infer
# deletions; a later pass can still establish an authoritative full snapshot.
_SCAN_MAX_FILES = 50_000
_SCAN_MAX_SECONDS = 5.0
_EVENT_LIMIT = 20_000
# A reconciliation can discover tens of thousands of offline changes at once
# (for example when a formerly indexed cache tree becomes opaque). Replaying
# that many rows is slower than one clean snapshot and would be pruned anyway.
_RECONCILE_REPLAY_LIMIT = 500
# Compact bootstrap restores root plus previously expanded directories. Cap each
# sibling set independently so one generated/dump directory cannot turn a cold
# page load into a multi-megabyte JSON response and browser main-thread stall.
_BOOTSTRAP_CHILDREN_PER_PARENT = 500
# Automatic service startup never moves database pages. New indexes enable
# incremental auto-vacuum before creating the schema; legacy indexes report an
# explicit offline full-vacuum requirement instead of delaying readiness.
# Both thresholds must be crossed before any reclaim operation is considered.
_VACUUM_MIN_RECLAIM_BYTES = 16 * 1024 * 1024
_VACUUM_MIN_FREE_RATIO = 0.25
_INCREMENTAL_VACUUM_MAX_PAGES = 4096
_FULL_VACUUM_MIN_FREE_BYTES = 64 * 1024 * 1024


class WorkspaceScanIncomplete(RuntimeError):
    """Raised when a full reconciliation cannot safely prove deletions."""


class WorkspaceScanCancelled(RuntimeError):
    """Raised when lifecycle shutdown asks a full scan to stop."""


def database_path(primary_root: Path) -> Path:
    return primary_root.resolve() / INTERNAL_DIR_NAME / _DB_NAME


def _entry(path: str, is_dir: bool, stat: os.stat_result) -> dict[str, Any]:
    return {
        "path": path,
        "name": Path(path).name,
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "mtime": stat.st_mtime,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "inode": stat.st_ino,
    }


def _parent_path(path: str) -> str:
    """Return the normalized logical parent used by the SQLite covering index."""
    return path.rpartition("/")[0]


def is_ignored_descendant(path: str | Path) -> bool:
    """Return whether a path sits below an intentionally opaque subtree."""
    return any(
        part in _IGNORED_SUBTREES
        for part in Path(path).parts[:-1]
    )


def scan_workspace(
    root: Path,
    cancel_event: threading.Event | None = None,
    *,
    max_files: int | None = _SCAN_MAX_FILES,
    max_seconds: float | None = _SCAN_MAX_SECONDS,
    report: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return one bounded, optionally resumable filesystem snapshot pass."""
    root = root.resolve()
    scan_progress = progress if progress is not None else {}
    seen_paths: set[str] = scan_progress.setdefault("seen_paths", set())
    stack: list[tuple[Path, Path, int, bytes | None]] = scan_progress.setdefault(
        "stack", [(root, Path(), 0, None)]
    )
    resumed = bool(seen_paths)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    scanned_files = 0
    partial_reason: str | None = None
    while stack and partial_reason is None:
        if cancel_event is not None and cancel_event.is_set():
            raise WorkspaceScanCancelled("workspace scan cancelled")
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            partial_reason = "time_limit"
            break
        directory, logical_parent, skip, expected_prefix = stack.pop()
        # scandir order is not stable across passes. Hash the exact consumed
        # prefix so an order or membership change restarts instead of skipping
        # a live entry and later inferring its deletion.
        prefix = hashlib.blake2b(digest_size=16)
        prefix_verified = skip == 0
        index = 0
        try:
            with os.scandir(directory) as iterator:
                for child in iterator:
                    if child.name in _EXCLUDED_DIRS:
                        continue
                    if index < skip:
                        prefix.update(os.fsencode(child.name))
                        prefix.update(b"\0")
                        index += 1
                        if index == skip:
                            if (
                                expected_prefix is None
                                or prefix.digest() != expected_prefix
                            ):
                                scan_progress.clear()
                                raise WorkspaceScanIncomplete(
                                    f"directory changed during resumed scan: {directory}"
                                )
                            prefix_verified = True
                        continue
                    if cancel_event is not None and cancel_event.is_set():
                        raise WorkspaceScanCancelled(
                            "workspace scan cancelled"
                        )
                    if max_files is not None and scanned_files >= max_files:
                        stack.append((
                            directory,
                            logical_parent,
                            index,
                            prefix.digest(),
                        ))
                        partial_reason = "file_limit"
                        break
                    if (
                        max_seconds is not None
                        and time.monotonic() - started >= max_seconds
                    ):
                        stack.append((
                            directory,
                            logical_parent,
                            index,
                            prefix.digest(),
                        ))
                        partial_reason = "time_limit"
                        break
                    logical = logical_parent / child.name
                    try:
                        is_symlink = child.is_symlink()
                        is_dir = child.is_dir()
                        stat = child.stat(follow_symlinks=not is_symlink)
                    except OSError as exc:
                        scan_progress.clear()
                        # A disappearing or temporarily unreadable entry makes
                        # this snapshot non-authoritative for deletion.
                        raise WorkspaceScanIncomplete(str(child.path)) from exc
                    prefix.update(os.fsencode(child.name))
                    prefix.update(b"\0")
                    index += 1
                    path = logical.as_posix()
                    seen_paths.add(path)
                    rows.append(_entry(path, is_dir, stat))
                    scanned_files += 1
                    if (
                        is_dir
                        and not is_symlink
                        and child.name not in _IGNORED_SUBTREES
                    ):
                        stack.append((Path(child.path), logical, 0, None))
            if not prefix_verified:
                scan_progress.clear()
                raise WorkspaceScanIncomplete(
                    f"directory changed during resumed scan: {directory}"
                )
        except OSError as exc:
            scan_progress.clear()
            # Keep the last-good index instead of inventing deletes for an
            # unreadable subtree.
            raise WorkspaceScanIncomplete(str(directory)) from exc
    rows.sort(key=lambda row: row["path"])
    partial = partial_reason is not None
    snapshot_files = len(seen_paths)
    complete_paths = None if partial else set(seen_paths)
    if not partial:
        scan_progress.clear()
    if report is not None:
        report.update({
            "partial": partial,
            "partial_reason": partial_reason,
            "scanned_files": scanned_files,
            "snapshot_files": snapshot_files,
            "resumed": resumed,
            "scan_ms": int((time.monotonic() - started) * 1000),
        })
        if complete_paths is not None:
            report["_snapshot_paths"] = complete_paths
    return rows


ScanRow = tuple[str, bool, int, float, int, int, int]


def compact_scan_rows(rows: Iterable[dict[str, Any]]) -> list[ScanRow]:
    """Encode filesystem rows compactly for scanner-process IPC."""
    return [
        (
            row["path"],
            bool(row["is_dir"]),
            int(row["size"]),
            float(row["mtime"]),
            int(row["mtime_ns"]),
            int(row["ctime_ns"]),
            int(row["inode"]),
        )
        for row in rows
    ]


def expand_scan_rows(
    rows: Iterable[ScanRow | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Decode compact IPC rows while accepting direct in-process test rows."""
    expanded: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            expanded.append(dict(row))
            continue
        path, is_dir, size, mtime, mtime_ns, ctime_ns, inode = row
        expanded.append({
            "path": path,
            "name": Path(path).name,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
            "mtime_ns": mtime_ns,
            "ctime_ns": ctime_ns,
            "inode": inode,
        })
    return expanded


def workspace_scan_worker(connection: Any, cancel_event: Any) -> None:
    """Serve compact scan requests in one reusable spawned worker process."""
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            root, max_files, max_seconds, progress = request
            report: dict[str, Any] = {}
            try:
                rows = scan_workspace(
                    Path(root),
                    cancel_event=cancel_event,
                    max_files=max_files,
                    max_seconds=max_seconds,
                    report=report,
                    progress=progress,
                )
                connection.send(("ok", compact_scan_rows(rows), report, progress))
            except BaseException as exc:
                connection.send(("error", type(exc).__name__, str(exc), progress))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class WorkspaceStore:
    """Serialize per-workspace mutations into contiguous SQLite transactions."""

    def __init__(
        self,
        primary_root: Path,
        *,
        event_limit: int = _EVENT_LIMIT,
    ) -> None:
        self.primary_root = primary_root.resolve()
        self.path = database_path(self.primary_root)
        self.event_limit = max(100, event_limit)
        self._lock = threading.RLock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        # Native watchers may emit duplicate `modified` notifications while this
        # host filesystem exposes only second-resolution ctime/mtime. Keep a
        # bounded-content fingerprint for touched files so a real same-metadata
        # rewrite is not dropped, while an immediate duplicate remains a no-op.
        self._native_content_signatures: dict[tuple[str, str], str] = {}
        self._ready = False

    def initialize(self) -> None:
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                # WAL is persistent for the database. Setting it once here
                # avoids taking the journal-mode lock on every short-lived
                # read connection used by bootstrap/delta hot paths.
                db.execute("PRAGMA journal_mode = WAL")
                # New databases adopt incremental auto-vacuum before their
                # schema is created. Legacy databases only switch through an
                # explicitly opted-in offline maintain_database() call; normal
                # service startup never performs a full VACUUM.
                db.execute("PRAGMA auto_vacuum = INCREMENTAL")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id TEXT PRIMARY KEY,
                        path TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        primary_workspace INTEGER NOT NULL DEFAULT 0,
                        initialized INTEGER NOT NULL DEFAULT 0,
                        current_seq INTEGER NOT NULL DEFAULT 0,
                        scanned_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS files (
                        workspace_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        parent TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL,
                        is_dir INTEGER NOT NULL,
                        size INTEGER NOT NULL,
                        mtime REAL NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        ctime_ns INTEGER NOT NULL DEFAULT 0,
                        inode INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (workspace_id, path),
                        FOREIGN KEY (workspace_id)
                            REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        workspace_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        type TEXT NOT NULL,
                        path TEXT NOT NULL,
                        name TEXT,
                        is_dir INTEGER,
                        size INTEGER,
                        mtime REAL,
                        mtime_ns INTEGER,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (workspace_id, seq),
                        FOREIGN KEY (workspace_id)
                            REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS events_workspace_path
                    ON events(workspace_id, path);
                    """
                )
                columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(files)")
                }
                if "ctime_ns" not in columns:
                    db.execute(
                        "ALTER TABLE files "
                        "ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 0"
                    )
                if "inode" not in columns:
                    db.execute(
                        "ALTER TABLE files "
                        "ADD COLUMN inode INTEGER NOT NULL DEFAULT 0"
                    )
                if "parent" not in columns:
                    db.execute(
                        "ALTER TABLE files "
                        "ADD COLUMN parent TEXT NOT NULL DEFAULT ''"
                    )
                    # `name` is already the path basename, so this backfill is
                    # deterministic and stays inside one SQLite transaction.
                    db.execute(
                        """
                        UPDATE files
                        SET parent = CASE
                            WHEN instr(path, '/') = 0 THEN ''
                            ELSE substr(path, 1, length(path) - length(name) - 1)
                        END
                        """
                    )
                db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS files_workspace_parent_path
                    ON files(workspace_id, parent, path)
                    """
                )
                db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS files_workspace_parent_kind_name
                    ON files(
                        workspace_id,
                        parent,
                        is_dir DESC,
                        name COLLATE NOCASE,
                        name,
                        path
                    )
                    """
                )
            self._secure_database_files()
            self._ready = True

    def maintain_database(
        self,
        *,
        allow_full_vacuum: bool = False,
    ) -> dict[str, Any]:
        """Reclaim freelist bloat only when explicitly invoked.

        New databases use incremental auto-vacuum from their first schema
        transaction. A legacy database needs one full VACUUM to switch modes,
        but that operation is never implicit: it can copy the whole database
        and must not sit on the service readiness path. An offline maintenance
        command may opt in after stopping MuseLab; even then, require enough
        free space for both the original and replacement database.
        """
        self.initialize()
        started = time.monotonic()
        required_headroom = 0
        with self._lock, self._connect() as db:
            before = self._database_stats(db)
            should_reclaim = (
                before["reclaimable_bytes"] >= _VACUUM_MIN_RECLAIM_BYTES
                and before["free_ratio"] >= _VACUUM_MIN_FREE_RATIO
            )
            action = "none"
            full_vacuum_required = False
            if should_reclaim and before["auto_vacuum"] != 2:
                full_vacuum_required = True
                action = "full-required"
                if allow_full_vacuum:
                    database_bytes = self.path.stat().st_size
                    required_headroom = max(
                        database_bytes * 2,
                        _FULL_VACUUM_MIN_FREE_BYTES,
                    )
                    free_bytes = shutil.disk_usage(self.path.parent).free
                    if free_bytes >= required_headroom:
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                        db.execute("PRAGMA auto_vacuum = INCREMENTAL")
                        db.execute("VACUUM")
                        action = "full"
                        full_vacuum_required = False
                    else:
                        action = "full-skipped-no-space"
            elif should_reclaim:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                pages = min(
                    before["freelist_count"],
                    _INCREMENTAL_VACUUM_MAX_PAGES,
                )
                db.execute(f"PRAGMA incremental_vacuum({pages})")
                action = "incremental"
            after = self._database_stats(db)
        self._secure_database_files()
        return {
            "action": action,
            "before": before,
            "after": after,
            "full_vacuum_required": full_vacuum_required,
            "required_headroom_bytes": required_headroom,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        }

    @staticmethod
    def _database_stats(db: sqlite3.Connection) -> dict[str, Any]:
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        auto_vacuum = int(db.execute("PRAGMA auto_vacuum").fetchone()[0])
        return {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "reclaimable_bytes": page_size * freelist_count,
            "free_ratio": (
                round(freelist_count / page_count, 4)
                if page_count
                else 0.0
            ),
            "auto_vacuum": auto_vacuum,
        }

    def register_workspace(
        self,
        workspace_id: str,
        root: Path,
        name: str,
        *,
        primary: bool = False,
    ) -> None:
        self.initialize()
        resolved_root = str(root.resolve())
        primary_value = int(primary)
        with self._workspace_lock(workspace_id), self._connect() as db:
            # A remove/re-add can allocate a fresh generation for the same
            # path.  If the previous process exited after updating the
            # registry but before deleting its SQLite row, recover here
            # instead of failing the new registration on UNIQUE(path).
            db.execute(
                "DELETE FROM workspaces WHERE path = ? AND id <> ?",
                (resolved_root, workspace_id),
            )
            current = db.execute(
                """
                SELECT path, name, primary_workspace
                FROM workspaces WHERE id = ?
                """,
                (workspace_id,),
            ).fetchone()
            if (
                current is not None
                and current["path"] == resolved_root
                and current["name"] == name
                and int(current["primary_workspace"]) == primary_value
            ):
                return
            db.execute(
                """
                INSERT INTO workspaces(id, path, name, primary_workspace)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path,
                    name=excluded.name,
                    primary_workspace=excluded.primary_workspace
                """,
                (workspace_id, resolved_root, name, primary_value),
            )

    def remove_workspace(self, workspace_id: str) -> None:
        self.initialize()
        with self._workspace_lock(workspace_id):
            with self._connect() as db:
                db.execute(
                    "DELETE FROM workspaces WHERE id = ?",
                    (workspace_id,),
                )

    def state(self, workspace_id: str) -> dict[str, Any]:
        """Return lightweight initialization and cursor state."""
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT initialized, current_seq
                FROM workspaces WHERE id = ?
                """,
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown workspace: {workspace_id}")
        return {
            "initialized": bool(row["initialized"]),
            "cursor": int(row["current_seq"]),
        }

    def current_cursor(self, workspace_id: str) -> int:
        return int(self.state(workspace_id)["cursor"])

    def watch_directories(
        self,
        workspace_id: str,
        root: Path,
    ) -> tuple[Path, ...]:
        """Return indexed real directories suitable for shallow OS watches.

        `watchfiles` applies its Python filter after the native backend has
        registered recursive watches.  Passing the selected directories
        explicitly with ``recursive=False`` is what actually keeps opaque
        trees such as ``.local`` and ``node_modules`` out of inotify/polling.
        """
        self.initialize()
        root = root.resolve()
        with self._connect() as db:
            known = db.execute(
                """
                SELECT path
                FROM files
                WHERE workspace_id = ? AND is_dir = 1
                ORDER BY path
                """,
                (workspace_id,),
            ).fetchall()

        directories: list[Path] = [root]
        seen = {root}
        for row in known:
            relative = Path(row["path"])
            if (
                not relative.parts
                or relative.name in _IGNORED_SUBTREES
                or is_ignored_descendant(relative)
            ):
                continue
            candidate = root / relative
            try:
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
            except OSError:
                continue
            if candidate not in seen:
                seen.add(candidate)
                directories.append(candidate)
        return tuple(directories)

    def reconcile(
        self,
        workspace_id: str,
        root: Path,
        name: str,
        *,
        primary: bool = False,
        cancel_event: threading.Event | None = None,
        max_files: int | None = _SCAN_MAX_FILES,
        max_seconds: float | None = _SCAN_MAX_SECONDS,
        report: dict[str, Any] | None = None,
        scan_progress: dict[str, Any] | None = None,
        snapshot: list[dict[str, Any]] | None = None,
        expected_cursor: int | None = None,
        return_payload: bool = False,
    ) -> int | dict[str, Any]:
        """Scan or apply a snapshot, logging offline changes atomically."""
        root = root.resolve()
        scan_report: dict[str, Any] = report if report is not None else {}
        # Filesystem walking may happen in a separate process. The workspace lock
        # serializes only durable application and native watcher transactions.
        with self._workspace_lock(workspace_id):
            if snapshot is None:
                snapshot = scan_workspace(
                    root,
                    cancel_event=cancel_event,
                    max_files=max_files,
                    max_seconds=max_seconds,
                    report=scan_report,
                    progress=scan_progress,
                )
            partial = bool(scan_report.get("partial"))
            if cancel_event is not None and cancel_event.is_set():
                raise WorkspaceScanCancelled("workspace scan cancelled")
            if expected_cursor is None:
                self.register_workspace(
                    workspace_id,
                    root,
                    name,
                    primary=primary,
                )
            if cancel_event is not None and cancel_event.is_set():
                raise WorkspaceScanCancelled("workspace scan cancelled")
            with self._connect() as db:
                if cancel_event is not None and cancel_event.is_set():
                    raise WorkspaceScanCancelled("workspace scan cancelled")
                db.execute("BEGIN IMMEDIATE")
                state = db.execute(
                    """
                    SELECT initialized, current_seq, path
                    FROM workspaces WHERE id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                if state is None:
                    db.rollback()
                    raise KeyError(f"unknown workspace: {workspace_id}")
                initialized = bool(state["initialized"])
                seq = int(state["current_seq"])
                if expected_cursor is not None and (
                    seq != expected_cursor
                    or state["path"] != str(root)
                ):
                    db.rollback()
                    return {
                        "_stale": True,
                        "cursor": seq,
                        "changes": [],
                        "resync": True,
                    }
                old = {
                    row["path"]: row
                    for row in self._file_rows(db, workspace_id)
                }
                new = {row["path"]: row for row in snapshot}
                complete_paths = scan_report.pop(
                    "_snapshot_paths",
                    set(new),
                )
                resumed = bool(scan_report.get("resumed"))

                changes: list[dict[str, Any]] = []
                resync = False
                if not initialized:
                    # A failed first scan may have left watcher-created rows. A
                    # complete baseline replaces them authoritatively; a bounded
                    # partial scan only merges observations because absence was
                    # not proven for the unvisited remainder.
                    if not partial:
                        db.execute(
                            "DELETE FROM files WHERE workspace_id = ?",
                            (workspace_id,),
                        )
                        self._insert_files(db, workspace_id, snapshot)
                    else:
                        for row in snapshot:
                            self._upsert_file(db, workspace_id, row)
                else:
                    deleted = (
                        []
                        if partial
                        else sorted(old.keys() - complete_paths)
                    )
                    added = sorted(new.keys() - old.keys())
                    modified = sorted(
                        path
                        for path in old.keys() & new.keys()
                        if self._signature(old[path])
                        != self._signature(new[path])
                    )
                    change_count = len(deleted) + len(added) + len(modified)
                    if (
                        change_count > self._replay_limit()
                        and not partial
                        and not resumed
                    ):
                        # A complete one-pass snapshot can replace the index in
                        # bulk. Resumed/partial passes contain only this pass's
                        # metadata, so they must preserve rows observed earlier.
                        db.execute(
                            "DELETE FROM files WHERE workspace_id = ?",
                            (workspace_id,),
                        )
                        self._insert_files(db, workspace_id, snapshot)
                        seq = self._reset_replay(db, workspace_id, seq)
                        resync = True
                    else:
                        if deleted:
                            db.executemany(
                                """
                                DELETE FROM files
                                WHERE workspace_id = ? AND path = ?
                                """,
                                ((workspace_id, path) for path in deleted),
                            )
                        for path in added + modified:
                            self._upsert_file(
                                db,
                                workspace_id,
                                new[path],
                            )
                        if change_count > self._replay_limit():
                            # Preserve the incrementally assembled index but use
                            # a cursor gap instead of retaining a huge replay.
                            seq = self._reset_replay(db, workspace_id, seq)
                            resync = True
                        else:
                            changes.extend(
                                {"type": "deleted", "path": path}
                                for path in deleted
                            )
                            changes.extend(
                                {"type": "added", **new[path]}
                                for path in added
                            )
                            changes.extend(
                                {"type": "modified", **new[path]}
                                for path in modified
                            )
                            seq = self._append_events(
                                db,
                                workspace_id,
                                seq,
                                changes,
                            )

                # Cancellation cannot pre-empt a single sqlite C call, but it
                # can prevent a fully computed, now-stale snapshot from being
                # committed after runtime shutdown has begun. Checks bracket
                # the final metadata/prune work so an exception rolls the
                # explicit transaction back via the connection context.
                if cancel_event is not None and cancel_event.is_set():
                    raise WorkspaceScanCancelled("workspace scan cancelled")
                db.execute(
                    """
                    UPDATE workspaces
                    SET initialized = 1, current_seq = ?, scanned_at = ?
                    WHERE id = ?
                    """,
                    (seq, time.time(), workspace_id),
                )
                self._prune(db, workspace_id, seq)
                if cancel_event is not None and cancel_event.is_set():
                    raise WorkspaceScanCancelled("workspace scan cancelled")
                db.commit()
                if return_payload:
                    return {
                        "cursor": seq,
                        "changes": [] if resync else self._wire_changes(changes),
                        "resync": resync,
                    }
                return seq

    def apply_reconcile_snapshot(
        self,
        workspace_id: str,
        root: Path,
        name: str,
        snapshot: Iterable[ScanRow],
        scan_report: dict[str, Any],
        *,
        expected_cursor: int,
        primary: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Verify the durable cursor, apply a scan, and return its exact payload."""
        result = self.reconcile(
            workspace_id,
            root,
            name,
            primary=primary,
            cancel_event=cancel_event,
            report=scan_report,
            snapshot=expand_scan_rows(snapshot),
            expected_cursor=expected_cursor,
            return_payload=True,
        )
        assert isinstance(result, dict)
        return result

    def apply_changes(
        self,
        workspace_id: str,
        root: Path,
        changes: Iterable[dict[str, str]],
    ) -> dict[str, Any]:
        """Apply one native watcher batch and allocate seqs atomically."""
        root = root.resolve()
        kinds_by_path: dict[str, set[str]] = {}
        for change in changes:
            kind = str(change.get("type") or "")
            if kind not in {"added", "modified", "deleted"}:
                continue
            relative = Path(str(change.get("path") or "").strip("/"))
            if (
                not relative.parts
                or any(
                    part in {"", ".", ".."} or part in _EXCLUDED_DIRS
                    for part in relative.parts
                )
                or is_ignored_descendant(relative)
            ):
                continue
            path = relative.as_posix()
            kinds_by_path.setdefault(path, set()).add(kind)

        if not kinds_by_path:
            cursor = self.current_cursor(workspace_id)
            return {"cursor": cursor, "changes": []}

        with self._workspace_lock(workspace_id):
            self.initialize()
            with self._connect() as db:
                existing_at_start = {
                    path: db.execute(
                        """
                        SELECT path, name, is_dir, size, mtime, mtime_ns,
                               ctime_ns, inode
                        FROM files
                        WHERE workspace_id = ? AND path = ?
                        """,
                        (workspace_id, path),
                    ).fetchone()
                    for path in kinds_by_path
                }

            prepared: dict[str, dict[str, Any]] = {}
            missing: list[str] = []
            covered_missing: list[str] = []
            scanned_subtrees: dict[str, set[str]] = {}
            subtree_scan_incomplete = False

            for path in sorted(
                kinds_by_path,
                key=lambda value: (value.count("/"), value),
            ):
                if any(
                    path == parent or path.startswith(parent + "/")
                    for parent in covered_missing
                ):
                    continue
                target = root / path
                try:
                    is_symlink = target.is_symlink()
                    stat = target.stat(follow_symlinks=not is_symlink)
                    is_dir = target.is_dir()
                except FileNotFoundError:
                    covered_missing.append(path)
                    missing.append(path)
                    continue
                except OSError:
                    # A permission race is not proof of deletion.
                    continue

                prepared[path] = _entry(path, is_dir, stat)
                if (
                    "added" not in kinds_by_path[path]
                    or not is_dir
                    or is_symlink
                    or Path(path).name in _IGNORED_SUBTREES
                ):
                    continue

                # A native watcher may report only the directory node when a
                # populated tree is moved into the workspace. Recurse for an
                # add/replace, never for an ordinary directory "modified"
                # notification (the source of the former O(subtree) storm).
                try:
                    descendants = scan_workspace(target)
                except WorkspaceScanIncomplete:
                    # An unreadable/transiently changing replacement is not
                    # evidence that every formerly indexed child disappeared.
                    # Keep the last-good descendants and let reconciliation
                    # retry instead of emitting a false mass deletion.
                    subtree_scan_incomplete = True
                    continue
                current_paths = {path}
                for row in descendants:
                    child_path = (Path(path) / row["path"]).as_posix()
                    prepared[child_path] = {
                        **row,
                        "path": child_path,
                        "name": Path(child_path).name,
                    }
                    current_paths.add(child_path)
                scanned_subtrees[path] = current_paths

            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                state = db.execute(
                    """
                    SELECT current_seq
                    FROM workspaces WHERE id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                if state is None:
                    db.rollback()
                    raise KeyError(f"unknown workspace: {workspace_id}")
                seq = int(state["current_seq"])
                emitted: list[dict[str, Any]] = []
                deleted_seen: set[str] = set()
                watch_refresh = False

                # If a directory was atomically replaced, remove descendants
                # that were present in the old index but absent on disk.
                for parent, current_paths in scanned_subtrees.items():
                    if existing_at_start.get(parent) is None:
                        continue
                    stale_rows = [
                        row
                        for row in self._subtree_rows(
                            db,
                            workspace_id,
                            parent,
                        )
                        if row["path"] not in current_paths
                    ]
                    stale = [row["path"] for row in stale_rows]
                    if stale:
                        watch_refresh = (
                            watch_refresh
                            or any(bool(row["is_dir"]) for row in stale_rows)
                        )
                        db.executemany(
                            """
                            DELETE FROM files
                            WHERE workspace_id = ? AND path = ?
                            """,
                            ((workspace_id, path) for path in stale),
                        )
                        for path in stale:
                            if path not in deleted_seen:
                                deleted_seen.add(path)
                                emitted.append({
                                    "type": "deleted",
                                    "path": path,
                                })

                for path in missing:
                    known = self._subtree_rows(db, workspace_id, path)
                    if not known:
                        # Duplicate native deletes must not consume the replay
                        # window or invalidate a perfectly usable client cursor.
                        continue
                    db.execute(
                        """
                        DELETE FROM files
                        WHERE workspace_id = ?
                          AND (path = ? OR path LIKE ? ESCAPE '\\')
                        """,
                        (
                            workspace_id,
                            path,
                            self._subtree_pattern(path),
                        ),
                    )
                    for item in known:
                        deleted_path = item["path"]
                        if deleted_path in deleted_seen:
                            continue
                        deleted_seen.add(deleted_path)
                        watch_refresh = watch_refresh or bool(item["is_dir"])
                        emitted.append({
                            "type": "deleted",
                            "path": deleted_path,
                        })

                for path, row in sorted(
                    prepared.items(),
                    key=lambda item: (
                        item[0].count("/"),
                        item[0],
                    ),
                ):
                    old = db.execute(
                        """
                        SELECT path, name, is_dir, size, mtime, mtime_ns,
                               ctime_ns, inode
                        FROM files
                        WHERE workspace_id = ? AND path = ?
                        """,
                        (workspace_id, path),
                    ).fetchone()
                    force_file_modified = False
                    if (
                        not row["is_dir"]
                        and "modified" in kinds_by_path.get(path, set())
                    ):
                        try:
                            content_signature = self._content_signature(
                                root / path, int(row["size"]),
                            )
                        except OSError:
                            content_signature = ""
                        cache_key = (workspace_id, path)
                        force_file_modified = (
                            not content_signature
                            or self._native_content_signatures.get(cache_key)
                            != content_signature
                        )
                        if content_signature:
                            self._native_content_signatures[cache_key] = content_signature
                    if (
                        old is not None
                        and self._signature(old) == self._signature(row)
                        and not force_file_modified
                    ):
                        continue
                    watch_refresh = (
                        watch_refresh
                        or (
                            bool(row["is_dir"])
                            and (
                                old is None
                                or not bool(old["is_dir"])
                            )
                        )
                        or (
                            old is not None
                            and bool(old["is_dir"]) != bool(row["is_dir"])
                        )
                    )

                    # Replacing a directory with a file makes every old child
                    # stale even if the backend emits no separate child deletes.
                    if old is not None and bool(old["is_dir"]) and not row["is_dir"]:
                        descendants = [
                            item
                            for item in self._subtree_rows(
                                db,
                                workspace_id,
                                path,
                            )
                            if item["path"] != path
                        ]
                        if descendants:
                            db.executemany(
                                """
                                DELETE FROM files
                                WHERE workspace_id = ? AND path = ?
                                """,
                                (
                                    (workspace_id, item["path"])
                                    for item in descendants
                                ),
                            )
                            for item in descendants:
                                deleted_path = item["path"]
                                if deleted_path not in deleted_seen:
                                    deleted_seen.add(deleted_path)
                                    emitted.append({
                                        "type": "deleted",
                                        "path": deleted_path,
                                    })

                    event = {
                        **row,
                        "type": "modified" if old is not None else "added",
                    }
                    self._upsert_file(db, workspace_id, row)
                    emitted.append(event)

                resync = len(emitted) > self._replay_limit()
                if resync:
                    seq = self._reset_replay(db, workspace_id, seq)
                else:
                    seq = self._append_events(
                        db,
                        workspace_id,
                        seq,
                        emitted,
                    )
                db.execute(
                    """
                    UPDATE workspaces
                    SET current_seq = ? WHERE id = ?
                    """,
                    (seq, workspace_id),
                )
                self._prune(db, workspace_id, seq)
                db.commit()

        payload = {
            "cursor": seq,
            "changes": [] if resync else self._wire_changes(emitted),
        }
        if resync:
            payload["resync"] = True
        if watch_refresh:
            payload["_watch_refresh"] = True
        if subtree_scan_incomplete:
            payload["_reconcile"] = True
        return payload

    def bootstrap(
        self,
        workspace_id: str,
        *,
        show_hidden: bool = True,
        parents: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Return a cursor-consistent file snapshot.

        ``parents is None`` preserves the legacy full-tree response. Passing a
        collection returns only root children and direct children of the
        expanded parents, which keeps refresh and cold workspace switches
        bounded even for very large indexes.
        """
        self.initialize()
        normalized_parents = (
            None
            if parents is None
            else self._expanded_parent_paths(parents)
        )
        with self._connect() as db:
            db.execute("BEGIN")
            try:
                workspace = db.execute(
                    """
                    SELECT id, path, name, primary_workspace, current_seq
                    FROM workspaces WHERE id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                if workspace is None:
                    raise KeyError(f"unknown workspace: {workspace_id}")
                truncated_parents: list[str] = []
                if normalized_parents is None:
                    rows = self._file_rows(
                        db,
                        workspace_id,
                        show_hidden=show_hidden,
                    )
                else:
                    rows, truncated_parents = self._file_rows_for_parents(
                        db,
                        workspace_id,
                        normalized_parents,
                        show_hidden=show_hidden,
                    )
                entries = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"ctime_ns", "inode"}
                    }
                    for row in rows
                ]
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return {
            "workspace_id": workspace["id"],
            "root": workspace["path"],
            "name": workspace["name"],
            "primary": bool(workspace["primary_workspace"]),
            "cursor": workspace["current_seq"],
            "entries": entries,
            **(
                {
                    "partial": True,
                    "parents": normalized_parents,
                    "truncated_parents": truncated_parents,
                    "children_per_parent_limit": _BOOTSTRAP_CHILDREN_PER_PARENT,
                }
                if normalized_parents is not None
                else {}
            ),
        }

    def delta(
        self,
        workspace_id: str,
        cursor: int,
        *,
        limit: int = 2000,
    ) -> dict[str, Any]:
        self.initialize()
        cursor = max(0, int(cursor))
        limit = max(1, min(int(limit), 5000))
        with self._connect() as db:
            db.execute("BEGIN")
            try:
                state = db.execute(
                    """
                    SELECT current_seq
                    FROM workspaces WHERE id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                if state is None:
                    raise KeyError(f"unknown workspace: {workspace_id}")
                current = int(state["current_seq"])
                oldest = db.execute(
                    """
                    SELECT MIN(seq)
                    FROM events WHERE workspace_id = ?
                    """,
                    (workspace_id,),
                ).fetchone()[0]
                needs_resync = (
                    cursor > current
                    or (oldest is None and cursor < current)
                    or (
                        oldest is not None
                        and cursor < int(oldest) - 1
                    )
                )
                rows = [] if needs_resync else db.execute(
                    """
                    SELECT seq, type, path, name, is_dir, size, mtime, mtime_ns
                    FROM events
                    WHERE workspace_id = ? AND seq > ?
                    ORDER BY seq
                    LIMIT ?
                    """,
                    (workspace_id, cursor, limit + 1),
                ).fetchall()
                db.commit()
            except BaseException:
                db.rollback()
                raise
        if needs_resync:
            return {
                "workspace_id": workspace_id,
                "cursor": current,
                "changes": [],
                "resync": True,
                "has_more": False,
            }
        has_more = len(rows) > limit
        rows = rows[:limit]
        changes = [self._event_wire(row) for row in rows]
        next_cursor = changes[-1]["seq"] if changes else cursor
        return {
            "workspace_id": workspace_id,
            "cursor": next_cursor,
            "changes": changes,
            "resync": False,
            "has_more": has_more,
        }

    def close(self) -> None:
        # Connections are deliberately short-lived; reset lazy init for tests.
        with self._lock:
            self._ready = False

    def _workspace_lock(self, workspace_id: str) -> threading.RLock:
        with self._lock:
            return self._workspace_locks.setdefault(
                workspace_id,
                threading.RLock(),
            )

    def _replay_limit(self) -> int:
        return min(self.event_limit, _RECONCILE_REPLAY_LIMIT)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = NORMAL")
        return db

    def _secure_database_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                continue

    @staticmethod
    def _content_signature(path: Path, size: int) -> str:
        """Hash bounded samples from one watcher-touched file."""
        chunk = 64 * 1024
        digest = hashlib.blake2b(digest_size=16)
        digest.update(str(size).encode("ascii"))
        with path.open("rb") as handle:
            if size <= chunk * 3:
                digest.update(handle.read())
            else:
                digest.update(handle.read(chunk))
                handle.seek(max(chunk, size // 2 - chunk // 2))
                digest.update(handle.read(chunk))
                handle.seek(max(0, size - chunk))
                digest.update(handle.read(chunk))
        return digest.hexdigest()

    @staticmethod
    def _signature(row: dict[str, Any] | sqlite3.Row) -> tuple[Any, ...]:
        return (
            row["is_dir"],
            row["size"],
            row["mtime_ns"],
            row["ctime_ns"],
            row["inode"],
        )

    @staticmethod
    def _file_rows(
        db: sqlite3.Connection,
        workspace_id: str,
        *,
        show_hidden: bool = True,
    ) -> list[dict[str, Any]]:
        hidden_clause = ""
        if not show_hidden:
            # SQLite GLOB's `*` spans slashes. Together these exclude a dot-name
            # at the root or at any deeper path without materializing all rows.
            hidden_clause = (
                " AND path NOT GLOB '.*'"
                " AND path NOT GLOB '*/.*'"
            )
        rows = db.execute(
            f"""
            SELECT path, name, is_dir, size, mtime, mtime_ns, ctime_ns, inode
            FROM files
            WHERE workspace_id = ?{hidden_clause}
            ORDER BY path
            """,
            (workspace_id,),
        ).fetchall()
        return [
            {
                "path": row["path"],
                "name": row["name"],
                "is_dir": bool(row["is_dir"]),
                "size": row["size"],
                "mtime": row["mtime"],
                "mtime_ns": row["mtime_ns"],
                "ctime_ns": row["ctime_ns"],
                "inode": row["inode"],
            }
            for row in rows
        ]

    @staticmethod
    def _expanded_parent_paths(parents: Iterable[str]) -> list[str]:
        """Normalize expanded paths and retain their ancestor chain.

        The API layer rejects malformed input. This defensive normalization is
        also used by direct callers and ensures a compact snapshot can always
        materialize every requested parent from the root down.
        """
        expanded: set[str] = set()
        for raw in parents:
            value = str(raw).strip()
            if not value or value.startswith(("/", "\\")) or "\\" in value:
                continue
            parts = value.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                continue
            for depth in range(1, len(parts) + 1):
                expanded.add("/".join(parts[:depth]))
        # Keep SQLite bind counts bounded. Sorting shallow-first guarantees an
        # included descendant never loses the ancestor needed to render it.
        return sorted(
            expanded,
            key=lambda path: (path.count("/"), path),
        )[:100]

    @staticmethod
    def _file_rows_for_parents(
        db: sqlite3.Connection,
        workspace_id: str,
        parents: Iterable[str],
        *,
        show_hidden: bool = True,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read bounded children for root and each expanded directory.

        The parent-count bound alone is insufficient: a single generated
        directory can contain tens of thousands of direct children. Query each
        selected parent through the directory-order index with a hard sibling
        cap so compact bootstrap stays bounded per expanded directory.
        """
        selected_parents = ["", *dict.fromkeys(parents)]
        hidden_clause = ""
        if not show_hidden:
            hidden_clause = (
                " AND path NOT GLOB '.*'"
                " AND path NOT GLOB '*/.*'"
            )
        rows: list[sqlite3.Row] = []
        truncated_parents: list[str] = []
        for parent in selected_parents:
            parent_rows = db.execute(
                f"""
                SELECT path, name, is_dir, size, mtime, mtime_ns,
                       ctime_ns, inode
                FROM files INDEXED BY files_workspace_parent_kind_name
                WHERE workspace_id = ? AND parent = ?{hidden_clause}
                ORDER BY is_dir DESC, name COLLATE NOCASE, name, path
                LIMIT ?
                """,
                (
                    workspace_id,
                    parent,
                    _BOOTSTRAP_CHILDREN_PER_PARENT + 1,
                ),
            ).fetchall()
            if len(parent_rows) > _BOOTSTRAP_CHILDREN_PER_PARENT:
                truncated_parents.append(parent)
            rows.extend(parent_rows[:_BOOTSTRAP_CHILDREN_PER_PARENT])
        entries = [
            {
                "path": row["path"],
                "name": row["name"],
                "is_dir": bool(row["is_dir"]),
                "size": row["size"],
                "mtime": row["mtime"],
                "mtime_ns": row["mtime_ns"],
                "ctime_ns": row["ctime_ns"],
                "inode": row["inode"],
            }
            for row in rows
        ]
        return entries, truncated_parents

    @staticmethod
    def _subtree_pattern(path: str) -> str:
        escaped = (
            path
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return escaped + "/%"

    @classmethod
    def _subtree_rows(
        cls,
        db: sqlite3.Connection,
        workspace_id: str,
        path: str,
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            """
            SELECT path, name, is_dir, size, mtime, mtime_ns, ctime_ns, inode
            FROM files
            WHERE workspace_id = ?
              AND (path = ? OR path LIKE ? ESCAPE '\\')
            ORDER BY length(path) DESC, path
            """,
            (
                workspace_id,
                path,
                cls._subtree_pattern(path),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_files(
        db: sqlite3.Connection,
        workspace_id: str,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        db.executemany(
            """
            INSERT INTO files(
                workspace_id, path, parent, name, is_dir, size, mtime, mtime_ns,
                ctime_ns, inode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    workspace_id,
                    row["path"],
                    _parent_path(row["path"]),
                    row["name"],
                    int(row["is_dir"]),
                    row["size"],
                    row["mtime"],
                    row["mtime_ns"],
                    row["ctime_ns"],
                    row["inode"],
                )
                for row in rows
            ),
        )

    @staticmethod
    def _upsert_file(
        db: sqlite3.Connection,
        workspace_id: str,
        row: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO files(
                workspace_id, path, parent, name, is_dir, size, mtime, mtime_ns,
                ctime_ns, inode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, path) DO UPDATE SET
                parent=excluded.parent,
                name=excluded.name,
                is_dir=excluded.is_dir,
                size=excluded.size,
                mtime=excluded.mtime,
                mtime_ns=excluded.mtime_ns,
                ctime_ns=excluded.ctime_ns,
                inode=excluded.inode
            """,
            (
                workspace_id,
                row["path"],
                _parent_path(row["path"]),
                row["name"],
                int(row["is_dir"]),
                row["size"],
                row["mtime"],
                row["mtime_ns"],
                row["ctime_ns"],
                row["inode"],
            ),
        )

    @staticmethod
    def _append_events(
        db: sqlite3.Connection,
        workspace_id: str,
        seq: int,
        changes: Iterable[dict[str, Any]],
    ) -> int:
        now = time.time()
        for change in changes:
            seq += 1
            change["seq"] = seq
            db.execute(
                """
                INSERT INTO events(
                    workspace_id, seq, type, path, name, is_dir, size,
                    mtime, mtime_ns, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    seq,
                    change["type"],
                    change["path"],
                    change.get("name"),
                    (
                        int(change["is_dir"])
                        if "is_dir" in change
                        else None
                    ),
                    change.get("size"),
                    change.get("mtime"),
                    change.get("mtime_ns"),
                    now,
                ),
            )
        return seq

    @staticmethod
    def _reset_replay(
        db: sqlite3.Connection,
        workspace_id: str,
        seq: int,
    ) -> int:
        db.execute(
            "DELETE FROM events WHERE workspace_id = ?",
            (workspace_id,),
        )
        return seq + 1

    def _prune(
        self,
        db: sqlite3.Connection,
        workspace_id: str,
        current_seq: int,
    ) -> None:
        cutoff = current_seq - self.event_limit
        if cutoff > 0:
            db.execute(
                """
                DELETE FROM events
                WHERE workspace_id = ? AND seq <= ?
                """,
                (workspace_id, cutoff),
            )

    @staticmethod
    def _wire_changes(
        changes: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"ctime_ns", "inode"}
            }
            for row in changes
        ]

    @staticmethod
    def _event_wire(row: sqlite3.Row) -> dict[str, Any]:
        event: dict[str, Any] = {
            "seq": row["seq"],
            "type": row["type"],
            "path": row["path"],
        }
        if row["name"] is not None:
            event.update({
                "name": row["name"],
                "is_dir": bool(row["is_dir"]),
                "size": row["size"],
                "mtime": row["mtime"],
                "mtime_ns": row["mtime_ns"],
            })
        return event
