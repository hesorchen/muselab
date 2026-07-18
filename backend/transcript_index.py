"""Persistent byte-offset index for Claude CLI JSONL transcripts.

The index is intentionally transcript-format aware but UI-format agnostic.  A
caller supplies ``describe_record`` so bubble counting uses the exact same
record expansion code as the endpoint that later renders a window.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import atomic_write_text

# Increment whenever persisted descriptor semantics (bubble expansion, preview,
# tool/task metadata) change, not only when the JSON container shape changes.
SCHEMA_VERSION = 2
_TRANSCRIPT_TYPES = {"user", "assistant", "progress", "system", "attachment"}

@dataclass
class _LockSlot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


_locks_guard = threading.Lock()
_locks: dict[str, _LockSlot] = {}
_index_cache: dict[str, tuple[Path, tuple[int, int] | None, dict[str, Any]]] = {}
_INDEX_CACHE_MAX = 64


@contextmanager
def _sid_lock(sid: str):
    """Serialize one sid without retaining every sid forever.

    ``users`` is incremented before a caller can block on the per-sid lock, so
    the slot cannot disappear while waiters still reference it.  The final
    holder removes the registry entry after releasing the lock.
    """
    with _locks_guard:
        slot = _locks.setdefault(sid, _LockSlot())
        slot.users += 1
    try:
        with slot.lock:
            yield
    finally:
        with _locks_guard:
            slot.users -= 1
            if slot.users == 0 and _locks.get(sid) is slot:
                _locks.pop(sid, None)


def _source_stat(path: Path) -> dict[str, int]:
    st = path.stat()
    return {
        "dev": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _prefix_digest(path: Path, length: int, block: int = 1024 * 1024) -> str:
    """Fingerprint every byte in an indexed prefix.

    Unchanged requests return from ``ensure_index`` before calling this helper.
    On append, however, correctness requires proving that the already-indexed
    prefix was not rewritten in place.  Sampling only the first/last blocks can
    accept stale byte offsets when a sync/restore process changes the middle
    and then grows the file, so append refreshes deliberately hash the prefix.
    """
    if length <= 0:
        return ""
    remaining = length
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(length).encode("ascii"))
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(block, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining:
        raise OSError(f"transcript prefix shortened while hashing: {path}")
    return digest.hexdigest()


def _empty_index(generation: str | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "history_generation": generation or uuid.uuid4().hex,
        "source": {
            "dev": 0,
            "inode": 0,
            "size": 0,
            "mtime_ns": 0,
            "scanned_bytes": 0,
            "prefix_digest": "",
        },
        "records": [],
        "orders": {"normal": [], "full": []},
        "bubble_prefix": {"normal": [0], "full": [0]},
        "tool_use_names": {},
        "task_status": {},
    }


def _index_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None
    source = data.get("source")
    if not isinstance(source, dict) or not isinstance(data.get("records"), list):
        return None
    return data


def _task_status_from_descriptor(desc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for item in desc.get("task_notifications") or []:
        tool_id = str(item.get("tool_use_id") or "")
        if not tool_id:
            continue
        raw_state = str(item.get("status") or "")
        state = raw_state if raw_state in {"completed", "failed", "stopped"} else "done"
        out.append((tool_id, {
            "task_id": item.get("task_id") or "",
            "state": state,
            "summary": item.get("summary") or "",
            "output_file": item.get("output_file") or "",
        }))
    return out


def _append_complete_lines(
    transcript_path: Path,
    index: dict[str, Any],
    start: int,
    end: int,
    describe_record: Callable[[dict[str, Any]], dict[str, Any]],
) -> int:
    """Scan complete newline-terminated records in ``[start, end)``.

    The returned offset is the beginning of an incomplete tail, or ``end`` if
    every byte through ``end`` belongs to a complete line.  Malformed complete
    lines advance the cursor but are deliberately absent from ``records``.
    """
    records = index["records"]
    with transcript_path.open("rb") as handle:
        handle.seek(start)
        while handle.tell() < end:
            line_start = handle.tell()
            raw_line = handle.readline(end - line_start)
            if not raw_line:
                return line_start
            # A torn append remains pending until a later write supplies the
            # newline; re-scan from its beginning next time.
            if not raw_line.endswith(b"\n"):
                return line_start
            line_end = handle.tell()
            raw = raw_line[:-1].strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            record_type = entry.get("type")
            record_uuid = entry.get("uuid")
            if record_type not in _TRANSCRIPT_TYPES or not isinstance(record_uuid, str):
                continue
            desc = describe_record(entry) or {}
            records.append({
                "offset": line_start,
                "length": line_end - line_start,
                "uuid": record_uuid,
                "parent": entry.get("parentUuid"),
                "type": record_type,
                "is_sidechain": bool(entry.get("isSidechain")),
                "team_name": entry.get("teamName"),
                "is_meta": bool(entry.get("isMeta")),
                "compact": bool(entry.get("isCompactSummary")),
                "bubble_count": int(desc.get("bubble_count") or 0),
                "user_preview": desc.get("user_preview") or "",
                "real_user_prompt": bool(desc.get("real_user_prompt")),
                "has_inline_images": bool(desc.get("has_inline_images")),
                "tool_uses": desc.get("tool_uses") or [],
                "task_notifications": desc.get("task_notifications") or [],
            })
    return end


def _rebuild_derived(index: dict[str, Any]) -> None:
    records: list[dict[str, Any]] = index["records"]

    # Full history preserves the old raw-reader contract: first UUID wins,
    # user/assistant records only, chronological file order, no branch filters.
    full: list[int] = []
    full_seen: set[str] = set()
    for i, rec in enumerate(records):
        uid = rec["uuid"]
        if rec["type"] in {"user", "assistant"} and uid not in full_seen:
            full_seen.add(uid)
            full.append(i)

    # Match claude_agent_sdk._build_conversation_chain: last duplicate UUID
    # wins; terminal and leaf tie-breaking use the latest file position.
    by_uuid: dict[str, int] = {}
    for i, rec in enumerate(records):
        by_uuid[rec["uuid"]] = i
    parent_uuids = {rec.get("parent") for rec in records if rec.get("parent")}
    terminals = [i for i, rec in enumerate(records) if rec["uuid"] not in parent_uuids]
    leaves: list[int] = []
    for terminal in terminals:
        cur: int | None = terminal
        seen: set[str] = set()
        while cur is not None:
            rec = records[cur]
            uid = rec["uuid"]
            if uid in seen:
                break
            seen.add(uid)
            if rec["type"] in {"user", "assistant"}:
                leaves.append(cur)
                break
            parent = rec.get("parent")
            cur = by_uuid.get(parent) if parent else None
    main_leaves = [
        i for i in leaves
        if not records[i]["is_sidechain"]
        and not records[i]["team_name"]
        and not records[i]["is_meta"]
    ]
    normal: list[int] = []
    if leaves:
        leaf = max(main_leaves or leaves)
        reverse_chain: list[int] = []
        seen: set[str] = set()
        cur: int | None = leaf
        while cur is not None:
            rec = records[cur]
            uid = rec["uuid"]
            if uid in seen:
                break
            seen.add(uid)
            reverse_chain.append(cur)
            parent = rec.get("parent")
            cur = by_uuid.get(parent) if parent else None
        for i in reversed(reverse_chain):
            rec = records[i]
            if (rec["type"] in {"user", "assistant"}
                    and not rec["is_meta"]
                    and not rec["is_sidechain"]
                    and not rec["team_name"]):
                normal.append(i)

    index["orders"] = {"normal": normal, "full": full}
    prefixes: dict[str, list[int]] = {}
    for name, order in index["orders"].items():
        prefix = [0]
        for i in order:
            prefix.append(prefix[-1] + max(0, int(records[i].get("bubble_count") or 0)))
        prefixes[name] = prefix
    index["bubble_prefix"] = prefixes

    tool_names: dict[str, str] = {}
    statuses: dict[str, dict[str, Any]] = {}
    for rec in records:
        for tool in rec.get("tool_uses") or []:
            tool_id = str(tool.get("id") or "")
            if tool_id:
                tool_names[tool_id] = str(tool.get("name") or "")
        for tool_id, status in _task_status_from_descriptor(rec):
            statuses[tool_id] = status
    index["tool_use_names"] = tool_names
    index["task_status"] = statuses


def ensure_index(
    sid: str,
    transcript_path: Path,
    index_path: Path,
    describe_record: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Load, incrementally extend, or rebuild one transcript index.

    Calls for the same session are single-flight within this process.  Append
    scans begin at ``source.scanned_bytes``; unchanged calls never read the
    transcript body.
    """
    with _sid_lock(sid):
        stat = _source_stat(transcript_path)
        cached = _index_cache.get(sid)
        index = (
            cached[2]
            if cached is not None
            and cached[0] == index_path
            and cached[1] == _index_signature(index_path)
            else None
        )
        if index is not None:
            source = index["source"]
            if all(source.get(key) == stat[key]
                   for key in ("dev", "inode", "size", "mtime_ns")):
                return index
        if index is None:
            index = _load(index_path)
        rebuild = index is None
        if index is not None:
            source = index["source"]
            scanned_before = int(source.get("scanned_bytes") or 0)
            prefix_changed = (
                stat["size"] > int(source.get("size") or 0)
                and source.get("prefix_digest", "")
                != _prefix_digest(transcript_path, scanned_before)
            )
            rebuild = (
                source.get("dev") != stat["dev"]
                or source.get("inode") != stat["inode"]
                or stat["size"] < scanned_before
                or stat["size"] < int(source.get("size") or 0)
                or prefix_changed
                # Same-size content changes cannot be an append.
                or (stat["size"] == source.get("size")
                    and stat["mtime_ns"] != source.get("mtime_ns"))
            )
        if rebuild:
            index = _empty_index()
            start = 0
        else:
            start = int(index["source"].get("scanned_bytes") or 0)

        changed = rebuild or stat["size"] != index["source"].get("size")
        if changed:
            records_before = len(index["records"])
            scanned = _append_complete_lines(
                transcript_path, index, start, stat["size"], describe_record)
            if not rebuild and len(index["records"]) != records_before:
                index["history_generation"] = uuid.uuid4().hex
            index["source"] = {
                **stat,
                "scanned_bytes": scanned,
                "prefix_digest": _prefix_digest(transcript_path, scanned),
            }
            _rebuild_derived(index)
            atomic_write_text(
                index_path,
                json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            )
        _index_cache.pop(sid, None)
        _index_cache[sid] = (index_path, _index_signature(index_path), index)
        while len(_index_cache) > _INDEX_CACHE_MAX:
            _index_cache.pop(next(iter(_index_cache)))
        return index


