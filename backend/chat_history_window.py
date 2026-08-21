"""Presentation-only history coordinates over canonical transcript records.

Claude CLI JSONL remains the sole canonical transcript.  This module only
assembles read-time windows and optional display snapshots; it never writes or
rewrites transcript bytes.  ``backend.chat`` retains compatibility facades.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .chat_presentation import ui_messages_metrics


ShapeRecords = Callable[[Path, dict, list[int], dict[str, dict]], list[dict]]

CHUNK_TARGET_HEIGHT = 4800
CHUNK_MAX_BYTES = 384 * 1024
CHUNK_MAX_BLOCKS = 96


def combined_generation(base: str, snapshot_generation: str) -> str:
    if not snapshot_generation:
        return base
    return f"{base or 'none'}~cancelled-{snapshot_generation}"


def history_segments(
    index: dict | None,
    snapshots: list[dict],
    order: str,
) -> tuple[list[dict], int]:
    """Build virtual display segments without changing canonical records."""
    records = (index or {}).get("records") or []
    orders = (index or {}).get("orders") or {}
    record_ids = list(orders.get(order) or [])
    positions: dict[str, int] = {}
    for position, record_id in enumerate(record_ids):
        uid = str(records[record_id].get("uuid") or "")
        if uid:
            positions[uid] = position
    full_uuids = {
        str(records[record_id].get("uuid") or "")
        for record_id in orders.get("full") or []
    }
    prefix = ((index or {}).get("bubble_prefix") or {}).get(order) or [0]

    placed: dict[int, list[dict]] = {}
    included: list[dict] = []
    for snapshot in snapshots:
        anchors = snapshot.get("anchors") or {}
        anchor = anchors.get(order) or {}
        anchor_uuid = str(anchor.get("uuid") or "")
        if anchor_uuid and anchor_uuid in positions:
            position = positions[anchor_uuid]
        elif order == "normal" and anchor_uuid and anchor_uuid in full_uuids:
            continue
        else:
            target = max(0, int(anchor.get("total") or 0))
            position = -1
            for pos in range(len(record_ids)):
                reached = int(prefix[pos + 1] if pos + 1 < len(prefix) else 0)
                if reached > target:
                    break
                position = pos
        placed.setdefault(position, []).append(snapshot)
        included.append(snapshot)

    for group in placed.values():
        group.sort(key=lambda item: (
            int(item.get("started_at_ms") or 0), str(item.get("turn_id") or "")))

    hidden = {
        str(uid)
        for snapshot in included
        for uid in (snapshot.get("hidden_uuids") or [])
        if uid
    }
    segments: list[dict] = []

    def append_snapshots(position: int) -> None:
        for snapshot in placed.get(position, []):
            messages = snapshot.get("messages") or []
            if messages:
                segments.append({
                    "kind": "snapshot",
                    "snapshot": snapshot,
                    "count": len(messages),
                })

    append_snapshots(-1)
    for position, record_id in enumerate(record_ids):
        record = records[record_id]
        if str(record.get("uuid") or "") not in hidden:
            count = max(0, int(record.get("bubble_count") or 0))
            if count:
                segments.append({
                    "kind": "record",
                    "record_id": record_id,
                    "count": count,
                })
        append_snapshots(position)
    snapshot_turns = sum(
        1
        for snapshot in included
        if any(
            isinstance(message, dict) and message.get("role") == "user"
            for message in (snapshot.get("messages") or [])
        )
    )
    return segments, snapshot_turns


def history_stats(
    index: dict | None,
    snapshots: list[dict],
    order: str,
) -> tuple[int, int]:
    segments, snapshot_turns = history_segments(index, snapshots, order)
    total = sum(int(segment.get("count") or 0) for segment in segments)
    records = (index or {}).get("records") or []
    canonical_turns = sum(
        1
        for segment in segments
        if segment.get("kind") == "record"
        and records[segment["record_id"]].get("real_user_prompt")
    )
    return total, canonical_turns + snapshot_turns


def build_history_manifest(
    index: dict | None,
    snapshots: list[dict],
    order: str,
    *,
    generation: str = "",
) -> dict:
    """Build a lightweight bubble-coordinate manifest for bounded UI reads.

    Boundaries use estimated rendered height, serialized response bytes and UI
    block count. Turn count is deliberately absent: one agentic turn can contain
    hundreds of independently rendered tool/thinking/result blocks.
    """
    segments, _ = history_segments(index, snapshots, order)
    records = (index or {}).get("records") or []
    chunks: list[dict[str, int]] = []
    cursor = 0
    estimated_top = 0
    current: dict[str, int] | None = None

    def close_current() -> None:
        nonlocal current, estimated_top
        if current is None or current["block_count"] <= 0:
            current = None
            return
        current["id"] = len(chunks)
        current["end"] = current["start"] + current["block_count"]
        current["estimated_top"] = estimated_top
        chunks.append(current)
        estimated_top += current["estimated_height"]
        current = None

    for segment in segments:
        count = max(0, int(segment.get("count") or 0))
        if not count:
            continue
        if segment.get("kind") == "record":
            record_id = int(segment.get("record_id") or 0)
            record = records[record_id] if 0 <= record_id < len(records) else {}
            metrics = list(record.get("bubble_metrics") or [])
            fallback_height = max(88, int(record.get("estimated_height") or 0) // count)
            fallback_bytes = max(64, int(record.get("serialized_bytes") or 0) // count)
        else:
            snapshot_metrics = ui_messages_metrics(
                list((segment.get("snapshot") or {}).get("messages") or []))
            metrics = list(snapshot_metrics.get("bubble_metrics") or [])
            fallback_height = 88
            fallback_bytes = 256

        for local in range(count):
            metric = metrics[local] if local < len(metrics) else {}
            height = max(64, int(metric.get("estimated_height") or fallback_height)) + 8
            size = max(1, int(metric.get("serialized_bytes") or fallback_bytes))
            would_exceed = current is not None and (
                current["estimated_height"] + height > CHUNK_TARGET_HEIGHT
                or current["serialized_bytes"] + size > CHUNK_MAX_BYTES
                or current["block_count"] + 1 > CHUNK_MAX_BLOCKS
            )
            if would_exceed:
                close_current()
            if current is None:
                current = {
                    "start": cursor,
                    "block_count": 0,
                    "estimated_height": 0,
                    "serialized_bytes": 0,
                }
            current["block_count"] += 1
            current["estimated_height"] += height
            current["serialized_bytes"] += size
            cursor += 1
    close_current()
    return {
        "version": 1,
        "generation": generation,
        "order": order,
        "total_blocks": cursor,
        "estimated_height": estimated_top,
        "chunks": chunks,
    }


def history_window(
    transcript_path: Path | None,
    index: dict | None,
    snapshots: list[dict],
    annotations: dict[str, dict],
    order: str,
    *,
    shape_records: ShapeRecords,
    tail: int = 0,
    offset: int = -1,
    limit: int = 0,
) -> tuple[list[dict], int, int, bool]:
    """Read one window from canonical records plus display-only snapshots."""
    segments, _ = history_segments(index, snapshots, order)
    total = sum(int(segment.get("count") or 0) for segment in segments)
    if offset >= 0:
        start = max(0, min(offset, total))
        end = total if limit <= 0 else min(total, start + limit)
    elif tail > 0:
        start = max(0, total - tail)
        end = total
    else:
        start, end = 0, total

    pieces: list[tuple[str, Any, int, int]] = []
    selected_record_ids: list[int] = []
    cursor = 0
    for segment in segments:
        count = int(segment.get("count") or 0)
        seg_start, seg_end = cursor, cursor + count
        cursor = seg_end
        overlap_start = max(start, seg_start)
        overlap_end = min(end, seg_end)
        if overlap_start >= overlap_end:
            continue
        local_start = overlap_start - seg_start
        local_end = overlap_end - seg_start
        if segment["kind"] == "record":
            record_id = int(segment["record_id"])
            selected_record_ids.append(record_id)
            pieces.append(("record", record_id, local_start, local_end))
        else:
            pieces.append((
                "snapshot", segment["snapshot"], local_start, local_end))

    shaped_by_record: dict[int, list[dict]] = {}
    if transcript_path is not None and index is not None and selected_record_ids:
        shaped = shape_records(
            transcript_path, index, selected_record_ids, annotations)
        shaped_cursor = 0
        records = index.get("records") or []
        for record_id in selected_record_ids:
            count = max(0, int(records[record_id].get("bubble_count") or 0))
            shaped_by_record[record_id] = shaped[
                shaped_cursor:shaped_cursor + count]
            shaped_cursor += count

    window: list[dict] = []
    for kind, source, local_start, local_end in pieces:
        if kind == "record":
            messages = shaped_by_record.get(int(source), [])
        else:
            started_at_ms = int(source.get("started_at_ms") or 0)
            turn_id = str(source.get("turn_id") or "")
            messages = [dict(message)
                        for message in (source.get("messages") or [])]
            for message in messages:
                if turn_id:
                    message.setdefault(
                        "_turn_origin_uuid", f"terminal:{turn_id}")
                if started_at_ms > 0:
                    message.setdefault("turn_started_at", started_at_ms)
        window.extend(messages[local_start:local_end])
    return window, total, start, end < total


def history_window_around_uuid(
    transcript_path: Path,
    index: dict,
    snapshots: list[dict],
    annotations: dict[str, dict],
    uuid_value: str,
    before: int,
    after: int,
    *,
    shape_records: ShapeRecords,
    limit: int = 0,
) -> tuple[list[dict], int, int, bool] | None:
    """Read an around-UUID window in the same virtual display coordinates."""
    segments, _ = history_segments(index, snapshots, "full")
    records = index.get("records") or []
    total = sum(int(segment.get("count") or 0) for segment in segments)
    cursor = 0
    target_start = -1
    target_end = -1
    for segment in segments:
        count = max(0, int(segment.get("count") or 0))
        if segment.get("kind") == "record":
            record_id = int(segment.get("record_id") or 0)
            if (
                0 <= record_id < len(records)
                and str(records[record_id].get("uuid") or "") == uuid_value
            ):
                target_start = cursor
                target_end = cursor + count
                break
        cursor += count
    if target_start < 0 or target_end <= target_start:
        return None

    if limit > 0:
        span = min(limit, target_end - target_start)
        context = max(0, limit - span)
        before_budget = context // 2
        after_budget = context - before_budget
        start = max(0, target_start - before_budget)
        end = min(total, target_start + span + after_budget)
        if end - start < limit:
            start = max(0, end - limit)
            end = min(total, start + limit)
        if not (start <= target_start < end):
            start = max(0, min(target_start, total - limit))
            end = min(total, start + limit)
    else:
        start = max(0, target_start - max(0, before))
        end = min(total, target_end + max(0, after))

    window, window_total, win_offset, has_later = history_window(
        transcript_path,
        index,
        snapshots,
        annotations,
        "full",
        shape_records=shape_records,
        offset=start,
        limit=end - start,
    )
    return window, window_total, win_offset, has_later
