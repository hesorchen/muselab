"""Private idempotent admission receipts, never prompts or protocol payloads.

An interrupted reservation is deliberately unresolved, not silently retried.
The caller can reconcile/cancel its exact request after a lost HTTP response.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import time

from . import sessions
from .private_storage import ensure_private_directory, ensure_private_regular_file


@contextmanager
def _connect():
    base = sessions.SESS_DIR / ".submissions"
    ensure_private_directory(base)
    path = base / "receipts.sqlite3"
    if not ensure_private_regular_file(path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    ensure_private_regular_file(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS receipts (
          sid TEXT NOT NULL, kind TEXT NOT NULL, request_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, state TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
          PRIMARY KEY(sid,kind,request_id))""")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _public(row):
    if row is None:
        return {"state": "not_found"}
    return {"state": row["state"], "result": json.loads(row["result"])}


def reserve(sid: str, kind: str, request_id: str, payload: dict) -> tuple[bool, dict]:
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM receipts WHERE sid=? AND kind=? AND request_id=?",
            (sid, kind, request_id)).fetchone()
        if row is not None:
            if row["state"] not in {"cancelled", "cancel_requested"} and row["fingerprint"] != fingerprint:
                raise ValueError("Submission ID was already used for different content")
            return False, _public(row)
        conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,'{}',?)",
                     (sid, kind, request_id, fingerprint, "pending", time.time()))
        return True, {"state": "pending"}


def finish(sid: str, kind: str, request_id: str, state: str, result: dict) -> dict:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM receipts WHERE sid=? AND kind=? AND request_id=?",
            (sid, kind, request_id)).fetchone()
        # Cancellation before admission must survive a delayed acceptance.
        cancelled = row is not None and row["state"] in {"cancelled", "cancel_requested"}
        conn.execute(
            "UPDATE receipts SET state=?,result=?,updated_at=?"
            " WHERE sid=? AND kind=? AND request_id=?",
            ("cancelled" if cancelled else state, json.dumps(result), time.time(),
             sid, kind, request_id))
        return {"state": "cancelled" if cancelled else state, "result": result}


def lookup(sid: str, kind: str, request_id: str) -> dict:
    with _connect() as conn:
        return _public(conn.execute(
            "SELECT * FROM receipts WHERE sid=? AND kind=? AND request_id=?",
            (sid, kind, request_id)).fetchone())


def purge(sid: str) -> None:
    """Session deletion includes its request metadata, not only its transcript."""
    if not (sessions.SESS_DIR / ".submissions" / "receipts.sqlite3").exists():
        return
    with _connect() as conn:
        conn.execute("DELETE FROM receipts WHERE sid=?", (sid,))


def cancel(sid: str, kind: str, request_id: str) -> dict:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO receipts VALUES (?,?,?,'','cancelled','{}',?)"
            " ON CONFLICT(sid,kind,request_id) DO UPDATE SET state=CASE WHEN receipts.state IN ('pending','cancel_requested')\n"
            " THEN 'cancel_requested' ELSE 'cancelled' END,updated_at=?",
            (sid, kind, request_id, time.time(), time.time()))
        return _public(conn.execute(
            "SELECT * FROM receipts WHERE sid=? AND kind=? AND request_id=?",
            (sid, kind, request_id)).fetchone())
