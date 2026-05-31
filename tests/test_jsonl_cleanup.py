"""Starter regression tests for backend.jsonl_cleanup.

jsonl_cleanup rewrites Claude Code session history in place (atomic
tmp + rename) to strip thinking blocks whose signatures Anthropic's
resume API would reject. Because it mutates real conversation data,
the high-risk invariants are:

  1. Valid (long-signature) thinking blocks are NEVER dropped.
  2. Invalid (missing / empty / short-signature) thinking blocks ARE
     dropped, but the surrounding visible text is preserved verbatim.
  3. A message that would end up empty gets a placeholder, never an
     empty content[] (Anthropic rejects that).
  4. A clean file is a true no-op — not even rewritten (idempotent).
  5. Malformed / non-message lines are left byte-for-byte intact —
     we must never silently delete data we don't understand.

These are pure-function tests (no network, no SDK) — safe in CI.

What is still uncovered (follow-up): clean_session() project-dir
discovery against a fake ~/.claude/projects tree, clean_all_under()
recursive sweep ordering, and the write-failure error path.
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.jsonl_cleanup import (
    MIN_SIG_LEN,
    clean_jsonl,
    is_invalid_thinking,
)


_GOOD_SIG = "s" * (MIN_SIG_LEN + 10)   # long enough to look real
_BAD_SIG = "s" * (MIN_SIG_LEN - 1)     # one char under the threshold


def test_is_invalid_thinking_classification():
    # Missing signature → invalid.
    assert is_invalid_thinking({"type": "thinking", "text": "x"}) is True
    # Empty signature → invalid.
    assert is_invalid_thinking({"type": "thinking", "signature": ""}) is True
    # Short signature → invalid.
    assert is_invalid_thinking({"type": "thinking", "signature": _BAD_SIG}) is True
    # Non-string signature → invalid.
    assert is_invalid_thinking({"type": "thinking", "signature": 123}) is True
    # Long signature → valid, must be kept.
    assert is_invalid_thinking({"type": "thinking", "signature": _GOOD_SIG}) is False
    # Not a thinking block → not our business.
    assert is_invalid_thinking({"type": "text", "text": "hi"}) is False
    assert is_invalid_thinking("not a dict") is False


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n",
        encoding="utf-8",
    )


def test_drops_invalid_thinking_but_keeps_visible_text(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "thinking", "signature": _BAD_SIG, "thinking": "scratch"},
            {"type": "text", "text": "the visible answer"},
        ]}},
    ])
    report = clean_jsonl(p)
    assert report.dirty is True
    assert report.lines_changed == 1
    assert report.blocks_dropped == 1

    out = json.loads(p.read_text(encoding="utf-8").strip())
    blocks = out["message"]["content"]
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "the visible answer"}


def test_keeps_valid_thinking_block(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "thinking", "signature": _GOOD_SIG, "thinking": "real"},
            {"type": "text", "text": "answer"},
        ]}},
    ])
    report = clean_jsonl(p)
    assert report.dirty is False
    assert report.blocks_dropped == 0
    # Untouched.
    out = json.loads(p.read_text(encoding="utf-8").strip())
    assert len(out["message"]["content"]) == 2


def test_empty_content_gets_placeholder_not_empty_array(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "thinking", "signature": "", "thinking": "only thinking"},
        ]}},
    ])
    report = clean_jsonl(p)
    assert report.dirty is True
    out = json.loads(p.read_text(encoding="utf-8").strip())
    content = out["message"]["content"]
    assert content, "content[] must never be left empty"
    assert content[0]["type"] == "text"
    assert content[0]["text"]  # non-empty placeholder


def test_clean_file_is_noop_and_not_rewritten(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "text", "text": "plain answer"},
        ]}},
    ])
    before = p.read_bytes()
    mtime_before = p.stat().st_mtime_ns
    report = clean_jsonl(p)
    assert report.dirty is False
    # Bytes identical and (since any_change was False) no rewrite happened.
    assert p.read_bytes() == before
    assert p.stat().st_mtime_ns == mtime_before


def test_malformed_and_non_message_lines_preserved(tmp_path):
    p = tmp_path / "s.jsonl"
    # Mix: a garbage line, a line with no "message" key, and a real dirty one.
    p.write_text(
        "this is not json\n"
        + json.dumps({"type": "summary", "text": "no message key"}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "signature": _BAD_SIG},
            {"type": "text", "text": "kept"},
        ]}}) + "\n",
        encoding="utf-8",
    )
    report = clean_jsonl(p)
    assert report.lines_total == 3
    assert report.lines_changed == 1
    lines = p.read_text(encoding="utf-8").splitlines()
    # Garbage line preserved byte-for-byte.
    assert lines[0] == "this is not json"
    # Non-message line preserved.
    assert json.loads(lines[1])["text"] == "no message key"
    # Dirty line cleaned.
    assert json.loads(lines[2])["message"]["content"] == [
        {"type": "text", "text": "kept"}
    ]
