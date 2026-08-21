"""Claude CLI canonical transcript discovery and raw readers.

This module owns only filesystem/session-store mechanics.  ``backend.chat``
keeps compatibility wrappers so its long-standing monkeypatch surface remains
the execution boundary for callers and tests.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
from typing import Callable, Iterator
import uuid


JSONL_PATH_CACHE: dict[str, Path] = {}
JSONL_PATH_CACHE_MAX = 4096


def cli_project_roots(vendor_config_dir: Path) -> list[Path]:
    """Return existing native and vendor Claude CLI project roots."""
    candidates = [
        Path.home() / ".claude" / "projects",
        vendor_config_dir / "projects",
    ]
    return [root for root in candidates if root.exists()]


def cli_encode_cwd(
    path: str,
    *,
    project_key_for_directory: Callable[[str], str],
) -> str:
    """Encode a workspace path exactly as the Claude SDK/CLI does."""
    return project_key_for_directory(path)


def find_session_jsonl(
    sid: str,
    *,
    project_roots: Callable[[], list[Path]],
    cache: dict[str, Path] = JSONL_PATH_CACHE,
    cache_max: int = JSONL_PATH_CACHE_MAX,
) -> Path | None:
    """Locate and positive-cache one canonical Claude CLI JSONL transcript."""
    cached = cache.get(sid)
    if cached is not None:
        if cached.is_file():
            return cached
        cache.pop(sid, None)
    for projects_root in project_roots():
        for hit in projects_root.glob(f"*/{sid}.jsonl"):
            if hit.is_file():
                if len(cache) >= cache_max:
                    cache.clear()
                cache[sid] = hit
                return hit
    return None


def canonical_session_evidence_path(
    sid: str,
    workspace: Path,
    *,
    find_session_jsonl: Callable[[str], Path | None],
    project_roots: Callable[[], list[Path]],
    encode_cwd: Callable[[str], str],
) -> Path | None:
    """Return a validated canonical transcript path beneath a CLI root."""
    try:
        canonical_sid = str(uuid.UUID(sid))
    except (ValueError, AttributeError, TypeError):
        return None
    if sid != canonical_sid:
        return None
    candidate = find_session_jsonl(sid)
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve(strict=True)
        roots = {root.resolve(strict=True) for root in project_roots()}
    except OSError:
        return None
    if resolved.name != f"{sid}.jsonl":
        return None
    if resolved.parent.name != encode_cwd(str(workspace)):
        return None
    if resolved.parent.parent not in roots:
        return None
    return resolved


def compact_tail_cursor(
    sid: str,
    *,
    find_session_jsonl: Callable[[str], Path | None],
) -> tuple[Path | None, int]:
    """Snapshot the canonical transcript byte boundary before ``/compact``."""
    path = find_session_jsonl(sid)
    if path is None:
        return None, 0
    try:
        return path, path.stat().st_size
    except OSError:
        return path, 0


def compact_tail_outcome(path: Path | None, offset: int) -> dict[str, bool]:
    """Return privacy-safe facts from records appended by one ``/compact``."""
    result = {
        "boundary": False,
        "summary": False,
        "context_error": False,
    }
    if path is None:
        return result
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(offset if 0 <= offset <= size else 0)
            raw = handle.read()
    except OSError:
        return result
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("subtype") == "compact_boundary":
            result["boundary"] = True
        if entry.get("isCompactSummary") is True:
            result["summary"] = True
        if entry.get("subtype") != "local_command":
            continue
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        text = str(entry.get("content") or data.get("content") or "").lower()
        if any(marker in text for marker in (
            "context window", "context length", "input exceeds",
            "maximum context", "prompt too long", "too many tokens",
        )):
            result["context_error"] = True
    return result


@contextmanager
def session_config_dir(
    model: str,
    *,
    lock,
    is_third_party: Callable[[str], bool],
    vendor_config_dir: Callable[[], Path],
) -> Iterator[None]:
    """Serialize and scope the SDK's process-global ``CLAUDE_CONFIG_DIR``."""
    with lock:
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            if model and is_third_party(model):
                os.environ["CLAUDE_CONFIG_DIR"] = str(vendor_config_dir())
            yield
        finally:
            if old is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = old
            else:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)