def record_indices_for_bubble_window(
    index: dict[str, Any],
    order: str,
    start: int,
    end: int,
) -> tuple[list[int], int, int]:
    """Return records intersecting a bubble range and their exact slice bounds."""
    order_ids = index["orders"][order]
    prefix = index["bubble_prefix"][order]
    total = prefix[-1]
    start = max(0, min(start, total))
    end = max(start, min(end, total))
    if start == end:
        return [], 0, total
    first = min(len(order_ids) - 1, bisect_right(prefix, start) - 1)
    last = min(len(order_ids), bisect_left(prefix, end))
    return order_ids[first:last], start - prefix[first], total


def record_indices_around_uuid(
    index: dict[str, Any],
    uuid_value: str,
    before: int,
    after: int,
    *,
    limit: int = 0,
) -> tuple[list[int], int, int, int, int]:
    """Return a full-order *bubble* window containing ``uuid_value``.

    ``before`` and ``after`` are bubble counts, not JSONL-record counts.  When
    ``limit`` is supplied (the legacy ``around_uuid + limit`` request shape),
    it caps the whole returned bubble window and reserves space for at least
    the first bubble produced by the target record.  The returned tuple is
    ``(record_ids, inner_start, window_start, window_end, total)``; callers
    shape only those records and then apply the exact inner bubble slice.
    """
    order = index["orders"]["full"]
    prefix = index["bubble_prefix"]["full"]
    total = prefix[-1]
    pos = next((i for i, rec_i in enumerate(order)
                if index["records"][rec_i]["uuid"] == uuid_value), -1)
    if pos < 0:
        return [], 0, 0, 0, total

    target_start = prefix[pos]
    target_end = prefix[pos + 1]
    if target_end <= target_start:
        return [], 0, 0, 0, total

    if limit > 0:
        # A multi-bubble record may itself exceed the cap.  Keeping the slice
        # anchored at target_start still guarantees the requested UUID is in
        # the response without allowing a pathological record to explode the
        # frontend's mounted-window budget.
        span = min(limit, target_end - target_start)
        context = max(0, limit - span)
        before_budget = context // 2
        after_budget = context - before_budget
        start = max(0, target_start - before_budget)
        end = min(total, target_start + span + after_budget)
        # Rebalance at either history boundary so a nominal limit remains a
        # limit-sized window whenever enough bubbles exist.
        if end - start < limit:
            start = max(0, end - limit)
            end = min(total, start + limit)
        # Boundary rebalancing must never shift beyond the target anchor.
        if not (start <= target_start < end):
            start = max(0, min(target_start, total - limit))
            end = min(total, start + limit)
    else:
        start = max(0, target_start - max(0, before))
        end = min(total, target_end + max(0, after))

    record_ids, inner_start, _ = record_indices_for_bubble_window(
        index, "full", start, end)
    return record_ids, inner_start, start, end, total


def read_records(
    transcript_path: Path,
    index: dict[str, Any],
    record_indices: list[int],
) -> list[dict[str, Any]]:
    """Seek and parse only selected indexed JSONL records."""
    out: list[dict[str, Any]] = []
    records = index["records"]
    with transcript_path.open("rb") as handle:
        for record_i in record_indices:
            meta = records[record_i]
            handle.seek(meta["offset"])
            raw = handle.read(meta["length"]).strip()
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out
