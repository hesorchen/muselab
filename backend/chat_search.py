"""Rebuildable, incremental substring search over canonical JSONL transcripts.

Only user/assistant text is indexed. The private cache never replaces canonical
history. One worker owns indexing/query work; superseded HTTP requests cancel
both queued work and an executing SQLite scan. FTS5 is an optional accelerator:
short queries and SQLite builds without trigram support use the same exact
Unicode-lowercase substring predicate.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable

from .chat_history import raw_msg_from_entry
from .observability import perf_event
from .private_storage import ensure_private_directory, ensure_private_regular_file

_executor: ThreadPoolExecutor | None = None
_pending: set[threading.Event] = set()


class SearchCancelled(Exception):
    pass


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise SearchCancelled


def _guard(handle, offset: int) -> str:
    handle.seek(max(0, offset - 65536))
    return hashlib.blake2b(handle.read(min(offset, 65536)), digest_size=16).hexdigest()


def _source_match_span(text: str, folded_text: str, needle: str) -> tuple[int, int]:
    """Map a lowercase substring match back to original-text coordinates."""
    start = folded_text.index(needle)
    end = start + len(needle)
    if len(text) == len(folded_text):
        return start, len(needle)
    # Unicode lowercasing can expand a source character (for example İ).
    # Indexed offsets then drift from the text the snippet renderer slices.
    folded_offset = 0
    source_start = 0
    for index, character in enumerate(text):
        if folded_offset <= start:
            source_start = index
        folded_offset += len(character.lower())
        if folded_offset >= end:
            return source_start, index + 1 - source_start
    return len(text), 0


class SearchIndex:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        ensure_private_directory(self.path.parent)
        if not ensure_private_regular_file(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT PRIMARY KEY, sid TEXT NOT NULL, signature TEXT NOT NULL,
                    offset INTEGER NOT NULL, ordinal INTEGER NOT NULL, guard TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    uuid TEXT NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL,
                    body TEXT NOT NULL, folded TEXT NOT NULL,
                    UNIQUE(source, ordinal),
                    FOREIGN KEY(source) REFERENCES sources(path) ON DELETE CASCADE
                );
            ''')
            # https://www.sqlite.org/fts5.html#the_trigram_tokenizer
            try:
                db.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    folded, content='documents', content_rowid='id',
                    tokenize='trigram case_sensitive 1')''')
            except sqlite3.OperationalError as exc:
                if not any(word in str(exc).lower() for word in ('tokenizer', 'no such module')):
                    db.close()
                    raise
            else:
                has_triggers = db.execute("SELECT 1 FROM sqlite_master WHERE name='search_insert'").fetchone()
                db.executescript('''
                    CREATE TRIGGER IF NOT EXISTS search_insert AFTER INSERT ON documents BEGIN
                        INSERT INTO search_fts(rowid, folded) VALUES (new.id, new.folded);
                    END;
                    CREATE TRIGGER IF NOT EXISTS search_delete AFTER DELETE ON documents BEGIN
                        INSERT INTO search_fts(search_fts, rowid, folded)
                        VALUES ('delete', old.id, old.folded);
                    END;
                ''')
                if not has_triggers:
                    db.execute("INSERT INTO search_fts(search_fts) VALUES ('rebuild')")
            if db.execute('PRAGMA user_version').fetchone()[0] < 1:
                # Legacy checkpoints skipped native steering attachments. Their
                # unchanged files need one rebuild under the complete parser.
                db.execute('DELETE FROM sources')
                db.execute('PRAGMA user_version=1')
            db.commit()
            return db
        except BaseException:
            db.close()
            raise

    def _refresh_file(self, db, path, cancel, extract, strip, metrics):
        key = str(path)
        try:
            stat = path.stat()
            signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            old = db.execute('SELECT * FROM sources WHERE path=?', (key,)).fetchone()
            if old and json.loads(old['signature']) == signature:
                return
            handle = path.open('rb')
        except FileNotFoundError:
            with db:
                db.execute('DELETE FROM sources WHERE path=?', (key,))
            return
        with handle:
            stat = os.fstat(handle.fileno())
            signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            old = db.execute('SELECT * FROM sources WHERE path=?', (key,)).fetchone()
            if old and json.loads(old['signature']) == signature:
                return
            offset = ordinal = 0
            if old:
                prev = json.loads(old['signature'])
                if (prev[:2] == signature[:2] and stat.st_size > prev[2]
                        and _guard(handle, old['offset']) == old['guard']):
                    offset, ordinal = old['offset'], old['ordinal']
            _check(cancel)
            with db:
                if offset == 0:
                    db.execute('DELETE FROM sources WHERE path=?', (key,))
                    db.execute('INSERT INTO sources VALUES (?, ?, ?, 0, 0, ?)',
                               (key, path.stem, json.dumps(signature), ''))
                else:
                    # A valid final line without newline is searchable, but is
                    # replayed on append in case the writer extended that line.
                    db.execute('DELETE FROM documents WHERE source=? AND ordinal>=?', (key, ordinal))
                handle.seek(offset)
                while handle.tell() < stat.st_size:
                    _check(cancel)
                    start = handle.tell()
                    raw = handle.readline(stat.st_size - start)
                    metrics['read_bytes'] += len(raw)
                    metrics['parsed_lines'] += 1
                    complete = raw.endswith(b'\n')
                    try:
                        entry = json.loads(raw.decode('utf-8-sig'))
                    except (UnicodeDecodeError, ValueError):
                        entry = None
                    if isinstance(entry, dict) and entry.get('type') == 'attachment':
                        message = raw_msg_from_entry(entry)
                        if message is not None and message.message.get('_muselab_steering'):
                            entry = {**entry, 'type': message.type, 'uuid': message.uuid,
                                     'message': message.message}
                    if isinstance(entry, dict) and entry.get('type') in ('user', 'assistant'):
                        message = entry.get('message')
                        body = extract(message.get('content')) if isinstance(message, dict) else ''
                        body = strip(body) or body
                        if body:
                            db.execute('''INSERT INTO documents
                                (source, ordinal, uuid, role, ts, body, folded)
                                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                (key, ordinal, str(entry.get('uuid') or ''), entry['type'],
                                 str(entry.get('timestamp') or ''), body, body.lower()))
                    if not complete:
                        offset = start
                        break
                    offset = handle.tell()
                    ordinal += 1
                    if ordinal % 128 == 0:
                        # Avoid a long Python JSON parsing burst monopolizing
                        # the interpreter while interactive requests run.
                        time.sleep(0)
                _check(cancel)
                # Do not publish a checkpoint for an in-place rewrite observed
                # during this pass. Appends are picked up on the next request.
                after = os.fstat(handle.fileno())
                if after.st_size < stat.st_size or (
                    after.st_size == stat.st_size and after.st_mtime_ns != stat.st_mtime_ns
                ):
                    raise OSError('transcript changed during search indexing')
                db.execute('UPDATE sources SET signature=?, offset=?, ordinal=?, guard=? WHERE path=?',
                           (json.dumps(signature), offset, ordinal, _guard(handle, offset), key))
            metrics['updated_files'] += 1

    def search(self, paths: list[Path], query: str, limit: int, names: dict,
               cancel: threading.Event, extract: Callable, strip: Callable,
               snippet: Callable, metrics: dict) -> dict:
        with closing(self._connect()) as db:
            db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            _check(cancel)
            allowed = {str(path) for path in paths}
            with db:
                for row in db.execute('SELECT path FROM sources').fetchall():
                    if row['path'] not in allowed:
                        db.execute('DELETE FROM sources WHERE path=?', (row['path'],))
            index_started = time.perf_counter()
            for path in paths:
                _check(cancel)
                self._refresh_file(db, path, cancel, extract, strip, metrics)
            metrics['index_ms'] = round((time.perf_counter() - index_started) * 1000)
            _check(cancel)
            sql_started = time.perf_counter()
            folded = query.lower()
            use_fts = len(folded) >= 3 and '\x00' not in folded and db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone()
            predicate = 'instr(d.folded, ?) > 0'
            args = [folded]
            if use_fts:
                predicate += ' AND d.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)'
                args.append('"' + folded.replace('"', '""') + '"')
            rows = db.execute(f'''WITH matches AS (
                SELECT d.id, s.sid, d.ts, d.ordinal,
                    row_number() OVER (PARTITION BY s.sid ORDER BY d.ts DESC, d.ordinal DESC, d.id DESC) AS n
                FROM documents d JOIN sources s ON s.path=d.source WHERE {predicate}
            ), capped AS (SELECT * FROM matches WHERE n<=5)
            SELECT d.*, c.sid, count(*) OVER () AS total
            FROM capped c JOIN documents d ON d.id=c.id
            ORDER BY c.ts DESC, c.ordinal DESC, c.id DESC LIMIT ?''', (*args, limit)).fetchall()
            hits = [{
                'sid': row['sid'], 'name': names.get(row['sid'], ''), 'uuid': row['uuid'],
                'role': row['role'], 'ts': row['ts'],
                'snippet': snippet(row['body'], *_source_match_span(row['body'], row['folded'], folded)),
            } for row in rows]
            metrics['lookup_ms'] = round((time.perf_counter() - sql_started) * 1000)
            metrics['fts'] = bool(use_fts)
            return {'hits': hits, 'total': rows[0]['total'] if rows else 0}


async def run_search(request, work: Callable) -> dict:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='chat-search')
    cancel = threading.Event()
    _pending.add(cancel)
    started = time.perf_counter()
    metrics = dict(read_bytes=0, parsed_lines=0, updated_files=0, queue_ms=0)
    outcome = 'error'

    def run():
        _check(cancel)
        metrics['queue_ms'] = round((time.perf_counter() - started) * 1000)
        return work(cancel, metrics)

    future = asyncio.wrap_future(_executor.submit(run))
    try:
        while not future.done():
            done, _ = await asyncio.wait({future}, timeout=0.1)
            if not done and await request.is_disconnected():
                raise SearchCancelled
        result = future.result()
        outcome = 'ok'
        return result
    except (asyncio.CancelledError, SearchCancelled):
        outcome = 'cancelled'
        raise
    finally:
        cancel.set()
        _pending.discard(cancel)
        if not future.done():
            future.cancel()
        perf_event('chat.search', status=outcome,
                   duration_ms=round((time.perf_counter() - started) * 1000), **metrics)


def shutdown() -> None:
    global _executor
    for cancel in tuple(_pending):
        cancel.set()
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
