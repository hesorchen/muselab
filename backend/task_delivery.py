"""Durable task evidence, derived only from tool and SDK lifecycle records.

No assistant prose is interpreted as a test result. Bodies and tool stdout are
not duplicated here; evidence links point back to the existing transcript.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import threading
import time
from typing import Any

from . import sessions as sess, sdk_lifecycle
from .private_storage import ensure_private_regular_file, write_private_bytes
from .runtime_identity import inspect_workspace, task_diff

_LOCKS = tuple(threading.RLock() for _ in range(64))
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_TURNS = 30
_MAX_TOOLS = 300


def lock(sid: str):
    return _LOCKS[hash(sid) % len(_LOCKS)]


def path(sid: str) -> Path:
    if not _SAFE.fullmatch(sid):
        raise ValueError("invalid session id")
    return Path(sess.SESS_DIR) / "delivery" / f"{sid}.json"


def load(sid: str) -> dict:
    target = path(sid)
    if not ensure_private_regular_file(target):
        return {"schema": 1, "turns": []}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("invalid delivery data")
    return data


def save(sid: str, data: dict) -> None:
    if sess.session_is_deleting(sid):
        return
    write_private_bytes(path(sid), json.dumps(data, ensure_ascii=False).encode())


def delete_data(sid: str) -> None:
    """Delete only this session's private evidence after its tombstone is set."""
    from . import file_checkpoints

    with lock(sid):
        targets = [path(sid), file_checkpoints._path(sid)]
        recovery_dir = Path(sess.SESS_DIR) / "checkpoint-recovery" / sid
        if recovery_dir.is_dir() and not recovery_dir.is_symlink():
            targets.extend(
                p
                for p in recovery_dir.glob("*.json")
                if re.fullmatch(r"[a-f0-9]{32}\.json", p.name)
            )
        for target in targets:
            try:
                if ensure_private_regular_file(target):
                    target.unlink()
            except OSError:
                pass
        try:
            recovery_dir.rmdir()
        except OSError:
            pass
    with file_checkpoints._PREVIEW_LOCK:
        for token in list(file_checkpoints._PREVIEWS):
            if file_checkpoints._PREVIEWS[token]["sid"] == sid:
                file_checkpoints._PREVIEWS.pop(token)["watch"].close()


def begin(sid: str, turn_id: str, cwd: Path) -> None:
    baseline = inspect_workspace(cwd, refresh=True)
    with lock(sid):
        data = load(sid)
        if any(t["id"] == turn_id for t in data["turns"]):
            return
        data["turns"] = [
            *data["turns"],
            {
                "id": turn_id,
                "started_at": time.time(),
                "status": "running",
                "workspace": str(cwd),
                "baseline": baseline,
                "tools": [],
                "checkpoint_id": None,
            },
        ][-_MAX_TURNS:]
        save(sid, data)


