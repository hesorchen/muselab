"""Create a recoverable session fork when native context compaction is stuck.

The source transcript is never modified.  We first ask the Claude Agent SDK to
make its normal, lossless fork (which also remaps the transcript UUID graph),
then atomically append a fresh ``compact_boundary`` and a bounded
``isCompactSummary`` record to *that fork only*.  Normal SDK/CLI resume follows
the new compact root, while the fork's full JSONL remains available for audit
and MuseLab's full-history view.

Only visible user/assistant text is carried into the recovery summary.  Meta,
sidechain, tool, thinking, attachment, command-wrapper and API-error payloads
are deliberately excluded so a huge hidden tool result cannot make the rescue
summary overflow the same context window again.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from claude_agent_sdk import fork_session as sdk_fork_session


DEFAULT_MAX_MESSAGE_CHARS = 12_000
DEFAULT_MAX_TOTAL_CHARS = 96_000
MIN_CONTEXT_DERIVED_CHARS = 16_000

_CONTINUATION_PREFIX = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The conversation is summarized below:\n\n"
)
_SUMMARY_NOTICE = (
    "Recovery summary generated from visible conversation text only. Hidden "
    "metadata, internal reasoning, tool calls/results, attachments, command "
    "wrappers, and transport errors were omitted.\n\n"
)

_COMMAND_WRAPPER_MARKERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<task-notification>",
)
_API_ERROR_RE = re.compile(
    r"^\s*(?:API\s+Error\s*:|Error:\s*(?:400|413)\b|"
    r"Your input exceeds? the context window\b|"
    r"Your input exceeded the context window\b)",
    re.IGNORECASE,
)
_DATA_URI_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/\r\n]+={0,2}",
    re.IGNORECASE,
)
_LONG_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/])")


class _ForkResult(Protocol):
    session_id: str


ForkSession = Callable[..., _ForkResult]


class ContextRecoveryError(RuntimeError):
    """Raised when a safe recovery fork cannot be completed."""


@dataclass(frozen=True)
class RecoverySummaryStats:
    visible_messages: int
    included_messages: int
    omitted_messages: int
    excluded_messages: int
    truncated_messages: int
    excerpt_chars: int
    summary_chars: int
    max_message_chars: int
    max_total_chars: int
    estimated_post_tokens: int


@dataclass(frozen=True)
class RecoveryForkResult:
    session_id: str
    path: Path
    boundary_uuid: str
    summary_uuid: str
    source_bytes: int
    stats: RecoverySummaryStats


@dataclass(frozen=True)
class _SourceSnapshot:
    size: int
    digest: str


@dataclass
class _ExtractionCounters:
    visible: int = 0
    excluded: int = 0
    truncated: int = 0


def _snapshot(path: Path) -> _SourceSnapshot:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return _SourceSnapshot(size=size, digest=digest.hexdigest())


def _context_budget(model_config_context: int | None) -> int:
    """Reserve only a conservative slice of a model's token context.

    Character counts are intentionally treated as a worst-case token proxy.
    This makes CJK and dense/code-heavy sessions safe without depending on a
    model-specific tokenizer in this low-level recovery path.
    """
    if not model_config_context or model_config_context <= 0:
        return DEFAULT_MAX_TOTAL_CHARS
    return min(
        DEFAULT_MAX_TOTAL_CHARS,
        max(MIN_CONTEXT_DERIVED_CHARS, model_config_context // 3),
    )


def _strip_embedded_binary(text: str) -> str:
    text = _DATA_URI_RE.sub("", text)
    return _LONG_BASE64_RE.sub("", text)


def _is_command_wrapper(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(marker) for marker in _COMMAND_WRAPPER_MARKERS)


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        # Explicitly ignore image/document/tool/thinking payloads.  A message
        # may still contain a separate, user-visible text block worth keeping.
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n\n".join(parts)


def _visible_text(entry: object) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    role = entry.get("type")
    if role not in {"user", "assistant"}:
        return None
    if (
        entry.get("isMeta")
        or entry.get("isSidechain")
        or entry.get("teamName")
        or entry.get("isCompactSummary")
        or entry.get("isVisibleInTranscriptOnly")
        or entry.get("isApiErrorMessage")
        or entry.get("error")
    ):
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    message_role = message.get("role")
    if message_role not in {None, role}:
        return None

    text = _strip_embedded_binary(_text_from_content(message.get("content"))).strip()
    if not text or _is_command_wrapper(text) or _API_ERROR_RE.match(text):
        return None
    return role, text


def _render_excerpt(
    source_path: Path,
    *,
    max_message_chars: int,
    max_total_chars: int,
) -> tuple[str, RecoverySummaryStats]:
    if max_message_chars <= 0:
        raise ValueError("max_message_chars must be positive")
    if max_total_chars <= 0:
        raise ValueError("max_total_chars must be positive")

    selected: deque[str] = deque()
    selected_chars = 0
    counters = _ExtractionCounters()

    with source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                counters.excluded += 1
                continue

            visible = _visible_text(entry)
            if visible is None:
                counters.excluded += 1
                continue
            role, text = visible
            counters.visible += 1

            if len(text) > max_message_chars:
                marker = "\n[message truncated]"
                if max_message_chars > len(marker):
                    text = text[: max_message_chars - len(marker)].rstrip() + marker
                else:
                    text = text[:max_message_chars]
                counters.truncated += 1

            label = "User" if role == "user" else "Assistant"
            rendered = f"[{label}]\n{text}\n"
            if len(rendered) > max_total_chars:
                rendered = rendered[:max_total_chars]
                counters.truncated += 1

            selected.append(rendered)
            selected_chars += len(rendered)
            while (
                len(selected) > 1
                and selected_chars + len(selected) - 1 > max_total_chars
            ):
                selected_chars -= len(selected.popleft())
            if selected_chars + len(selected) - 1 > max_total_chars:
                only = selected.pop()
                only = only[-max_total_chars:]
                selected.append(only)
                selected_chars = len(only)

    excerpt = "\n".join(selected)
    included = len(selected)
    omitted = max(0, counters.visible - included)
    summary = _CONTINUATION_PREFIX + _SUMMARY_NOTICE + excerpt
    estimated_post_tokens = max(1, math.ceil(len(summary) / 3))
    stats = RecoverySummaryStats(
        visible_messages=counters.visible,
        included_messages=included,
        omitted_messages=omitted,
        excluded_messages=counters.excluded,
        truncated_messages=counters.truncated,
        excerpt_chars=len(excerpt),
        summary_chars=len(summary),
        max_message_chars=max_message_chars,
        max_total_chars=max_total_chars,
        estimated_post_tokens=estimated_post_tokens,
    )
    return summary, stats


def _last_main_message_uuid(path: Path) -> str | None:
    last: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict) or entry.get("type") not in {"user", "assistant"}:
                continue
            if entry.get("isMeta") or entry.get("isSidechain") or entry.get("teamName"):
                continue
            uid = entry.get("uuid")
            if isinstance(uid, str):
                last = uid
    return last


def _atomic_append_records(path: Path, records: tuple[dict, ...]) -> None:
    """Append JSONL records through same-directory replace, always mode 0600."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.recover.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        last_byte = b""
        with os.fdopen(fd, "wb") as output, path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                last_byte = chunk[-1:]
            if last_byte and last_byte != b"\n":
                output.write(b"\n")
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file replace is already durable on normal local filesystems;
            # some platforms/filesystems do not support fsync on directories.
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


