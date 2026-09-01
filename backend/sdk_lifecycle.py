"""Safe, JSON-ready projections of Claude Agent SDK lifecycle values.

The SDK deliberately passes some future-facing dictionaries through without
filtering.  MuseLab must not copy those objects directly into SSE frames,
sidecars, logs, or browser state: origins can contain peer message bodies and
``ResultError.data`` contains the complete terminal protocol frame.

This module is intentionally pure.  It accepts SDK values, returns bounded
plain Python objects, and owns no chat/session state.
"""

from __future__ import annotations

import math
from typing import Any, Literal, TypedDict

from claude_agent_sdk import ResultError


TurnStatus = Literal["completed", "failed", "cancelled", "stopped"]


class NormalizedOrigin(TypedDict):
    kind: str
    subkind: str | None
    task_id: str | None
    source: Literal["sdk"]


class ResultErrorInfo(TypedDict):
    subtype: str | None
    errors: list[str]
    result: str | None
    api_error_status: int | None
    terminal_reason: str
    session_id: str | None
    exit_code: int | None


_MAX_ORIGIN_KIND_LENGTH = 64
_MAX_ORIGIN_SUBKIND_LENGTH = 64
_MAX_ORIGIN_TASK_ID_LENGTH = 256
_MAX_TERMINAL_REASON_LENGTH = 128

_MAX_MODEL_USAGE_ENTRIES = 32
_MAX_MODEL_ID_LENGTH = 256
_MAX_PROVIDER_LENGTH = 64
_MAX_USAGE_INTEGER = (1 << 63) - 1
_MAX_COST_USD = 1_000_000_000.0

_MAX_ERROR_PARTS = 16
_MAX_ERROR_PART_LENGTH = 2_048
_MAX_RESULT_LENGTH = 8_192
_MAX_ERROR_IDENTIFIER_LENGTH = 128
_MAX_SESSION_ID_LENGTH = 256
_MAX_EXIT_CODE = (1 << 31) - 1

_MODEL_USAGE_INTEGER_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "webSearchRequests",
    "contextWindow",
    "maxOutputTokens",
)


def _safe_identifier(value: Any, *, max_length: int) -> str | None:
    """Return one bounded, printable identifier without changing its value."""
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if value != value.strip() or not all(char.isprintable() for char in value):
        return None
    return value


def _bounded_display_text(value: Any, *, max_length: int) -> str | None:
    """Bound user-facing error prose and neutralize control characters."""
    if not isinstance(value, str) or not value:
        return None

    truncated = len(value) > max_length
    sample = value[:max_length]
    cleaned = "".join(
        char if char in {"\n", "\t"} or char.isprintable() else "�"
        for char in sample
    ).strip()
    if not cleaned:
        return None
    if truncated:
        cleaned = cleaned[: max_length - 1] + "…"
    return cleaned


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_USAGE_INTEGER:
        return None
    return value


def _safe_cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or parsed > _MAX_COST_USD:
        return None
    return parsed


def normalize_origin(value: Any) -> NormalizedOrigin | None:
    """Project an SDK ``MessageOrigin`` onto MuseLab's privacy-safe fields.

    Unknown but well-formed future ``kind`` values are retained.  All content
    and routing fields (``body``, ``from``, ``server``, peer names/session ids,
    verified pids, and arbitrary future keys) are intentionally discarded.
    """
    if not isinstance(value, dict):
        return None
    kind = _safe_identifier(
        value.get("kind"), max_length=_MAX_ORIGIN_KIND_LENGTH)
    if kind is None:
        return None
    return {
        "kind": kind,
        "subkind": _safe_identifier(
            value.get("subkind"), max_length=_MAX_ORIGIN_SUBKIND_LENGTH),
        "task_id": _safe_identifier(
            value.get("senderTaskId"), max_length=_MAX_ORIGIN_TASK_ID_LENGTH),
        "source": "sdk",
    }


