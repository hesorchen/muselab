from __future__ import annotations

import json
import shutil
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import get_session_messages
from claude_agent_sdk._internal.session_store import project_key_for_directory

from backend import context_recovery as recovery


def _uuid() -> str:
    return str(uuid.uuid4())


def _entry(
    session_id: str,
    entry_type: str,
    content: object,
    *,
    parent: str | None,
    entry_uuid: str | None = None,
    **extra: object,
) -> dict:
    uid = entry_uuid or _uuid()
    return {
        "type": entry_type,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": session_id,
        "timestamp": "2026-08-12T00:00:00.000Z",
        "message": {"role": entry_type, "content": content},
        **extra,
    }


def _write_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: list[dict],
    session_id: str,
) -> tuple[Path, Path]:
    config_dir = tmp_path / "claude-config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    project_dir = config_dir / "projects" / project_key_for_directory(cwd)
    project_dir.mkdir(parents=True)
    source = project_dir / f"{session_id}.jsonl"
    source.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    source.chmod(0o600)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return cwd, source


def _summary_content(path: Path) -> str:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    summaries = [record for record in records if record.get("isCompactSummary")]
    assert len(summaries) == 1
    return summaries[0]["message"]["content"]


def test_sdk_history_starts_at_bounded_summary_and_source_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sid = _uuid()
    user_id = _uuid()
    assistant_id = _uuid()
    meta_id = _uuid()
    error_id = _uuid()
    huge_meta_marker = "HUGE_META_MUST_NOT_ENTER_SUMMARY"
    tool_marker = "TOOL_PAYLOAD_MUST_NOT_ENTER_SUMMARY"
    binary_marker = "BINARY_SUFFIX_MUST_NOT_ENTER_SUMMARY"
    entries = [
        _entry(
            source_sid,
            "user",
            "visible user request",
            parent=None,
            entry_uuid=user_id,
        ),
        _entry(
            source_sid,
            "assistant",
            [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "visible assistant answer"},
                {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": tool_marker},
            ],
            parent=user_id,
            entry_uuid=assistant_id,
        ),
        _entry(
            source_sid,
            "user",
            huge_meta_marker + ("x" * 600_000),
            parent=assistant_id,
            entry_uuid=meta_id,
            isMeta=True,
        ),
        _entry(
            source_sid,
            "assistant",
            "API Error: 400 Your input exceeded the context window of this model.",
            parent=meta_id,
            entry_uuid=error_id,
            isApiErrorMessage=True,
        ),
        _entry(
            source_sid,
            "user",
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": ("A" * 2048) + binary_marker,
                    },
                },
                {"type": "text", "text": "latest visible request"},
            ],
            parent=error_id,
        ),
    ]
    cwd, source = _write_session(tmp_path, monkeypatch, entries, source_sid)
    source_before = source.read_bytes()

    result = recovery.create_recovery_fork(
        source_sid,
        source,
        cwd,
        "Recovered session",
        pre_tokens=360_000,
        model_config_context=400_000,
    )

    assert source.read_bytes() == source_before
    assert result.path != source
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    messages = get_session_messages(result.session_id, directory=str(cwd))
    assert len(messages) == 1
    assert messages[0].uuid == result.summary_uuid
    summary = messages[0].message["content"]
    assert "visible user request" in summary
    assert "visible assistant answer" in summary
    assert "latest visible request" in summary
    assert huge_meta_marker not in summary
    assert tool_marker not in summary
    assert binary_marker not in summary
    assert "private reasoning" not in summary
    assert "API Error: 400" not in summary
    assert result.stats.visible_messages == 3
    assert result.stats.included_messages == 3


def test_summary_uses_latest_messages_and_honours_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sid = _uuid()
    parent = None
    entries: list[dict] = []
    for index in range(8):
        uid = _uuid()
        entries.append(
            _entry(
                source_sid,
                "user" if index % 2 == 0 else "assistant",
                f"message-{index}-" + (str(index) * 80),
                parent=parent,
                entry_uuid=uid,
            )
        )
        parent = uid
    cwd, source = _write_session(tmp_path, monkeypatch, entries, source_sid)

    def fake_fork(session_id: str, *, directory: str, title: str | None):
        assert session_id == source_sid
        assert directory == str(cwd)
        assert title is None
        fork_sid = _uuid()
        shutil.copyfile(source, source.with_name(f"{fork_sid}.jsonl"))
        return SimpleNamespace(session_id=fork_sid)

    result = recovery.create_recovery_fork(
        source_sid,
        source,
        cwd,
        max_message_chars=40,
        max_total_chars=100,
        fork_session_fn=fake_fork,
    )
    summary = _summary_content(result.path)

    assert result.stats.excerpt_chars <= 100
    assert result.stats.max_message_chars == 40
    assert result.stats.max_total_chars == 100
    assert result.stats.truncated_messages > 0
    assert result.stats.omitted_messages > 0
    assert "message-7-" in summary
    assert "message-0-" not in summary


def test_failure_after_sdk_fork_rolls_back_only_the_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sid = _uuid()
    entry = _entry(source_sid, "user", "keep the source", parent=None)
    cwd, source = _write_session(tmp_path, monkeypatch, [entry], source_sid)
    source_before = source.read_bytes()
    created: list[Path] = []

    def fake_fork(session_id: str, *, directory: str, title: str | None):
        fork_sid = _uuid()
        fork_path = source.with_name(f"{fork_sid}.jsonl")
        shutil.copyfile(source, fork_path)
        created.append(fork_path)
        return SimpleNamespace(session_id=fork_sid)

    def fail_append(path: Path, records: tuple[dict, ...]) -> None:
        raise OSError("simulated atomic append failure")

    monkeypatch.setattr(recovery, "_atomic_append_records", fail_append)
    with pytest.raises(OSError, match="simulated atomic append failure"):
        recovery.create_recovery_fork(
            source_sid,
            source,
            cwd,
            fork_session_fn=fake_fork,
        )

    assert source.read_bytes() == source_before
    assert len(created) == 1
    assert not created[0].exists()
