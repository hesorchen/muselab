"""Crash-safe staged attachment registry used by the chat queue.

Payload bytes are stored in private, fsynced blob files. SQLite owns metadata,
exclusive leases, and queue references. A queue reference pins its blob past
the ordinary upload TTL; successful queue acknowledgement is the only normal
path that consumes it.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .private_storage import (
    PRIVATE_FILE_MODE,
    UnsafePrivatePath,
    ensure_private_directory,
    ensure_private_regular_file,
    private_path_kind,
    write_private_bytes,
)


_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,80}")
_BLOB_RE = re.compile(r"([A-Za-z0-9_-]{6,80})\.(blob|text)")


class DurableAttachmentError(RuntimeError):
    """The durable attachment transaction could not be completed safely."""


@dataclass(frozen=True)
class LeaseResult:
    entries: dict[str, dict]
    missing: tuple[str, ...] = ()
    busy: tuple[str, ...] = ()


class DurableAttachmentStore:
    """SQLite metadata plus opaque private blobs for staged attachments.

    The database never stores caller-provided paths. Blob names are derived
    solely from validated random attachment ids, and every read revalidates
    size and SHA-256 before returning private content to chat.py.
    """

    def __init__(self, root: Path):
        self.internal = Path(root) / ".muselab"
        self.base = self.internal / "staged-attachments"
        self.blobs = self.base / "blobs"
        self.path = self.base / "registry.sqlite3"
        self.lock_path = self.base / "registry.lock"
        self._lock = threading.RLock()
        ensure_private_directory(self.internal)
        ensure_private_directory(self.base)
        ensure_private_directory(self.blobs)
        kind = private_path_kind(self.path)
        if kind not in {"missing", "file"}:
            raise UnsafePrivatePath("durable attachment registry is unsafe")
        self._init()
        self._harden_permissions()
        with self._storage_lock():
            self._recover_storage_locked()

    @staticmethod
    def _validate_id(aid: str) -> str:
        value = str(aid or "")
        if _ID_RE.fullmatch(value) is None:
            raise DurableAttachmentError("invalid attachment id")
        return value

    def _blob_path(self, aid: str, suffix: str = "blob") -> Path:
        if suffix not in {"blob", "text"}:
            raise DurableAttachmentError("invalid attachment blob kind")
        return self.blobs / f"{self._validate_id(aid)}.{suffix}"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        kind = private_path_kind(self.path)
        if kind not in {"missing", "file"}:
            raise UnsafePrivatePath("durable attachment registry is unsafe")
        conn = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        try:
            ensure_private_regular_file(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA secure_delete=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _storage_lock(self) -> Iterator[None]:
        """Serialize filesystem/SQLite commit windows across processes."""
        ensure_private_directory(self.base)
        kind = private_path_kind(self.lock_path)
        if kind not in {"missing", "file"}:
            raise UnsafePrivatePath("durable attachment lock is unsafe")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(self.lock_path, flags, PRIVATE_FILE_MODE)
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise UnsafePrivatePath("durable attachment lock is unsafe")
            os.fchmod(fd, PRIVATE_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # BEGIN creates SQLite sidecars before sensitive rows are written.
            self._harden_permissions()
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _init(self) -> None:
        schema = """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS attachments (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          mime TEXT NOT NULL,
          name TEXT NOT NULL,
          size INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          text_size INTEGER NOT NULL DEFAULT 0,
          text_sha256 TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          lease_token TEXT,
          lease_owner TEXT NOT NULL DEFAULT '',
          lease_expires_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_attachment_expiry
          ON attachments(expires_at, created_at);
        CREATE TABLE IF NOT EXISTS queue_refs (
          item_id TEXT NOT NULL,
          attachment_id TEXT NOT NULL
            REFERENCES attachments(id) ON DELETE CASCADE,
          session_id TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'queued'
            CHECK(state IN ('queued', 'claimed', 'submitted')),
          turn_id TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY(item_id, attachment_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_ref_attachment
          ON queue_refs(attachment_id);
        CREATE INDEX IF NOT EXISTS idx_queue_ref_session
          ON queue_refs(session_id, item_id);
        """
        with self._lock, self._connect() as conn:
            conn.executescript(schema)

    def _harden_permissions(self) -> None:
        ensure_private_directory(self.internal)
        ensure_private_directory(self.base)
        ensure_private_directory(self.blobs)
        kind = private_path_kind(self.path)
        if kind == "file":
            ensure_private_regular_file(self.path)
        elif kind != "missing":
            raise UnsafePrivatePath("durable attachment registry is unsafe")
        lock_kind = private_path_kind(self.lock_path)
        if lock_kind == "file":
            ensure_private_regular_file(self.lock_path)
        elif lock_kind != "missing":
            raise UnsafePrivatePath("durable attachment lock is unsafe")
        for suffix in ("-wal", "-shm"):
            sibling = self.path.with_name(self.path.name + suffix)
            sibling_kind = private_path_kind(sibling)
            if sibling_kind == "file":
                ensure_private_regular_file(sibling)
            elif sibling_kind != "missing":
                raise UnsafePrivatePath("durable attachment registry is unsafe")

    @contextmanager
    def _open_blob(
        self,
        aid: str,
        suffix: str = "blob",
    ) -> Iterator[tuple[int, os.stat_result]]:
        path = self._blob_path(aid, suffix)
        if not ensure_private_regular_file(path):
            raise FileNotFoundError(path)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise UnsafePrivatePath("durable attachment blob is unsafe")
            if stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE:
                os.fchmod(fd, PRIVATE_FILE_MODE)
                current = os.fstat(fd)
            yield fd, current
        finally:
            os.close(fd)

    def _blob_available(
        self,
        aid: str,
        expected_size: int,
        suffix: str = "blob",
    ) -> bool:
        try:
            with self._open_blob(aid, suffix) as (_fd, current):
                return current.st_size == expected_size
        except (FileNotFoundError, OSError, UnsafePrivatePath):
            return False

    def _read_blob(
        self,
        aid: str,
        expected_size: int,
        suffix: str = "blob",
    ) -> bytes | None:
        try:
            with self._open_blob(aid, suffix) as (fd, current):
                if current.st_size != expected_size:
                    return None
                payload = bytearray()
                remaining = expected_size
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        return None
                    payload.extend(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1):
                    return None
                return bytes(payload)
        except (FileNotFoundError, OSError, UnsafePrivatePath):
            return None

    def _blob_valid(
        self,
        aid: str,
        expected_size: int,
        expected_digest: str,
        suffix: str = "blob",
    ) -> bool:
        payload = self._read_blob(aid, expected_size, suffix)
        return (
            payload is not None
            and hashlib.sha256(payload).hexdigest() == expected_digest
        )

    def _row_blobs_valid(self, row: sqlite3.Row) -> bool:
        aid = str(row["id"])
        if not self._blob_valid(
            aid, int(row["size"]), str(row["sha256"])
        ):
            return False
        text_size = int(row["text_size"])
        text_digest = str(row["text_sha256"])
        return (
            not text_digest
            or self._blob_valid(
                aid,
                text_size,
                text_digest,
                "text",
            )
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise UnsafePrivatePath("durable attachment directory is unsafe")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _recover_storage_locked(self, *, verify_rows: bool = True) -> None:
        """Remove incomplete filesystem commits and unusable metadata rows."""
        invalid: list[str] = []
        if verify_rows:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    """SELECT id,size,sha256,text_size,text_sha256
                       FROM attachments"""
                ).fetchall()
            for row in rows:
                aid = str(row["id"])
                if not self._row_blobs_valid(row):
                    invalid.append(aid)
        if invalid:
            with self._write_tx() as conn:
                marks = ",".join("?" for _ in invalid)
                conn.execute(
                    f"DELETE FROM attachments WHERE id IN ({marks})", invalid
                )

        with self._lock, self._connect() as conn:
            known: set[str] = set()
            for row in conn.execute(
                "SELECT id,text_sha256 FROM attachments"
            ):
                aid = str(row["id"])
                known.add(f"{aid}.blob")
                if str(row["text_sha256"]):
                    known.add(f"{aid}.text")
        changed = False
        for path in self.blobs.iterdir():
            name = path.name
            match = _BLOB_RE.fullmatch(name)
            is_orphan_blob = match is not None and name not in known
            is_write_temp = name.startswith(".") and name.endswith(".tmp")
            if not is_orphan_blob and not is_write_temp:
                continue
            if private_path_kind(path) != "file":
                continue
            try:
                path.unlink()
                changed = True
            except OSError:
                pass
        if changed:
            self._fsync_directory(self.blobs)

    @staticmethod
    def _payload(entry: dict) -> bytes:
        kind = str(entry.get("kind") or "image")
        if kind in {"image", "pdf"}:
            try:
                return base64.b64decode(
                    str(entry.get("b64") or ""), validate=True)
            except Exception as exc:
                raise DurableAttachmentError(
                    "attachment payload is not valid base64"
                ) from exc
        raw = entry.get("raw")
        if isinstance(raw, bytes):
            return raw
        if kind == "text":
            return str(entry.get("text") or "").encode("utf-8")
        raise DurableAttachmentError("attachment payload is unavailable")

    @staticmethod
    def _entry(
        row: sqlite3.Row,
        payload: bytes,
        text_payload: bytes | None = None,
    ) -> dict:
        kind = str(row["kind"])
        entry: dict = {
            "kind": kind,
            "mime": str(row["mime"]),
            "name": str(row["name"]),
            "ts": float(row["created_at"]),
        }
        if kind in {"image", "pdf"}:
            entry["b64"] = base64.b64encode(payload).decode("ascii")
        else:
            entry["raw"] = payload
            if kind == "text":
                entry["text"] = payload.decode("utf-8")
            elif text_payload is not None:
                entry["text"] = text_payload.decode("utf-8")
        return entry

    @staticmethod
    def _clear_expired_leases(
        conn: sqlite3.Connection,
        now: float,
        protected_tokens: set[str] | None = None,
    ) -> None:
        protected = sorted(token for token in (protected_tokens or set())
                           if token)
        exclusion = ""
        params: list[object] = [now]
        if protected:
            marks = ",".join("?" for _ in protected)
            exclusion = f" AND lease_token NOT IN ({marks})"
            params.extend(protected)
        conn.execute(
            f"""UPDATE attachments
                SET lease_token=NULL,lease_owner='',lease_expires_at=NULL
                WHERE lease_token IS NOT NULL AND lease_expires_at<=?
                {exclusion}""",
            params,
        )

    @staticmethod
    def _collectable_ids(
        conn: sqlite3.Connection,
        now: float,
        *,
        expired_only: bool,
    ) -> list[str]:
        where = "AND a.expires_at<=?" if expired_only else ""
        params: tuple[float, ...] = (now,) if expired_only else ()
        rows = conn.execute(
            f"""SELECT a.id FROM attachments a
                WHERE a.lease_token IS NULL {where}
                  AND NOT EXISTS (
                    SELECT 1 FROM queue_refs q
                    WHERE q.attachment_id=a.id
                  )
                ORDER BY a.created_at,a.id""",
            params,
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def _unlink_ids(self, ids: list[str] | tuple[str, ...]) -> None:
        changed = False
        for aid in ids:
            for suffix in ("blob", "text"):
                try:
                    path = self._blob_path(aid, suffix)
                    if private_path_kind(path) == "file":
                        path.unlink()
                        changed = True
                except (OSError, DurableAttachmentError):
                    # A missing blob is already collected. A future startup GC
                    # retries an unlink that failed after the metadata commit.
                    pass
        if changed:
            try:
                self._fsync_directory(self.blobs)
            except (OSError, UnsafePrivatePath):
                # Metadata is already authoritative. Startup recovery removes
                # a blob resurrected by an unflushed directory entry.
                pass

    def stage_batch(
        self,
        batch: list[tuple[str, dict]],
        *,
        ttl: float,
        max_entries: int,
        max_bytes: int,
    ) -> tuple[bool, tuple[str, ...]]:
        """Publish every blob and metadata row or publish none of the rows."""
        if not batch:
            return True, ()
        now = time.time()
        prepared: list[
            tuple[str, dict, bytes, str, bytes | None, str]
        ] = []
        ids: list[str] = []
        for aid, entry in batch:
            aid = self._validate_id(aid)
            if aid in ids:
                return False, ()
            payload = self._payload(entry)
            text_payload = None
            if (str(entry.get("kind") or "image") != "text"
                    and isinstance(entry.get("text"), str)):
                text_payload = str(entry["text"]).encode("utf-8")
            ids.append(aid)
            prepared.append((
                aid,
                entry,
                payload,
                hashlib.sha256(payload).hexdigest(),
                text_payload,
                (hashlib.sha256(text_payload).hexdigest()
                 if text_payload is not None else ""),
            ))

        written: list[str] = []
        evicted: list[str] = []
        may_cleanup = False
        with self._storage_lock():
            try:
                with self._lock:
                    with self._connect() as conn:
                        placeholders = ",".join("?" for _ in ids)
                        if conn.execute(
                            f"SELECT 1 FROM attachments WHERE id IN ({placeholders}) LIMIT 1",
                            ids,
                        ).fetchone() is not None:
                            return False, ()
                    may_cleanup = True
                    for (aid, _entry, payload, _digest,
                         text_payload, _text_digest) in prepared:
                        path = self._blob_path(aid)
                        text_path = self._blob_path(aid, "text")
                        if (private_path_kind(path) != "missing"
                                or (text_payload is not None
                                    and private_path_kind(text_path)
                                    != "missing")):
                            self._unlink_ids(written)
                            return False, ()
                        write_private_bytes(path, payload)
                        written.append(aid)
                        if text_payload is not None:
                            write_private_bytes(text_path, text_payload)
                    # Blob directory entries must reach stable storage before
                    # the SQLite commit makes the batch observable.
                    self._fsync_directory(self.blobs)

                    with self._write_tx() as conn:
                        if conn.execute(
                            f"SELECT 1 FROM attachments WHERE id IN ({placeholders}) LIMIT 1",
                            ids,
                        ).fetchone() is not None:
                            self._unlink_ids(written)
                            return False, ()
                        count, size = conn.execute(
                            """SELECT count(*),
                                      coalesce(sum(size+text_size),0)
                               FROM attachments"""
                        ).fetchone()
                        projected_count = int(count) + len(prepared)
                        projected_bytes = int(size) + sum(
                            len(payload) + len(text_payload or b"")
                            for (_aid, _entry, payload, _sha,
                                 text_payload, _text_sha) in prepared
                        )
                        candidates = conn.execute(
                            """SELECT a.id,(a.size+a.text_size) AS stored_size
                               FROM attachments a
                               WHERE a.lease_token IS NULL
                                 AND NOT EXISTS (
                                   SELECT 1 FROM queue_refs q
                                   WHERE q.attachment_id=a.id
                                 )
                               ORDER BY a.created_at,a.id"""
                        ).fetchall()
                        for row in candidates:
                            if (projected_count <= max_entries
                                    and projected_bytes <= max_bytes):
                                break
                            evicted.append(str(row["id"]))
                            projected_count -= 1
                            projected_bytes -= int(row["stored_size"])
                        if (projected_count > max_entries
                                or projected_bytes > max_bytes):
                            self._unlink_ids(written)
                            return False, ()
                        if evicted:
                            marks = ",".join("?" for _ in evicted)
                            conn.execute(
                                f"DELETE FROM attachments WHERE id IN ({marks})",
                                evicted,
                            )
                        conn.executemany(
                            """INSERT INTO attachments
                               (id,kind,mime,name,size,sha256,text_size,
                                text_sha256,created_at,expires_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            [(
                                aid,
                                str(entry.get("kind") or "image"),
                                str(entry.get("mime") or ""),
                                str(entry.get("name") or "file"),
                                len(payload),
                                digest,
                                len(text_payload or b""),
                                text_digest,
                                now,
                                now + max(0.0, float(ttl)),
                            ) for (aid, entry, payload, digest,
                                   text_payload, text_digest) in prepared],
                        )
                    self._unlink_ids(evicted)
                    return True, tuple(evicted)
            except BaseException:
                # Keep the same cross-process lock through repair so a second
                # publisher cannot reuse an id before failed bytes are gone.
                if may_cleanup:
                    try:
                        with self._write_tx() as conn:
                            marks = ",".join("?" for _ in ids)
                            conn.execute(
                                f"DELETE FROM attachments WHERE id IN ({marks})",
                                ids,
                            )
                    except BaseException:
                        pass
                    self._unlink_ids(written)
                raise

    def existing_ids(self, ids: list[str], *, now: float | None = None) -> set[str]:
        if not ids:
            return set()
        ids = [self._validate_id(aid) for aid in dict.fromkeys(ids)]
        current = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""SELECT a.id,a.expires_at,a.size,a.sha256,
                           a.text_size,a.text_sha256,
                           EXISTS(SELECT 1 FROM queue_refs q
                                  WHERE q.attachment_id=a.id) AS pinned
                    FROM attachments a WHERE a.id IN ({marks})""",
                ids,
            ).fetchall()
        return {
            str(row["id"])
            for row in rows
            if (bool(row["pinned"]) or float(row["expires_at"]) > current)
            and self._row_blobs_valid(row)
        }

    def registered_ids(
        self,
        ids: list[str],
        *,
        now: float | None = None,
    ) -> set[str]:
        """Resolve durable metadata without reading payload bytes.

        The chat hot-cache path uses this only to decide which ids need a
        durable lease. Cache misses are still fully verified by ``acquire``.
        """
        if not ids:
            return set()
        ids = [self._validate_id(aid) for aid in dict.fromkeys(ids)]
        current = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""SELECT a.id,a.expires_at,
                           EXISTS(SELECT 1 FROM queue_refs q
                                  WHERE q.attachment_id=a.id) AS pinned
                    FROM attachments a WHERE a.id IN ({marks})""",
                ids,
            ).fetchall()
        return {
            str(row["id"])
            for row in rows
            if bool(row["pinned"]) or float(row["expires_at"]) > current
        }

    def metadata(self, aid: str) -> dict | None:
        aid = self._validate_id(aid)
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT a.*,
                          EXISTS(SELECT 1 FROM queue_refs q
                                 WHERE q.attachment_id=a.id) AS pinned
                   FROM attachments a WHERE a.id=?""",
                (aid,),
            ).fetchone()
        if row is None or (not bool(row["pinned"])
                           and float(row["expires_at"]) <= now):
            return None
        if not self._row_blobs_valid(row):
            return None
        return {
            "id": aid,
            "kind": str(row["kind"]),
            "mime": str(row["mime"]),
            "name": str(row["name"]),
            "available": True,
        }

    def load_entry(self, aid: str) -> dict | None:
        aid = self._validate_id(aid)
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT a.*,
                          EXISTS(SELECT 1 FROM queue_refs q
                                 WHERE q.attachment_id=a.id) AS pinned
                   FROM attachments a WHERE a.id=?""",
                (aid,),
            ).fetchone()
        if row is None or (not bool(row["pinned"])
                           and float(row["expires_at"]) <= now):
            return None
        payload = self._read_blob(aid, int(row["size"]))
        if payload is None:
            return None
        if (len(payload) != int(row["size"])
                or hashlib.sha256(payload).hexdigest() != str(row["sha256"])):
            return None
        try:
            text_payload = None
            if str(row["text_sha256"]):
                text_payload = self._read_blob(
                    aid, int(row["text_size"]), "text"
                )
                if (text_payload is None
                        or hashlib.sha256(text_payload).hexdigest()
                        != str(row["text_sha256"])):
                    return None
            return self._entry(row, payload, text_payload)
        except (UnicodeDecodeError, ValueError):
            return None

    def bind_queue_item(
        self,
        session_id: str,
        item_id: str,
        ids: list[str],
        *,
        ttl: float,
    ) -> LeaseResult:
        ids = [self._validate_id(aid) for aid in dict.fromkeys(ids)]
        if not ids:
            return LeaseResult(entries={})
        now = time.time()
        with self._write_tx() as conn:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""SELECT a.id,a.expires_at,a.lease_token,a.size,a.sha256,
                           a.text_size,a.text_sha256,
                           q.item_id AS owner,q.session_id AS owner_session
                    FROM attachments a
                    LEFT JOIN queue_refs q ON q.attachment_id=a.id
                    WHERE a.id IN ({marks})""",
                ids,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing = tuple(
                aid for aid in ids
                if aid not in by_id
                or (float(by_id[aid]["expires_at"]) <= now
                    and not by_id[aid]["owner"])
                or (aid in by_id and not self._row_blobs_valid(by_id[aid]))
            )
            busy = tuple(
                aid for aid in ids
                if aid in by_id and (
                    by_id[aid]["lease_token"] is not None
                    or (by_id[aid]["owner"]
                        and (
                            str(by_id[aid]["owner"]) != item_id
                            or str(by_id[aid]["owner_session"]) != session_id
                        ))
                )
            )
            if missing or busy:
                return LeaseResult(entries={}, missing=missing, busy=busy)
            conn.executemany(
                """INSERT INTO queue_refs
                   (item_id,attachment_id,session_id,state,created_at,updated_at)
                   VALUES (?,?,?,'queued',?,?)
                   ON CONFLICT(item_id,attachment_id) DO UPDATE SET
                     session_id=excluded.session_id,updated_at=excluded.updated_at""",
                [(item_id, aid, session_id, now, now) for aid in ids],
            )
            conn.execute(
                f"UPDATE attachments SET expires_at=max(expires_at,?) WHERE id IN ({marks})",
                [now + max(0.0, ttl), *ids],
            )
        return LeaseResult(entries={})

    def acquire(
        self,
        ids: list[str],
        token: str,
        *,
        lease_seconds: float,
        queue_owner: tuple[str, str] | None = None,
        load_ids: list[str] | None = None,
    ) -> LeaseResult:
        ids = [self._validate_id(aid) for aid in dict.fromkeys(ids)]
        if not ids:
            return LeaseResult(entries={})
        requested_load = set(ids) if load_ids is None else {
            self._validate_id(aid) for aid in load_ids
        }
        if not requested_load.issubset(ids):
            raise DurableAttachmentError("load id is outside lease batch")
        now = time.time()
        with self._write_tx() as conn:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""SELECT a.id,a.expires_at,a.lease_token,a.size,a.sha256,
                           a.text_size,a.text_sha256,
                           q.item_id AS ref_item,q.session_id AS ref_session
                    FROM attachments a
                    LEFT JOIN queue_refs q ON q.attachment_id=a.id
                    WHERE a.id IN ({marks})""",
                ids,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing: list[str] = []
            busy: list[str] = []
            owner_session = queue_owner[0] if queue_owner else ""
            owner_item = queue_owner[1] if queue_owner else ""
            for aid in ids:
                row = by_id.get(aid)
                if row is None:
                    missing.append(aid)
                elif queue_owner and (
                    str(row["ref_item"] or "") != owner_item
                    or str(row["ref_session"] or "") != owner_session
                ):
                    missing.append(aid)
                elif (aid in requested_load
                      and not self._row_blobs_valid(row)):
                    missing.append(aid)
                elif (aid not in requested_load
                      and (not self._blob_available(aid, int(row["size"]))
                           or (str(row["text_sha256"])
                               and not self._blob_available(
                                   aid, int(row["text_size"]), "text")))):
                    missing.append(aid)
                elif not row["ref_item"] and float(row["expires_at"]) <= now:
                    missing.append(aid)
                elif not queue_owner and row["ref_item"]:
                    busy.append(aid)
                elif row["lease_token"] not in {None, token}:
                    busy.append(aid)
            if missing or busy:
                return LeaseResult(
                    entries={}, missing=tuple(missing), busy=tuple(busy))
            lease_owner = (
                f"queue:{queue_owner[0]}:{queue_owner[1]}"
                if queue_owner else "direct"
            )
            conn.execute(
                f"""UPDATE attachments
                    SET lease_token=?,lease_owner=?,lease_expires_at=?
                    WHERE id IN ({marks})""",
                [token, lease_owner, now + max(1.0, lease_seconds), *ids],
            )
            if queue_owner:
                conn.execute(
                    f"""UPDATE queue_refs SET
                          state=CASE WHEN state='submitted'
                                     THEN state ELSE 'claimed' END,
                          updated_at=?
                        WHERE item_id=? AND attachment_id IN ({marks})""",
                    [now, owner_item, *ids],
                )
        entries: dict[str, dict] = {}
        for aid in ids:
            if aid not in requested_load:
                continue
            entry = self.load_entry(aid)
            if entry is None:
                self.release(token, ttl=0)
                return LeaseResult(entries={}, missing=(aid,))
            entries[aid] = entry
        return LeaseResult(entries=entries)

    def mark_queue_turn(
        self,
        session_id: str,
        item_id: str,
        turn_id: str,
    ) -> None:
        now = time.time()
        with self._write_tx() as conn:
            conn.execute(
                """UPDATE queue_refs SET
                     state=CASE WHEN state='submitted'
                                THEN state ELSE 'claimed' END,
                     turn_id=?,updated_at=?
                   WHERE session_id=? AND item_id=?""",
                (str(turn_id or ""), now, session_id, item_id),
            )

    def commit(
        self,
        token: str,
        *,
        queue_session_id: str = "",
        queue_item_id: str = "",
    ) -> bool:
        if bool(queue_session_id) != bool(queue_item_id):
            return False
        deleted: list[str] = []
        with self._storage_lock():
            with self._write_tx() as conn:
                rows = conn.execute(
                    "SELECT id FROM attachments WHERE lease_token=?",
                    (token,),
                ).fetchall()
                ids = [str(row["id"]) for row in rows]
                if not ids:
                    return False
                if queue_item_id and queue_session_id:
                    marks = ",".join("?" for _ in ids)
                    owned = conn.execute(
                        f"""SELECT count(*) FROM queue_refs
                            WHERE session_id=? AND item_id=?
                              AND attachment_id IN ({marks})""",
                        [queue_session_id, queue_item_id, *ids],
                    ).fetchone()[0]
                    if int(owned) != len(ids):
                        return False
                    conn.execute(
                        f"""UPDATE queue_refs SET
                              state='submitted',updated_at=?
                           WHERE item_id=?
                             AND session_id=?
                             AND attachment_id IN ({marks})""",
                        [time.time(), queue_item_id, queue_session_id, *ids],
                    )
                    conn.execute(
                        """UPDATE attachments SET lease_token=NULL,lease_owner='',
                           lease_expires_at=NULL WHERE lease_token=?""",
                        (token,),
                    )
                else:
                    for aid in ids:
                        if conn.execute(
                            "SELECT 1 FROM queue_refs WHERE attachment_id=?",
                            (aid,),
                        ).fetchone() is None:
                            conn.execute(
                                "DELETE FROM attachments WHERE id=?", (aid,)
                            )
                            deleted.append(aid)
                        else:
                            conn.execute(
                                """UPDATE attachments SET lease_token=NULL,
                                   lease_owner='',lease_expires_at=NULL
                                   WHERE id=?""",
                                (aid,),
                            )
            self._unlink_ids(deleted)
        return True

    def release(self, token: str, *, ttl: float) -> bool:
        now = time.time()
        with self._write_tx() as conn:
            rows = conn.execute(
                "SELECT id FROM attachments WHERE lease_token=?", (token,)
            ).fetchall()
            if not rows:
                return False
            ids = [str(row["id"]) for row in rows]
            conn.execute(
                """UPDATE attachments SET lease_token=NULL,lease_owner='',
                   lease_expires_at=NULL,expires_at=? WHERE lease_token=?""",
                (now + max(0.0, ttl), token),
            )
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"""UPDATE queue_refs SET state='queued',turn_id='',updated_at=?
                    WHERE attachment_id IN ({marks}) AND state!='submitted'""",
                [now, *ids],
            )
        return True

    def finish_queue_item(
        self,
        session_id: str,
        item_id: str,
        *,
        consume: bool,
        ttl: float = 600,
    ) -> tuple[str, ...]:
        deleted: list[str] = []
        now = time.time()
        with self._storage_lock():
            with self._write_tx() as conn:
                rows = conn.execute(
                    """SELECT attachment_id,state FROM queue_refs
                       WHERE session_id=? AND item_id=?""",
                    (session_id, item_id),
                ).fetchall()
                ids = [str(row["attachment_id"]) for row in rows]
                submitted = {
                    str(row["attachment_id"])
                    for row in rows if str(row["state"]) == "submitted"
                }
                conn.execute(
                    "DELETE FROM queue_refs WHERE session_id=? AND item_id=?",
                    (session_id, item_id),
                )
                for aid in ids:
                    should_consume = consume or aid in submitted
                    other_ref = conn.execute(
                        "SELECT 1 FROM queue_refs WHERE attachment_id=?", (aid,)
                    ).fetchone()
                    leased = conn.execute(
                        "SELECT lease_token FROM attachments WHERE id=?", (aid,)
                    ).fetchone()
                    if (should_consume and other_ref is None
                            and leased is not None
                            and leased["lease_token"] is None):
                        conn.execute(
                            "DELETE FROM attachments WHERE id=?", (aid,)
                        )
                        deleted.append(aid)
                    elif not should_consume:
                        conn.execute(
                            "UPDATE attachments SET expires_at=? WHERE id=?",
                            (now + max(0.0, ttl), aid),
                        )
            self._unlink_ids(deleted)
        return tuple(deleted)

    def migrate_queue_items(
        self,
        source_sid: str,
        item_ids: list[str],
        target_sid: str,
    ) -> None:
        if not item_ids:
            return
        marks = ",".join("?" for _ in item_ids)
        with self._write_tx() as conn:
            conn.execute(
                f"""UPDATE queue_refs SET session_id=?,updated_at=?
                    WHERE session_id=? AND item_id IN ({marks})""",
                [target_sid, time.time(), source_sid, *item_ids],
            )

    def cancel_session(self, session_id: str) -> tuple[str, ...]:
        with self._lock, self._connect() as conn:
            item_ids = [
                str(row["item_id"])
                for row in conn.execute(
                    "SELECT DISTINCT item_id FROM queue_refs WHERE session_id=?",
                    (session_id,),
                )
            ]
        deleted: list[str] = []
        for item_id in item_ids:
            deleted.extend(self.finish_queue_item(
                session_id, item_id, consume=True
            ))
        return tuple(deleted)

    def discard_unowned(self, ids: list[str]) -> tuple[str, ...]:
        ids = [self._validate_id(aid) for aid in dict.fromkeys(ids)]
        deleted: list[str] = []
        with self._storage_lock():
            with self._write_tx() as conn:
                for aid in ids:
                    row = conn.execute(
                        "SELECT lease_token FROM attachments WHERE id=?", (aid,)
                    ).fetchone()
                    if row is None or row["lease_token"] is not None:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM queue_refs WHERE attachment_id=?", (aid,)
                    ).fetchone() is not None:
                        continue
                    conn.execute("DELETE FROM attachments WHERE id=?", (aid,))
                    deleted.append(aid)
            self._unlink_ids(deleted)
        return tuple(deleted)

    def gc(
        self,
        *,
        now: float | None = None,
        protected_tokens: set[str] | None = None,
    ) -> tuple[str, ...]:
        current = time.time() if now is None else now
        with self._storage_lock():
            with self._write_tx() as conn:
                self._clear_expired_leases(
                    conn, current, protected_tokens)
                ids = self._collectable_ids(conn, current, expired_only=True)
                if ids:
                    marks = ",".join("?" for _ in ids)
                    conn.execute(
                        f"DELETE FROM attachments WHERE id IN ({marks})", ids
                    )
            self._unlink_ids(ids)
            self._recover_storage_locked(verify_rows=False)
        return tuple(ids)

    def reconcile_queue_refs(
        self,
        owners: dict[str, str],
        *,
        ttl: float,
    ) -> dict[str, int]:
        """Recover leases and repair refs against a complete queue snapshot."""
        now = time.time()
        deleted: list[str] = []
        released = 0
        moved = 0
        with self._storage_lock():
            with self._write_tx() as conn:
                # A service restart has no surviving in-process lease owner.
                conn.execute(
                    """UPDATE attachments SET lease_token=NULL,lease_owner='',
                       lease_expires_at=NULL WHERE lease_token IS NOT NULL"""
                )
                refs = conn.execute(
                    "SELECT item_id,attachment_id,session_id,state FROM queue_refs"
                ).fetchall()
                for row in refs:
                    item_id = str(row["item_id"])
                    aid = str(row["attachment_id"])
                    owner = owners.get(item_id)
                    if owner is not None:
                        if owner != str(row["session_id"]):
                            conn.execute(
                                """UPDATE queue_refs SET
                                     session_id=?,updated_at=?
                                   WHERE item_id=? AND attachment_id=?""",
                                (owner, now, item_id, aid),
                            )
                            moved += 1
                        continue
                    conn.execute(
                        """DELETE FROM queue_refs
                           WHERE item_id=? AND attachment_id=?""",
                        (item_id, aid),
                    )
                    released += 1
                    if str(row["state"]) == "submitted":
                        if conn.execute(
                            "SELECT 1 FROM queue_refs WHERE attachment_id=?",
                            (aid,),
                        ).fetchone() is None:
                            conn.execute(
                                "DELETE FROM attachments WHERE id=?", (aid,)
                            )
                            deleted.append(aid)
                    else:
                        conn.execute(
                            "UPDATE attachments SET expires_at=? WHERE id=?",
                            (now + max(0.0, ttl), aid),
                        )
                expired = self._collectable_ids(
                    conn, now, expired_only=True
                )
                if expired:
                    marks = ",".join("?" for _ in expired)
                    conn.execute(
                        f"DELETE FROM attachments WHERE id IN ({marks})",
                        expired,
                    )
            self._unlink_ids([*deleted, *expired])
            self._recover_storage_locked(verify_rows=False)
        return {
            "released": released,
            "moved": moved,
            "deleted": len(deleted) + len(expired),
        }