def _safe_command(value: Any) -> str:
    text = str(value or "")[:2000]
    # Quoted HTTP headers contain a scheme and credential in one shell word.
    # Redact them before matching assignments inside the quoted header.
    text = re.sub(
        r"(?i)('\s*authorization\s*:\s*)[^']*(?:'|$)",
        r"\1[redacted]'", text,
    )
    text = re.sub(
        r'(?i)("\s*authorization\s*:\s*)(?:\\[\s\S]|[^"\\])*(?:"|\\?$)',
        r'\1[redacted]"', text,
    )
    # Consume the full shell word, including quotes, escaped spaces and
    # adjacent quoted/unquoted parts. A preview may cut a quoted value short.
    word = r"""(?:[^\s;'"\\]|\\[\s\S]|'[^']*(?:'|$)|"(?:\\[\s\S]|[^"\\])*(?:"|\\?$))+"""
    text = re.sub(
        r"(?i)(\b[A-Z_]*(?:TOKEN|PASSWORD|SECRET|API_KEY)\s*[=:]\s*"
        r"|\bauthorization\s*=\s*)" + word,
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:token|password|api-key|secret)(?:=|\s+))" + word,
        r"\1[redacted]", text,
    )
    text = re.sub(
        r"""(?i)(\bauthorization\s*:\s*)(?:(?:Basic|Bearer)\s+)?[^\s;'"]+""",
        r"\1[redacted]", text,
    )
    text = re.sub(r"(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
    return text


def relative_path(cwd: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    target = Path(value)
    if not target.is_absolute():
        target = cwd / target
    try:
        return str(target.resolve().relative_to(cwd.resolve()))
    except (OSError, ValueError):
        return None


def record_tool(sid: str, turn_id: str, cwd: Path, event: str, payload: dict, tool_id: str) -> None:
    name = str(payload.get("tool_name") or "")[:80]
    if not name or not tool_id or not _SAFE.fullmatch(tool_id):
        return
    inputs = payload.get("tool_input") or {}
    inputs = inputs if isinstance(inputs, dict) else {}
    response = payload.get("tool_response") or {}
    response = response if isinstance(response, dict) else {}
    with lock(sid):
        data = load(sid)
        turn = next((t for t in data["turns"] if t["id"] == turn_id), None)
        if not turn:
            return
        record = next((t for t in turn["tools"] if t["id"] == tool_id), None)
        if record is None:
            if len(turn["tools"]) >= _MAX_TOOLS:
                turn["truncated"] = True
                save(sid, data)
                return
            record = {"id": tool_id, "name": name, "status": "running", "started_at": time.time()}
            turn["tools"].append(record)
        if name == "Bash":
            record["command"] = _safe_command(inputs.get("command"))
        target = relative_path(cwd, inputs.get("file_path") or inputs.get("notebook_path"))
        if target is not None and name in {"Write", "Edit", "NotebookEdit"}:
            record["path"] = target
        if event != "PreToolUse":
            record["status"] = "tool_failed" if event == "PostToolUseFailure" else "tool_completed"
            record["ended_at"] = time.time()
            exit_code = response.get("exit_code", response.get("exitCode"))
            record["exit_code"] = (
                exit_code
                if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                else None
            )
            if response.get("interrupted"):
                record["status"] = "interrupted"
        save(sid, data)


def observe_message(sid: str, turn_id: str, message: Any) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        UserMessage,
        ToolResultBlock,
        ToolUseBlock,
    )

    if not isinstance(message, (AssistantMessage, UserMessage, ResultMessage)):
        return
    with lock(sid):
        data = load(sid)
        turn = next((t for t in data["turns"] if t["id"] == turn_id), None)
        if not turn:
            return
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                record = next((t for t in turn["tools"] if t["id"] == block.id), None)
                if record is None and len(turn["tools"]) < _MAX_TOOLS:
                    record = {
                        "id": block.id,
                        "name": block.name,
                        "status": "running",
                        "started_at": time.time(),
                    }
                    if block.name == "Bash":
                        record["command"] = _safe_command(block.input.get("command"))
                    turn["tools"].append(record)
                if record is not None:
                    target = relative_path(
                        Path(turn["workspace"]),
                        block.input.get("file_path") or block.input.get("notebook_path"),
                    )
                    if target and block.name in {"Write", "Edit", "NotebookEdit"}:
                        record["path"] = target
                    if getattr(message, "parent_tool_use_id", None):
                        record["scope"] = "subagent"
                if record is not None and getattr(message, "uuid", None):
                    record["message_id"] = message.uuid
        elif isinstance(message, UserMessage):
            content = message.content
            has_results = isinstance(content, list) and any(
                isinstance(b, ToolResultBlock) for b in content
            )
            if has_results:
                for block in content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    record = next((t for t in turn["tools"] if t["id"] == block.tool_use_id), None)
                    if record is not None:
                        record["status"] = "tool_failed" if block.is_error else "tool_completed"
                        record["ended_at"] = time.time()
                        result = getattr(message, "tool_use_result", None) or {}
                        if isinstance(result, dict):
                            code = result.get("exit_code", result.get("exitCode"))
                            record["exit_code"] = (
                                code
                                if isinstance(code, int) and not isinstance(code, bool)
                                else None
                            )
            origin = getattr(message, "origin", None) or {}
            origin_kind = (
                origin.get("kind") if isinstance(origin, dict) else getattr(origin, "kind", None)
            )
            if (
                message.uuid
                and not message.parent_tool_use_id
                and not has_results
                and origin_kind in (None, "human")
                and not (
                    isinstance(content, str) and content.lstrip().startswith("<task-notification>")
                )
            ):
                from . import file_checkpoints

                file_checkpoints.record_id(sid, turn_id, message.uuid)
                turn["checkpoint_id"] = message.uuid
                turn["message_id"] = message.uuid
        elif isinstance(message, ResultMessage):
            turn.update(
                status=sdk_lifecycle.terminal_status(
                    getattr(message, "terminal_reason", None),
                    is_error=bool(message.is_error),
                ),
                ended_at=time.time(),
                terminal_reason=str(getattr(message, "terminal_reason", "") or ""),
            )
        else:
            return
        save(sid, data)


def canonical_history(sid: str, cwd: Path) -> list[dict]:
    """Read a bounded canonical tail for older tasks without inventing a base."""
    from datetime import datetime
    from . import chat

    source = chat._canonical_session_evidence_path(sid, cwd)
    if source is None:
        return []
    limit = 2 * 1024 * 1024
    with source.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - limit))
        if size > limit:
            stream.readline()  # discard a possibly partial JSON record
        lines = stream.read(limit).splitlines()
    turns, turn = [], None
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if (
            not isinstance(record, dict)
            or record.get("isSidechain")
            or record.get("parentToolUseID")
        ):
            continue
        message = record.get("message") or {}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        message_id = record.get("uuid")
        if record.get("type") == "user" and message_id and not results and not record.get("isMeta"):
            origin = record.get("origin") or {}
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
            if isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
                continue
            try:
                started = datetime.fromisoformat(
                    str(record.get("timestamp") or "").replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                started = 0
            turn = {
                "id": message_id,
                "message_id": message_id,
                "started_at": started,
                "status": "history_only",
                "workspace": str(cwd),
                "baseline": {},
                "tools": [],
                "source": "canonical_history",
            }
            turns.append(turn)
            continue
        if turn is None:
            continue
        if record.get("type") == "assistant":
            for block in blocks:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "tool_use"
                    or not block.get("id")
                ):
                    continue
                inputs = block.get("input") or {}
                if not isinstance(inputs, dict) or len(turn["tools"]) >= _MAX_TOOLS:
                    turn["truncated"] = True
                    continue
                tool = {
                    "id": block["id"],
                    "name": block.get("name"),
                    "message_id": message_id,
                    "status": "outcome_unrecorded",
                    "exit_code": None,
                }
                if tool["name"] == "Bash":
                    tool["command"] = _safe_command(inputs.get("command"))
                target = relative_path(cwd, inputs.get("file_path") or inputs.get("notebook_path"))
                if target and tool["name"] in {"Write", "Edit", "NotebookEdit"}:
                    tool["path"] = target
                turn["tools"].append(tool)
        for block in results:
            tool = next(
                (t for t in reversed(turn["tools"]) if t["id"] == block.get("tool_use_id")), None
            )
            if tool:
                tool["status"] = "tool_failed" if block.get("is_error") else "tool_completed"
                structured = record.get("toolUseResult") or record.get("tool_use_result") or {}
                if isinstance(structured, dict):
                    code = structured.get("exit_code", structured.get("exitCode"))
                    tool["exit_code"] = (
                        code if isinstance(code, int) and not isinstance(code, bool) else None
                    )
    return turns[-_MAX_TURNS:]


