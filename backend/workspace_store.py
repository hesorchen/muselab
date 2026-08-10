"""SQLite-backed workspace file index and replayable filesystem event log."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .files import INTERNAL_DIR_NAME, TRASH_DIR_NAME


_DB_NAME = "workspace-state.sqlite3"
_EXCLUDED_DIRS = frozenset({INTERNAL_DIR_NAME, TRASH_DIR_NAME})
# These generated/control trees stay addressable through the lazy `/list` API,
# but persisting every descendant makes home-directory workspaces unusable.
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
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".hypothesis",
})
_EVENT_LIMIT = 20_000
# A reconciliation can discover tens of thousands of offline changes at once
# (for example when a formerly indexed cache tree becomes opaque). Replaying
# that many rows is slower than one clean snapshot and would be pruned anyway.
_RECONCILE_REPLAY_LIMIT = 500
# Compact bootstrap restores root plus previously expanded directories. Cap each
# sibling set independently so one generated/dump directory cannot turn a cold
# page load into a multi-megabyte JSON response and browser main-thread stall.
_BOOTSTRAP_CHILDREN_PER_PARENT = 500


class WorkspaceScanIncomplete(RuntimeError):
    """Raised when a full reconciliation cannot safely prove deletions."""


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


def scan_workspace(root: Path) -> list[dict[str, Any]]:
    """Return a complete metadata snapshot without following directory links."""
    root = root.resolve()
    rows: list[dict[str, Any]] = []
    stack: list[tuple[Path, Path]] = [(root, Path())]
    while stack:
        directory, logical_parent = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = list(iterator)
        except OSError as exc:
            # A partial snapshot cannot distinguish an unreadable subtree from a
            # deleted one. Keep the last-good index instead of inventing deletes.
            raise WorkspaceScanIncomplete(str(directory)) from exc
        for child in children:
            if child.name in _EXCLUDED_DIRS:
                continue
            logical = logical_parent / child.name
            try:
                is_symlink = child.is_symlink()
                is_dir = child.is_dir()
                stat = child.stat(follow_symlinks=not is_symlink)
            except OSError as exc:
                # A disappearing or temporarily unreadable entry makes this
                # snapshot non-authoritative for deletion. Reconcile again on
                # the next pass instead of erasing its last-good row/subtree.
                raise WorkspaceScanIncomplete(str(child.path)) from exc
            path = logical.as_posix()
            rows.append(_entry(path, is_dir, stat))
            if (
                is_dir
                and not is_symlink
                and child.name not in _IGNORED_SUBTREES
            ):
                stack.append((Path(child.path), logical))
    rows.sort(key=lambda row: row["path"])
    return rows


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
    ) -> int:
        """Scan disk, update only changed rows, and log offline changes."""
        root = root.resolve()
        # Keep a watcher batch from committing after our snapshot but before the
        # reconciliation transaction. Different workspaces may still scan in
        # parallel, while bootstrap/delta reads keep using the last-good index.
        with self._workspace_lock(workspace_id):
            snapshot = scan_workspace(root)
            self.register_workspace(
                workspace_id,
                root,
                name,
                primary=primary,
            )
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                state = db.execute(
                    """
                    SELECT initialized, current_seq
                    FROM workspaces WHERE id = ?
                    """,
                    (workspace_id,),
                ).fetchone()
                initialized = bool(state["initialized"]) if state else False
                seq = int(state["current_seq"]) if state else 0
                old = {
                    row["path"]: row
                    for row in self._file_rows(db, workspace_id)
                }
                new = {row["path"]: row for row in snapshot}

                changes: list[dict[str, Any]] = []
                if not initialized:
                    # A failed first scan may have left watcher-created rows.
                    # Establish one authoritative baseline without replaying it
                    # as thousands of synthetic "added" events.
                    db.execute(
                        "DELETE FROM files WHERE workspace_id = ?",
                        (workspace_id,),
                    )
                    self._insert_files(db, workspace_id, snapshot)
                else:
                    deleted = sorted(old.keys() - new.keys())
                    added = sorted(new.keys() - old.keys())
                    modified = sorted(
                        path
                        for path in old.keys() & new.keys()
                        if self._signature(old[path])
                        != self._signature(new[path])
                    )
                    change_count = len(deleted) + len(added) + len(modified)
                    if change_count > self._replay_limit():
                        # A generated-tree policy change can invalidate tens of
                        # thousands of rows at once. Replacing that workspace's
                        # index in two bulk statements is substantially cheaper
                        # than issuing one DELETE/UPSERT per stale path, and the
                        # cursor gap below already requires clients to bootstrap.
                        db.execute(
                            "DELETE FROM files WHERE workspace_id = ?",
                            (workspace_id,),
                        )
                        self._insert_files(db, workspace_id, snapshot)
                        # Create a deliberate cursor gap. `delta()` turns any
                        # pre-reset cursor into `resync=true`, while a client
                        # bootstrapped at the new cursor continues normally.
                        seq = self._reset_replay(db, workspace_id, seq)
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

                db.execute(
                    """
                    UPDATE workspaces
                    SET initialized = 1, current_seq = ?, scanned_at = ?
                    WHERE id = ?
                    """,
                    (seq, time.time(), workspace_id),
                )
                self._prune(db, workspace_id, seq)
                db.commit()
                return seq

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
                    if (
                        old is not None
                        and self._signature(old) == self._signature(row)
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
