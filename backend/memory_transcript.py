"""Pure turn-boundary helpers for canonical Claude CLI JSONL records."""

from __future__ import annotations

from collections.abc import Callable


def _user_text(record: dict) -> str:
    if record.get("type") != "user":
        return ""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _is_real_user_record(
    record: dict,
    is_interrupt: Callable[[object], bool],
) -> bool:
    if record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not is_interrupt(content)
    if not isinstance(content, list):
        return True
    return any(
        isinstance(block, dict)
        and block.get("type") != "tool_result"
        and (
            block.get("type") != "text"
            or (
                str(block.get("text", "")).strip()
                and not is_interrupt(block.get("text"))
            )
        )
        for block in content
    )


def slice_turn_records(
    records: list[dict],
    target_text: str,
    *,
    is_interrupt: Callable[[object], bool],
) -> list[dict]:
    """Return one real user turn, retaining its tool-result user records."""
    normalized_target = " ".join(target_text.split())
    start = -1
    for index, record in enumerate(records):
        normalized = " ".join(_user_text(record).split())
        if normalized == normalized_target or (
            normalized_target and normalized_target[:500] in normalized
        ):
            start = index
    if start < 0:
        return []
    for index in range(start + 1, len(records)):
        if _is_real_user_record(records[index], is_interrupt):
            return records[start:index]
    return records[start:]