def report(sid: str, cwd: Path, turn_id: str = "") -> dict:
    with lock(sid):
        data = load(sid)
    turns = data["turns"]
    if not turns or (turn_id and not any(t["id"] == turn_id for t in turns)):
        try:
            known = {t.get("message_id") for t in turns}
            turns = [t for t in canonical_history(sid, cwd) if t["message_id"] not in known] + turns
        except (OSError, ValueError):
            pass
    turn = (
        next((t for t in turns if t["id"] == turn_id), None)
        if turn_id
        else (turns[-1] if turns else None)
    )
    result = {
        "turns": [
            {k: t.get(k) for k in ("id", "started_at", "status", "message_id")}
            for t in reversed(turns)
        ],
        "turn": turn,
    }
    if not turn:
        result["unverified"] = ["no_recorded_task_baseline"]
        return result
    result["diff"] = task_diff(cwd, turn.get("baseline", {}))
    result["artifacts"] = [
        dict(t, exists=relative_path(cwd, t["path"]) == t["path"] and (cwd / t["path"]).is_file())
        for t in turn["tools"]
        if "path" in t and t["status"] == "tool_completed"
    ]
    result["commands"] = [t for t in turn["tools"] if t["name"] == "Bash"]
    result["unverified"] = ["test_assertions_not_inferred_from_prose"]
    if turn.get("source") == "canonical_history":
        result["unverified"].append("historical_evidence_without_task_baseline")
    if not result["commands"]:
        result["unverified"].append("no_command_evidence")
    elif any(t.get("exit_code") is None for t in result["commands"]):
        result["unverified"].append("some_commands_have_no_structured_exit_code")
    if turn.get("truncated"):
        result["unverified"].append("tool_evidence_limit_reached")
    return result


def build_hooks(sid: str, cwd: Path):
    """Observe SDK public hooks without changing permissions or tool output."""
    from claude_agent_sdk.types import HookMatcher

    def callback(event: str):
        async def observe(payload, tool_use_id, _context):
            from . import chat

            active = chat._active_turns.get(sid)
            from . import file_checkpoints

            try:
                identity = str(tool_use_id or payload.get("tool_use_id") or "")
                if active is not None and not active.done:
                    await asyncio.to_thread(
                        file_checkpoints.observe, sid, active.turn_id, cwd, event, payload, identity
                    )
                    await asyncio.to_thread(
                        record_tool, sid, active.turn_id, cwd, event, payload, identity
                    )
                elif payload.get("tool_name") in {
                    "Write",
                    "Edit",
                    "NotebookEdit",
                } and not payload.get("agent_id"):
                    await asyncio.to_thread(
                        file_checkpoints.invalidate, sid, "write_without_task_boundary"
                    )
            except (OSError, ValueError, TypeError):
                # Evidence storage must not alter SDK permissions or tool output.
                # A failed file observation still disables every restore.
                try:
                    await asyncio.to_thread(
                        file_checkpoints.invalidate, sid, "observation_storage_unavailable"
                    )
                except (OSError, ValueError, TypeError):
                    pass
            return {}

        return observe

    return {
        event: HookMatcher(hooks=[callback(event)])
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
    }