def _safe_remove_failed_fork(path: Path | None, source_path: Path) -> None:
    if path is None:
        return
    try:
        if path.resolve(strict=False) == source_path.resolve(strict=False):
            return
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def create_recovery_fork(
    source_sid: str,
    source_path: str | Path,
    cwd: str | Path,
    title: str | None = None,
    pre_tokens: int = 0,
    model_config_context: int | None = None,
    *,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    max_total_chars: int | None = None,
    fork_session_fn: ForkSession = sdk_fork_session,
) -> RecoveryForkResult:
    """Create a lossless SDK fork whose active chain starts at a safe summary.

    ``CLAUDE_CONFIG_DIR`` selection is intentionally left to the caller, just
    like ``claude_agent_sdk.fork_session`` itself.  Any failure after the SDK
    fork is created removes that incomplete fork; the source transcript is
    checked before and after and is never opened for writing.
    """
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        parsed_source_sid = str(uuid.UUID(source_sid))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid source session id: {source_sid}") from exc
    if source.name != f"{parsed_source_sid}.jsonl":
        raise ValueError("source_path does not match source_sid")

    total_budget = max_total_chars
    if total_budget is None:
        total_budget = _context_budget(model_config_context)

    before = _snapshot(source)
    summary, stats = _render_excerpt(
        source,
        max_message_chars=max_message_chars,
        max_total_chars=total_budget,
    )
    if _snapshot(source) != before:
        raise ContextRecoveryError("source transcript changed while recovery summary was built")

    fork_path: Path | None = None
    try:
        fork_result = fork_session_fn(
            parsed_source_sid,
            directory=str(Path(cwd)),
            title=title,
        )
        try:
            fork_sid = str(uuid.UUID(fork_result.session_id))
        except (ValueError, AttributeError) as exc:
            raise ContextRecoveryError("SDK returned an invalid recovery session id") from exc

        fork_path = source.with_name(f"{fork_sid}.jsonl")
        if fork_path.resolve(strict=False) == source.resolve(strict=False):
            raise ContextRecoveryError("SDK recovery fork resolved to the source transcript")
        try:
            fork_stat = fork_path.lstat()
        except FileNotFoundError as exc:
            raise ContextRecoveryError("SDK recovery fork transcript was not created") from exc
        if not stat.S_ISREG(fork_stat.st_mode):
            raise ContextRecoveryError("SDK recovery fork transcript is not a regular file")

        logical_parent = _last_main_message_uuid(fork_path)
        boundary_uuid = str(uuid.uuid4())
        summary_uuid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        estimated_post_tokens = stats.estimated_post_tokens
        safe_pre_tokens = max(0, int(pre_tokens or 0))

        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": boundary_uuid,
            "parentUuid": None,
            "logicalParentUuid": logical_parent,
            "sessionId": fork_sid,
            "timestamp": now,
            "cwd": str(Path(cwd)),
            "entrypoint": "sdk-py",
            "userType": "external",
            "isMeta": False,
            "isSidechain": False,
            "level": "info",
            "compactMetadata": {
                "trigger": "manual",
                "preTokens": safe_pre_tokens,
                "postTokens": estimated_post_tokens,
                "durationMs": 0,
                "cumulativeDroppedTokens": max(0, safe_pre_tokens - estimated_post_tokens),
            },
        }
        compact_summary = {
            "type": "user",
            "uuid": summary_uuid,
            "parentUuid": boundary_uuid,
            "sessionId": fork_sid,
            "timestamp": now,
            "cwd": str(Path(cwd)),
            "entrypoint": "sdk-py",
            "userType": "external",
            "isSidechain": False,
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "promptId": str(uuid.uuid4()),
            "message": {"role": "user", "content": summary},
        }
        _atomic_append_records(fork_path, (boundary, compact_summary))

        if _snapshot(source) != before:
            raise ContextRecoveryError("source transcript changed while recovery fork was created")
        if stat.S_IMODE(fork_path.stat().st_mode) != 0o600:
            raise ContextRecoveryError("recovery fork transcript permissions are not 0600")

        return RecoveryForkResult(
            session_id=fork_sid,
            path=fork_path,
            boundary_uuid=boundary_uuid,
            summary_uuid=summary_uuid,
            source_bytes=before.size,
            stats=stats,
        )
    except Exception:
        _safe_remove_failed_fork(fork_path, source)
        raise