def get_session_msgs(
    sid: str,
    model: str,
    *,
    config_dir: Callable[[str], object],
    loader: Callable[..., list],
    workspace: Callable[[str], Path],
) -> list:
    """Read SDK messages from the native or vendor-isolated session store."""
    with config_dir(model):
        return loader(sid, directory=str(workspace(sid)))


def transcript_ts_ms(entry: dict) -> int | None:
    """Return epoch milliseconds for a raw transcript ISO-8601 timestamp."""
    raw = entry.get("timestamp") or ""
    if not raw:
        return None
    try:
        return int(dt.datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


class RawMsg:
    """Minimal SDK SessionMessage-compatible view of one raw JSONL record."""

    __slots__ = ("uuid", "type", "message", "mts")

    def __init__(
        self,
        uuid: str,
        type_: str,
        message: dict,
        mts: int | None = None,
    ):
        self.uuid = uuid
        self.type = type_
        self.message = message
        self.mts = mts


def full_session_msgs(
    sid: str,
    *,
    find_session_jsonl: Callable[[str], Path | None],
    raw_msg_type: type[RawMsg] = RawMsg,
    timestamp_ms: Callable[[dict], int | None] = transcript_ts_ms,
) -> list[RawMsg]:
    """Read every canonical user/assistant record in JSONL file order."""
    jsonl_path = find_session_jsonl(sid)
    if jsonl_path is None:
        return []
    out: list[RawMsg] = []
    seen: set[str] = set()
    try:
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("type") not in ("user", "assistant"):
                    continue
                message_uuid = entry.get("uuid")
                if not message_uuid or message_uuid in seen:
                    continue
                seen.add(message_uuid)
                out.append(raw_msg_type(
                    message_uuid,
                    entry.get("type"),
                    entry.get("message") or {},
                    timestamp_ms(entry),
                ))
    except Exception:
        return []
    return out


def read_tail_lines(path: Path, n: int, block: int = 65536) -> list[str]:
    """Return the last ``n`` non-empty lines with O(tail) file I/O."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            read = min(block, pos)
            pos -= read
            handle.seek(pos)
            data = handle.read(read) + data
        lines = data.split(b"\n")
        return [
            line.decode("utf-8", "replace")
            for line in lines[-n:]
            if line.strip()
        ]


def recent_turn_uuids(
    sid: str,
    want_image_user: bool,
    tail_lines: int = 400,
    *,
    find_session_jsonl: Callable[[str], Path | None],
    tail_reader: Callable[[Path, int], list[str]] = read_tail_lines,
) -> tuple[str | None, str | None]:
    """Read the transcript tail for the latest assistant and matching user."""
    path = find_session_jsonl(sid)
    if path is None:
        return None, None
    try:
        lines = tail_reader(path, tail_lines)
    except Exception:
        return None, None
    assistant_uuid: str | None = None
    user_uuid: str | None = None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        entry_type = entry.get("type")
        message_uuid = entry.get("uuid")
        if not message_uuid:
            continue
        if entry_type == "assistant" and assistant_uuid is None:
            assistant_uuid = message_uuid
        elif entry_type == "user" and user_uuid is None:
            if want_image_user:
                content = (entry.get("message") or {}).get("content") or []
                has_image = isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "image"
                    for block in content
                )
                if has_image:
                    user_uuid = message_uuid
            else:
                user_uuid = message_uuid
        if assistant_uuid and user_uuid:
            break
    return assistant_uuid, user_uuid


def jsonl_signature(
    sid: str,
    *,
    find_session_jsonl: Callable[[str], Path | None],
) -> tuple[float, int] | None:
    """Return the canonical transcript's mtime/size freshness signature."""
    path = find_session_jsonl(sid)
    if path is None:
        return None
    try:
        stat = path.stat()
        return stat.st_mtime, stat.st_size
    except OSError:
        return None
