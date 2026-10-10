"""Canonical JSONL turn-boundary regressions."""

import json
import random

import pytest

from backend.memory_engine import _read_turn_records
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


def _full_read(path, target, *, cap, is_interrupt):
    """Reference: the previous whole-file (capped) read."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > cap:
            stream.seek(size - cap)
            stream.readline()
        raw_lines = stream.readlines()
    records = []
    for raw in raw_lines:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                records.append(value)
        except Exception:
            continue
    return slice_turn_records(records, target, is_interrupt=is_interrupt)


_TEXTS = ["问", "问题 ABC", "emoji 😀🎉 混合 ünï", "x" * 300, "长" * 200, "a b\tc"]
_NOT_INTERRUPT = lambda _value: False  # noqa: E731


def _random_transcript(rng, rows):
    lines = []
    for i in range(rows):
        kind = rng.choice(["user", "assistant", "tool_result", "junk", "blank"])
        pad = rng.choice(_TEXTS) * rng.randint(1, 40)
        if kind == "user":
            row = {"type": "user", "uuid": f"u{i}",
                   "message": {"content": rng.choice(_TEXTS) + pad[:5]}}
        elif kind == "assistant":
            row = {"type": "assistant", "uuid": f"a{i}", "message": {"content": [
                {"type": "text", "text": pad}]}}
        elif kind == "tool_result":
            row = {"type": "user", "uuid": f"r{i}", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t", "content": pad}]}}
        elif kind == "junk":
            lines.append(pad.encode("utf-8")[:rng.randint(1, 30)] + b"{")
            continue
        else:
            lines.append(b"")
            continue
        lines.append(json.dumps(row, ensure_ascii=False).encode("utf-8"))
    return lines


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("trailing_newline", [True, False])
def test_tail_read_matches_full_read_on_random_multichunk_files(
        tmp_path, seed, trailing_newline):
    rng = random.Random(seed)
    lines = _random_transcript(rng, rng.randint(40, 120))
    data = b"\n".join(lines) + (b"\n" if trailing_newline else b"")
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    targets = ["", "no such turn", *_TEXTS, *(t + t[:5] for t in _TEXTS)]
    for cap in (len(data) + 10, len(data) // 2, len(data) // 5 + 1):
        for chunk in (1, 7, 64, 500, 4096):
            for target in targets:
                assert _read_turn_records(
                    path, target, is_interrupt=_NOT_INTERRUPT,
                    cap=cap, chunk=chunk,
                ) == _full_read(
                    path, target, cap=cap, is_interrupt=_NOT_INTERRUPT)


def test_tail_read_falls_back_to_file_head_when_target_only_at_head(tmp_path):
    rows = [{"type": "user", "uuid": "head", "message": {"content": "最早的问题"}}]
    rows += [{"type": "assistant", "uuid": f"a{i}",
              "message": {"content": [{"type": "text", "text": "填充😀" * 50}]}}
             for i in range(100)]
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
    got = _read_turn_records(
        path, "最早的问题", is_interrupt=_NOT_INTERRUPT, chunk=256)
    assert got == rows
    assert got[0]["uuid"] == "head"
    assert _read_turn_records(
        path, "不存在", is_interrupt=_NOT_INTERRUPT, chunk=256) == []


def test_tail_read_uses_last_match_and_does_not_cut_multibyte_boundaries(tmp_path):
    rows = []
    for turn in range(30):
        rows.append({"type": "user", "uuid": f"u{turn}",
                     "message": {"content": "重复的问题 😀"}})
        rows.append({"type": "assistant", "uuid": f"a{turn}", "message": {
            "content": [{"type": "text", "text": "答复，多字节：" + "界" * (turn + 1)}]}})
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8"))
    expected = rows[-2:]
    # Sweep every chunk size so each byte offset of the file serves as a window start.
    for chunk in range(1, 400):
        assert _read_turn_records(
            path, "重复的问题 😀", is_interrupt=_NOT_INTERRUPT, chunk=chunk,
        ) == expected


def test_tail_read_handles_empty_file(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"")
    assert _read_turn_records(path, "x", is_interrupt=_NOT_INTERRUPT) == []