def normalize_terminal_reason(value: Any) -> str:
    """Return a bounded SDK terminal-reason token, or ``""`` when invalid."""
    return _safe_identifier(
        value, max_length=_MAX_TERMINAL_REASON_LENGTH) or ""


def terminal_status(
    reason: Any,
    *,
    is_error: bool = False,
    cancelled: bool = False,
) -> TurnStatus:
    """Derive MuseLab's user-visible status with cancellation kept sticky."""
    normalized = normalize_terminal_reason(reason)
    if cancelled or normalized in {"aborted_streaming", "aborted_tools"}:
        return "cancelled"
    if normalized == "max_turns":
        return "stopped"
    if is_error:
        return "failed"
    return "completed"


def normalize_model_usage(value: Any) -> dict[str, dict[str, int | float | str]]:
    """Return a bounded whitelist of SDK ``model_usage`` entries.

    The SDK's inner camelCase field names are preserved so the projection stays
    lossless and recognizable.  Malformed fields are dropped independently;
    unknown/nested fields are never copied.
    """
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, int | float | str]] = {}
    for index, (raw_model, raw_usage) in enumerate(value.items()):
        # Cap inspected input, not just accepted output, to keep adversarial
        # dictionaries from turning normalization into unbounded work.
        if index >= _MAX_MODEL_USAGE_ENTRIES:
            break
        model = _safe_identifier(raw_model, max_length=_MAX_MODEL_ID_LENGTH)
        if model is None or not isinstance(raw_usage, dict):
            continue

        usage: dict[str, int | float | str] = {}
        for field in _MODEL_USAGE_INTEGER_FIELDS:
            count = _safe_non_negative_int(raw_usage.get(field))
            if count is not None:
                usage[field] = count

        cost = _safe_cost(raw_usage.get("costUSD"))
        if cost is not None:
            usage["costUSD"] = cost

        canonical_model = _safe_identifier(
            raw_usage.get("canonicalModel"), max_length=_MAX_MODEL_ID_LENGTH)
        if canonical_model is not None:
            usage["canonicalModel"] = canonical_model
        provider = _safe_identifier(
            raw_usage.get("provider"), max_length=_MAX_PROVIDER_LENGTH)
        if provider is not None:
            usage["provider"] = provider

        if usage:
            normalized[model] = usage
    return normalized


def result_error_info(value: Any) -> ResultErrorInfo | None:
    """Return bounded structured fields from an SDK ``ResultError``.

    This function deliberately never reads or returns ``ResultError.data``.
    The raw terminal frame can contain fields outside this public contract.
    """
    if not isinstance(value, ResultError):
        return None

    raw_errors = value.errors if isinstance(value.errors, (list, tuple)) else ()
    errors: list[str] = []
    for index, raw_error in enumerate(raw_errors):
        if index >= _MAX_ERROR_PARTS:
            break
        error = _bounded_display_text(
            raw_error, max_length=_MAX_ERROR_PART_LENGTH)
        if error is not None and error not in errors:
            errors.append(error)

    status = value.api_error_status
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or status < 100
        or status > 599
    ):
        status = None

    exit_code = value.exit_code
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or abs(exit_code) > _MAX_EXIT_CODE
    ):
        exit_code = None

    return {
        "subtype": _safe_identifier(
            value.subtype, max_length=_MAX_ERROR_IDENTIFIER_LENGTH),
        "errors": errors,
        "result": _bounded_display_text(
            value.result, max_length=_MAX_RESULT_LENGTH),
        "api_error_status": status,
        "terminal_reason": normalize_terminal_reason(value.terminal_reason),
        "session_id": _safe_identifier(
            value.session_id, max_length=_MAX_SESSION_ID_LENGTH),
        "exit_code": exit_code,
    }


__all__ = [
    "NormalizedOrigin",
    "ResultErrorInfo",
    "TurnStatus",
    "normalize_model_usage",
    "normalize_origin",
    "normalize_terminal_reason",
    "result_error_info",
    "terminal_status",
]
