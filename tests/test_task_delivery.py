"""Real file and Git evidence contracts; no provider calls or personal data."""

import inspect
import json
import subprocess
import uuid
import sys

import pytest


@pytest.fixture
def evidence(monkeypatch, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Safe checkpoint previews require Linux file notifications")
    from backend import task_delivery, file_checkpoints

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(task_delivery.sess, "SESS_DIR", tmp_path / "sessions")
    sid, turn = str(uuid.uuid4()), str(uuid.uuid4())
    task_delivery.begin(sid, turn, root)
    file_checkpoints.begin(sid, turn, root)
    return task_delivery, file_checkpoints, root, sid, turn


def tracked_write(context, target="result.txt", before="before", after="after", tool_id="tool_1"):
    _, checkpoints, root, sid, turn = context
    path = root / target
    if before is not None:
        path.write_text(before)
    payload = {
        "session_id": sid,
        "cwd": str(root),
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": after},
        "tool_use_id": tool_id,
    }
    checkpoints.observe(sid, turn, root, "PreToolUse", payload, tool_id)
    path.write_text(after)
    checkpoints.observe(sid, turn, root, "PostToolUse", payload, tool_id)
    return path


def test_real_sdk_shapes_keep_exit_code_unknown_and_link_message(evidence):
    from claude_agent_sdk import AssistantMessage, UserMessage, ToolUseBlock, ToolResultBlock

    delivery, _, root, sid, turn = evidence
    assistant = AssistantMessage(
        content=[
            ToolUseBlock(
                id="tool_bash",
                name="Bash",
                input={"command": "python -m pytest tests/test_example.py"},
            )
        ],
        model="test-model",
    )
    assistant.uuid = str(uuid.uuid4())
    delivery.observe_message(sid, turn, assistant)
    # Actual Bash shape contains stdout/stderr/interrupted, not exit_code.
    message = UserMessage(
        content=[ToolResultBlock(tool_use_id="tool_bash", content="1 passed", is_error=False)],
        tool_use_result={"stdout": "1 passed", "stderr": "", "interrupted": False},
    )
    delivery.observe_message(sid, turn, message)
    result = delivery.report(sid, root)
    command = result["commands"][0]
    assert command["status"] == "tool_completed"
    assert command["exit_code"] is None
    assert command["message_id"] == assistant.uuid
    assert "some_commands_have_no_structured_exit_code" in result["unverified"]
    assert "1 passed" not in json.dumps(delivery.load(sid))
    message.content[0].is_error = True
    delivery.observe_message(sid, turn, message)
    assert delivery.report(sid, root)["commands"][0]["status"] == "tool_failed"


def test_assistant_claim_never_creates_command_evidence(evidence):
    from claude_agent_sdk import AssistantMessage, TextBlock

    delivery, _, root, sid, turn = evidence
    delivery.observe_message(
        sid,
        turn,
        AssistantMessage(
            content=[TextBlock(text="All tests passed; release is safe.")], model="test-model"
        ),
    )
    assert delivery.report(sid, root)["commands"] == []


def test_checkpoint_uses_public_contract_without_invented_dry_run():
    from claude_agent_sdk import ClaudeSDKClient

    signature = inspect.signature(ClaudeSDKClient.rewind_files)
    assert "user_message_id" in signature.parameters
    assert "dry_run" not in signature.parameters


def test_checkpoint_preview_restore_verification_and_replay_rejected(evidence):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    target = tracked_write(evidence)
    preview = checkpoints.preview(sid, cid, root, "runtime-a")
    assert preview["can_restore"] and preview["sdk_dry_run"] is False
    assert preview["paths"] == [
        {"path": "result.txt", "action": "restore_file", "current_bytes": 5}
    ]
    prepared = checkpoints.prepare_restore(sid, cid, preview["token"], root, "runtime-a")
    target.write_text("before")  # Real SDK I/O is exercised by the offline CLI probe.
    result = checkpoints.verify_restore(root, prepared)
    assert result["verified"] and result["sdk_reported_skips"] is None
    recovery = (
        checkpoints.sess.SESS_DIR / "checkpoint-recovery" / sid / (result["recovery_id"] + ".json")
    )
    assert recovery.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="preview_expired_or_consumed"):
        checkpoints.prepare_restore(sid, cid, preview["token"], root, "runtime-a")
    assert (
        "checkpoint_already_invalidated"
        in checkpoints.preview(sid, cid, root, "runtime-a")["issues"]
    )


@pytest.mark.parametrize("change", ["different", "aba", "symlink", "hardlink", "rename"])
def test_checkpoint_rejects_external_file_changes(evidence, change):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    target = tracked_write(evidence)
    preview = checkpoints.preview(sid, cid, root, "runtime-a")
    if change == "different":
        target.write_text("external edit")
    elif change == "aba":
        target.write_text("other")
        target.write_text("after")
    elif change == "symlink":
        target.unlink()
        target.symlink_to(root / "elsewhere")
    elif change == "hardlink":
        (root / "linked").hardlink_to(target)
    elif change == "rename":
        target.rename(root / "renamed")
    with pytest.raises(ValueError, match="preview_state_changed"):
        checkpoints.prepare_restore(sid, cid, preview["token"], root, "runtime-a")


def test_runtime_aba_and_cross_session_token_are_rejected(evidence):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    tracked_write(evidence)
    preview = checkpoints.preview(sid, cid, root, "runtime-a")
    with pytest.raises(ValueError, match="runtime_changed"):
        checkpoints.prepare_restore(sid, cid, preview["token"], root, "runtime-b")
    preview = checkpoints.preview(sid, cid, root, "runtime-a")
    with pytest.raises(ValueError, match="preview_expired_or_consumed"):
        checkpoints.prepare_restore(str(uuid.uuid4()), cid, preview["token"], root, "runtime-a")


