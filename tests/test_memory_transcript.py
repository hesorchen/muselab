"""Canonical JSONL turn-boundary regressions."""

from backend.memory_transcript import slice_turn_records


def test_tool_result_user_record_stays_inside_turn():
    records = [
        {"type": "user", "message": {"content": "first"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "one"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "one"},
        ]}},
        {"type": "user", "message": {"content": "second"}},
    ]
    sliced = slice_turn_records(
        records, "first", is_interrupt=lambda _value: False)
    assert sliced == records[:3]
