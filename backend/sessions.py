"""Session metadata sidecar — paired with CLI's JSONL transcripts.

ARCHITECTURE
============
CLI is the source of truth for the conversation transcript. It writes a
JSONL file at ``~/.claude/projects/<cwd-key>/<sid>.jsonl`` every time the
SDK is invoked with ``resume=<sid>``. That file holds:
  - user / assistant messages (including tool_use + tool_result blocks)
  - compact_boundary + isCompactSummary entries when /compact has run
  - tool sidechains for subagents

muselab keeps a small sidecar of metadata the CLI doesn't track:
  - session-level: name, model, custom system_prompt, auto_named flag,
    created_at/updated_at
  - per-message annotations keyed by message UUID:
      cost (per-turn USD), model (badge), images (uploaded base64),
      docs (uploaded base64), and any custom UI markers

READ PATH:  chat.py merges SDK get_session_messages() with sidecar
            annotations for display.
WRITE PATH: CLI handles transcript via SDK; sessions.py only writes the
            sidecar. After every stream, chat.py calls bump_session() with
            the new message count + annotations for the new assistant turn.

Replaces the pre-2026-05-17 design where muselab stored the full transcript
in sessions/{sid}.json — double-write with CLI's JSONL caused compact_boundary
to be invisible in the UI after native /compact ran.
"""
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def _default_session_name() -> str:
    return "新会话 " + datetime.now().strftime("%m-%d %H:%M")


_FILLER_RE = re.compile(
    r"^(hi+|hello+|hey+|你好+|您好+|嗨+|早+|哈喽+|在吗+|嗯+|ok+|okay+|"
    r"test+|测试+|/\w+)\W*$",
    re.IGNORECASE,
)


def title_from_message(text: str, limit: int = 24) -> str:
    """First-line snippet of the user's first message, trimmed for the dropdown.
    Returns '' for greetings / fillers so the caller can wait for a real one."""
    if not text:
        return ""
    cleaned = re.sub(r"@\S+\s*", "", text).strip()
    if not cleaned or _FILLER_RE.match(cleaned):
        return ""
    first_line = cleaned.splitlines()[0] if cleaned else ""
    first_line = first_line.strip()
    if len(first_line) > limit:
        first_line = first_line[: limit - 1].rstrip() + "…"
    return first_line


SESS_DIR = Path(__file__).resolve().parent.parent / "sessions"
SESS_DIR.mkdir(exist_ok=True)
INDEX = SESS_DIR / "index.json"


def _sidecar_path(sid: str) -> Path:
    return SESS_DIR / f"{sid}.sidecar.json"


def _load_index() -> list[dict]:
    if not INDEX.exists():
        return []
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_index(items: list[dict]) -> None:
    INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_sidecar(sid: str) -> dict:
    p = _sidecar_path(sid)
    if not p.exists():
        return {"messages": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("messages", {})
        return d
    except Exception:
        return {"messages": {}}


def _save_sidecar(sid: str, data: dict) -> None:
    _sidecar_path(sid).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ============================================================================
# Session-level CRUD (metadata only — no transcript handling)
# ============================================================================

def list_sessions() -> list[dict]:
    return sorted(_load_index(), key=lambda s: s.get("updated_at", 0), reverse=True)


def create_session(name: str = "", model: str = "", system_prompt: str = "") -> dict:
    sid = str(uuid.uuid4())
    now = time.time()
    meta = {
        "id": sid,
        "name": name or _default_session_name(),
        "model": model,
        "system_prompt": system_prompt,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "auto_named": True,
    }
    idx = _load_index()
    idx.append(meta)
    _save_index(idx)
    # Initialize empty sidecar so reads don't have to special-case missing file.
    _save_sidecar(sid, {"messages": {}})
    return meta


def get_session_meta(sid: str) -> dict | None:
    """Returns just the session-level metadata. For full session view (with
    transcript), use chat.py's combined read path that pulls from SDK."""
    idx = _load_index()
    return next((s for s in idx if s["id"] == sid), None)


# Back-compat alias — some code calls get_session() expecting metadata.
get_session = get_session_meta


def delete_session(sid: str) -> bool:
    """Removes muselab's sidecar + index entry. Caller is responsible for
    also calling SDK delete_session() to remove the CLI JSONL."""
    idx = _load_index()
    new = [s for s in idx if s["id"] != sid]
    if len(new) == len(idx):
        return False
    _save_index(new)
    p = _sidecar_path(sid)
    if p.exists():
        try: p.unlink()
        except OSError: pass
    return True


def rename_session(sid: str, name: str) -> bool:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["name"] = name
            s["updated_at"] = time.time()
            s["auto_named"] = False
            _save_index(idx)
            return True
    return False


def update_model(sid: str, model: str) -> None:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["model"] = model
            _save_index(idx)
            return


def update_system_prompt(sid: str, system_prompt: str) -> bool:
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["system_prompt"] = system_prompt
            s["updated_at"] = time.time()
            _save_index(idx)
            return True
    return False


def delete_empty_sessions() -> list[str]:
    """Bulk-delete sessions with message_count == 0."""
    idx = _load_index()
    keep, removed = [], []
    for s in idx:
        if s.get("message_count", 0) == 0:
            removed.append(s["id"])
            p = _sidecar_path(s["id"])
            if p.exists():
                try: p.unlink()
                except OSError: pass
        else:
            keep.append(s)
    if removed:
        _save_index(keep)
    return removed


# ============================================================================
# Per-message annotations (cost, model, images, custom UI markers)
# ============================================================================

def get_message_annotations(sid: str) -> dict[str, dict]:
    """Per-message metadata keyed by message UUID. Empty dict if no sidecar."""
    return _load_sidecar(sid).get("messages", {})


def set_message_annotation(sid: str, msg_uuid: str, **fields: Any) -> None:
    """Update one message's annotations (cost, model, images, etc.).
    Fields with value None are skipped (use update with explicit empty
    if you want to clear). Atomic per-call write."""
    data = _load_sidecar(sid)
    msgs = data.setdefault("messages", {})
    cur = msgs.setdefault(msg_uuid, {})
    for k, v in fields.items():
        if v is None:
            continue
        cur[k] = v
    _save_sidecar(sid, data)


# ============================================================================
# Activity bumping — called after every stream turn
# ============================================================================

def bump_session(sid: str, message_count: int | None = None,
                  auto_rename_from: str | None = None) -> None:
    """Update updated_at and optionally message_count; opportunistically
    auto-rename from the first substantive user message text."""
    idx = _load_index()
    for s in idx:
        if s["id"] == sid:
            s["updated_at"] = time.time()
            if message_count is not None:
                s["message_count"] = message_count
            is_auto = s.get("auto_named",
                            s.get("name", "").startswith("新会话"))
            if is_auto and auto_rename_from:
                title = title_from_message(auto_rename_from)
                if title:
                    s["name"] = title
                    s["auto_named"] = False
            _save_index(idx)
            return