def test_checkpoint_new_file_and_unknown_sdk_skip_are_honest(evidence):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    target = tracked_write(evidence, before=None)
    preview = checkpoints.preview(sid, cid, root, "r")
    assert preview["paths"][0]["action"] == "remove_created_file"
    prepared = checkpoints.prepare_restore(sid, cid, preview["token"], root, "r")
    result = checkpoints.verify_restore(root, prepared)
    assert result["not_restored"] == ["result.txt"] and not result["verified"]
    target.unlink()
    assert checkpoints.verify_restore(root, prepared)["verified"]


def test_checkpoint_refuses_unobserved_and_subagent_writes(evidence):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    checkpoints.observe(
        sid,
        turn,
        root,
        "PostToolUse",
        {"tool_name": "Edit", "tool_input": {"file_path": str(root / "missing")}},
        "tool_unknown",
    )
    assert not checkpoints.preview(sid, cid, root, "r")["can_restore"]


def test_git_baseline_diff_is_honest_about_preexisting_dirty(tmp_path):
    from backend.runtime_identity import inspect_workspace, task_diff

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "source.py"
    target.write_text("original\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    clean = inspect_workspace(tmp_path, refresh=True)
    target.write_text("preexisting\n")
    dirty = inspect_workspace(tmp_path, refresh=True)
    target.write_text("task change\n")
    assert task_diff(tmp_path, clean)["scope"] == "workspace_since_base"
    report = task_diff(tmp_path, dirty)
    assert report["scope"] == "includes_preexisting_changes"
    assert "+task change" in report["patch"]
    assert report["untracked_included"] is False


def test_non_git_workspace_retains_tool_evidence(evidence):
    delivery, _, root, sid, turn = evidence
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(root / "report.md")},
        "tool_response": {},
    }
    (root / "report.md").write_text("report")
    delivery.record_tool(sid, turn, root, "PostToolUse", payload, "write_report")
    result = delivery.report(sid, root)
    assert not result["diff"]["available"]
    assert result["artifacts"][0]["path"] == "report.md"


def test_command_preview_redacts_credentials(evidence):
    delivery, _, root, sid, turn = evidence
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "API_TOKEN=private-value tool --password private-password"},
    }
    delivery.record_tool(sid, turn, root, "PreToolUse", payload, "token_tool")
    command = delivery.report(sid, root)["commands"][0]["command"]
    assert "private-value" not in command and "private-password" not in command


def test_legacy_canonical_evidence_has_no_fabricated_baseline(evidence, monkeypatch):
    delivery, _, root, sid, turn = evidence
    from backend import chat

    canonical = root / "fixture.jsonl"
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    records = [
        {
            "type": "user",
            "uuid": uid,
            "timestamp": "2026-09-06T00:00:00Z",
            "message": {"role": "user", "content": "Check the fixture"},
        },
        {
            "type": "assistant",
            "uuid": aid,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "check",
                        "name": "Bash",
                        "input": {"command": "python -m pytest"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "check",
                        "content": "1 passed",
                        "is_error": False,
                    }
                ],
            },
            "toolUseResult": {"stdout": "1 passed", "stderr": "", "interrupted": False},
        },
    ]
    canonical.write_text("\n".join(json.dumps(r) for r in records))
    monkeypatch.setattr(chat, "_canonical_session_evidence_path", lambda *_args: canonical)
    delivery.save(sid, {"schema": 1, "turns": []})
    result = delivery.report(sid, root)
    assert result["commands"][0]["message_id"] == aid
    assert result["commands"][0]["exit_code"] is None
    assert result["turn"]["status"] == "history_only"
    assert not result["diff"]["available"]
    assert "historical_evidence_without_task_baseline" in result["unverified"]


def test_stream_tokens_do_not_read_delivery_sidecar(evidence, monkeypatch):
    from claude_agent_sdk import StreamEvent

    delivery, _, _, sid, turn = evidence

    def unexpected_read(_sid):
        raise AssertionError("stream tokens must not read task evidence")

    monkeypatch.setattr(delivery, "load", unexpected_read)
    delivery.observe_message(
        sid,
        turn,
        StreamEvent(
            uuid=str(uuid.uuid4()),
            session_id=sid,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "token"}},
        ),
    )


def test_looping_tool_path_retains_failure_evidence(evidence):
    delivery, _, root, sid, turn = evidence
    target = root / 'loop'
    target.symlink_to('loop')
    payload = {'tool_name': 'Write', 'tool_input': {'file_path': str(target)}}
    delivery.record_tool(sid, turn, root, 'PreToolUse', payload, 'loop_write')
    delivery.record_tool(sid, turn, root, 'PostToolUseFailure', payload, 'loop_write')
    record = delivery.load(sid)['turns'][-1]['tools'][-1]
    assert record['id'] == 'loop_write'
    assert record['status'] == 'tool_failed'
    assert target.is_symlink()


def test_looping_tool_path_disables_unsafe_checkpoint_restore(evidence):
    _, checkpoints, root, sid, turn = evidence
    cid = str(uuid.uuid4())
    checkpoints.record_id(sid, turn, cid)
    target = root / 'loop'
    target.symlink_to('loop')
    payload = {'tool_name': 'Write', 'tool_input': {'file_path': str(target)}}
    checkpoints.observe(sid, turn, root, 'PreToolUse', payload, 'loop_write')
    checkpoints.observe(sid, turn, root, 'PostToolUseFailure', payload, 'loop_write')
    preview = checkpoints.preview(sid, cid, root, 'runtime')
    assert not preview['can_restore']
    assert 'unsafe_or_unobserved_path' in preview['issues']
    assert target.is_symlink()
