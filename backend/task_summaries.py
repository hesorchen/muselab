"""Bounded presentation contract for background-task summaries."""
from __future__ import annotations

from typing import Any, Mapping


TASK_SUMMARY_PREVIEW_CAP = 2_000


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_task_summary(
    summary: Any,
    *,
    summary_length: Any = None,
    summary_truncated: Any = None,
) -> dict[str, Any]:
    """Return a repeat-safe, bounded task-summary preview."""
    text = "" if summary is None else str(summary)
    received_length = len(text)
    try:
        original_length = int(summary_length)
    except (TypeError, ValueError):
        original_length = received_length
    original_length = max(received_length, original_length)
    preview = text[:TASK_SUMMARY_PREVIEW_CAP]
    truncated = _coerce_bool(summary_truncated) or original_length > len(preview)
    return {
        "summary": preview,
        "summary_length": original_length,
        "summary_truncated": truncated,
    }


def normalize_task_summary_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy one task-status mapping and enforce the summary contract."""
    normalized = dict(value)
    if "summary" not in normalized:
        return normalized
    normalized.update(normalize_task_summary(
        normalized.get("summary"),
        summary_length=normalized.get("summary_length"),
        summary_truncated=normalized.get("summary_truncated"),
    ))
    return normalized
