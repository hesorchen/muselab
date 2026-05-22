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
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# SDK-native session enumeration. CLI's JSONL is the truth for transcript +
# last-modified + custom_title; muselab index.json is the truth for
# model / system_prompt / auto_named flag and for "pre-first-query" sessions
# (CLI doesn't create the JSONL until the first query, but UI needs to show
# the session immediately after create_session).
from claude_agent_sdk import list_sessions as sdk_list_sessions
from claude_agent_sdk import get_session_info as sdk_get_session_info
from .settings import ROOT, atomic_write_text


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
    atomic_write_text(INDEX, json.dumps(items, ensure_ascii=False, indent=2))
    # Index was just rewritten — invalidate any cached list_sessions() output
    # so the next caller sees the rename / delete / bump immediately rather
    # than waiting for the TTL to expire.
    invalidate_sessions_cache()


# Serialize all index R-M-W. The mutators below (toggle_pin /
# register_session / delete_session / rename_session / update_*
# / bump_session) each do _load_index → mutate → _save_index, and two
# concurrent invocations (e.g. two streams finishing close together
# both calling bump_session) used to silently drop one update — second
# write overwrote the first's bump with its own pre-mutation snapshot.
# threading.Lock works fine because every mutator is called from sync
# code paths (async handlers either run them directly via FastAPI's
# threadpool, or via await asyncio.to_thread-style wrappers); the lock
# is non-reentrant but no mutator calls another while holding it.
_INDEX_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# list_sessions() TTL cache
# ---------------------------------------------------------------------------
# Profile on a 270-session archive showed `list_sessions()` takes 150-480ms,
# dominated by `sdk_list_sessions()` walking every JSONL for metadata. The
# function is called multiple times per request flow:
#   - /api/chat/sessions (UI refresh)
#   - search endpoint (builds id→name map)
#   - compact cross-session lookups
#   - heartbeat reconnect path
#
# Caching with a short TTL deduplicates "refresh storms" (heartbeat reconnect
# triggers refreshSessions + fetchContextInfo + scheduler unread simultaneously)
# without staling user-visible state for more than ~2s. Internal mutations
# (bump_session / rename / delete / pin) call `invalidate_sessions_cache()`
# via `_save_index` so muselab-driven changes appear immediately; only
# external JSONL writes (rare in muselab context) wait for the TTL.

_LIST_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_LIST_CACHE_TTL_S = 2.0


def invalidate_sessions_cache() -> None:
    """Drop the cached list_sessions() snapshot. Call after any mutation that
    changes index.json or adds/removes a session sidecar."""
    _LIST_CACHE["at"] = 0.0
    _LIST_CACHE["data"] = None


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
    atomic_write_text(_sidecar_path(sid), json.dumps(data, ensure_ascii=False))


# ============================================================================
# Session-level CRUD (metadata only — no transcript handling)
# ============================================================================

def _merge_sdk_with_index(info: Any, m: dict) -> dict:
    """Build a muselab-shaped session dict from a SDKSessionInfo + the
    muselab index entry (may be empty for sessions created outside muselab)."""
    name = (info.custom_title
             or m.get("name")
             or title_from_message(info.first_prompt or "")
             or _default_session_name())
    return {
        "id": info.session_id,
        "name": name,
        "model": m.get("model", ""),
        "system_prompt": m.get("system_prompt", ""),
        # Auto-named flag stays True only if neither SDK custom_title nor
        # an explicit muselab rename has happened yet.
        "auto_named": (m.get("auto_named", True)
                        and not info.custom_title),
        # SDK stores ms since epoch — convert to seconds to stay
        # consistent with muselab's pre-existing time.time() style.
        "created_at": (info.created_at / 1000.0
                        if info.created_at
                        else m.get("created_at", 0)),
        "updated_at": (info.last_modified / 1000.0
                        if info.last_modified
                        else m.get("updated_at", 0)),
        # message_count not in SDKSessionInfo (would need a full JSONL
        # scan per session). bump_session writes it to index after each
        # turn, so fall back there. New sessions show 0 until first turn.
        "message_count": m.get("message_count", 0),
        # turn_count = how many user prompts this session has. More intuitive
        # than message_count (which counts every assistant / thinking / tool
        # frame). Falls back to message_count // 2 for legacy entries written
        # before this field existed.
        "turn_count": m.get("turn_count",
                              max(0, m.get("message_count", 0) // 2)),
        "first_prompt": info.first_prompt or "",
        "tag": info.tag or m.get("tag"),
        "pinned": bool(m.get("pinned", False)),
    }


def toggle_pin(sid: str) -> bool:
    """Flip the `pinned` flag on a session in the index. Returns the new state.
    Frontend's session picker sorts pinned sessions to the top."""
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["pinned"] = not bool(s.get("pinned", False))
                _save_index(idx)
                return s["pinned"]
        # Session exists only in CLI JSONL (no muselab index entry yet) — create
        # a minimal entry to hold the pin flag.
        now = time.time()
        idx.append({
            "id": sid, "name": "", "model": "", "system_prompt": "",
            "created_at": now, "updated_at": now,
            "message_count": 0, "auto_named": True, "pinned": True,
        })
        _save_index(idx)
        return True


def list_sessions() -> list[dict]:
    """List sessions, preferring SDK truth (CLI JSONL last_modified +
    custom_title) and falling back to muselab index for muselab-specific
    fields and pre-first-query sessions.

    Cached for `_LIST_CACHE_TTL_S` seconds — see cache block in this module.
    Mutations call `invalidate_sessions_cache()` so cache staleness only
    affects external-to-muselab JSONL writes."""
    now = time.time()
    cached = _LIST_CACHE.get("data")
    if cached is not None and (now - _LIST_CACHE["at"]) < _LIST_CACHE_TTL_S:
        # Return a shallow copy of the list so callers that mutate-in-place
        # (e.g. add a transient field for rendering) don't poison the cache.
        # Inner dicts are still shared — read-only callers won't notice.
        return list(cached)
    index = _load_index()
    index_by_id = {s["id"]: s for s in index}
    sdk_list: list[Any] = []
    if ROOT is not None:
        try:
            sdk_list = sdk_list_sessions(directory=str(ROOT))
        except Exception as e:
            sys.stderr.write(
                f"[sessions] sdk_list_sessions failed, "
                f"falling back to index.json only: "
                f"{type(e).__name__}: {e}\n")
    out: list[dict] = []
    seen: set[str] = set()
    for info in sdk_list:
        m = index_by_id.get(info.session_id, {})
        out.append(_merge_sdk_with_index(info, m))
        seen.add(info.session_id)
    # Append muselab-only entries (no JSONL on disk yet — usually because
    # the user just created the session but hasn't sent the first message).
    for s in index:
        if s["id"] not in seen:
            out.append(s)
    # Sort: pinned sessions first (descending), then by updated_at desc.
    result = sorted(
        out,
        key=lambda s: (1 if s.get("pinned") else 0, s.get("updated_at", 0)),
        reverse=True,
    )
    _LIST_CACHE["data"] = result
    _LIST_CACHE["at"] = now
    # Return a shallow copy so caller mutations don't bleed back into cache.
    return list(result)


def create_session(name: str = "", model: str = "", system_prompt: str = "") -> dict:
    return register_session(str(uuid.uuid4()), name=name, model=model,
                            system_prompt=system_prompt, auto_named=True)


def register_session(sid: str, *, name: str = "", model: str = "",
                     system_prompt: str = "", auto_named: bool = True,
                     message_count: int = 0) -> dict:
    """Add a session that already has a UUID (e.g. one minted by SDK
    fork_session) to the muselab index. Same shape as create_session
    but without generating a fresh UUID."""
    now = time.time()
    meta = {
        "id": sid,
        "name": name or _default_session_name(),
        "model": model,
        "system_prompt": system_prompt,
        "created_at": now,
        "updated_at": now,
        "message_count": message_count,
        "auto_named": auto_named,
    }
    with _INDEX_LOCK:
        idx = _load_index()
        idx.append(meta)
        _save_index(idx)
    _save_sidecar(sid, {"messages": {}})
    return meta


def get_session_meta(sid: str) -> dict | None:
    """Returns just the session-level metadata. For full session view (with
    transcript), use chat.py's combined read path that pulls from SDK.

    Merges SDK truth (custom_title, last_modified, created_at, tag) with
    muselab index (model, system_prompt, auto_named). Falls back to
    index-only if SDK can't see the session (e.g. CLI hasn't created
    the JSONL yet) or if SDK is unavailable."""
    idx = _load_index()
    m = next((s for s in idx if s["id"] == sid), None)
    info = None
    if ROOT is not None:
        try:
            info = sdk_get_session_info(sid, directory=str(ROOT))
        except Exception as e:
            sys.stderr.write(
                f"[sessions] sdk_get_session_info({sid}) failed: "
                f"{type(e).__name__}: {e}\n")
    if info is not None:
        return _merge_sdk_with_index(info, m or {})
    return m


# Back-compat alias — some code calls get_session() expecting metadata.
get_session = get_session_meta


def delete_session(sid: str) -> bool:
    """Removes muselab's sidecar + index entry. Caller is responsible for
    also calling SDK delete_session() to remove the CLI JSONL."""
    with _INDEX_LOCK:
        idx = _load_index()
        new = [s for s in idx if s["id"] != sid]
        if len(new) == len(idx):
            return False
        _save_index(new)
    p = _sidecar_path(sid)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return True


def rename_session(sid: str, name: str) -> bool:
    with _INDEX_LOCK:
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
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["model"] = model
                _save_index(idx)
                return


# effort is one of: "" (auto/SDK default) | "low" | "medium" | "high" | "xhigh" | "max"
# Empty string means "let the SDK pick" — same as no override. Stored on the
# session so picking a deep-research effort on one tab doesn't leak into others.
def update_effort(sid: str, effort: str) -> None:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["effort"] = effort
                _save_index(idx)
                return


def update_system_prompt(sid: str, system_prompt: str) -> bool:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["system_prompt"] = system_prompt
                s["updated_at"] = time.time()
                _save_index(idx)
                return True
        return False


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
                  turn_count: int | None = None,
                  auto_rename_from: str | None = None) -> None:
    """Update updated_at and optionally message_count / turn_count;
    opportunistically auto-rename from the first substantive user message
    text. Auto-rename also writes an `ai-title` entry to the CLI JSONL so
    that `claude --resume` picker shows muselab-created sessions (without
    the ai-title entry, cc's picker silently skips them — only
    `--resume <sid>` explicit resume works)."""
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["updated_at"] = time.time()
                if message_count is not None:
                    s["message_count"] = message_count
                if turn_count is not None:
                    s["turn_count"] = turn_count
                is_auto = s.get("auto_named",
                                s.get("name", "").startswith("新会话"))
                if is_auto and auto_rename_from:
                    title = title_from_message(auto_rename_from)
                    if title:
                        s["name"] = title
                        s["auto_named"] = False
                        # Propagate to CLI JSONL so cc's `/resume` picker sees us.
                        # rename_session writes a {"type":"ai-title", "aiTitle":...}
                        # entry — this is what the picker filters on. Silent on
                        # FileNotFound (JSONL not created yet) / ValueError.
                        try:
                            from claude_agent_sdk import rename_session as _sdk_rn
                            from .settings import ROOT as _ROOT
                            if _ROOT is not None:
                                _sdk_rn(sid, title, directory=str(_ROOT))
                        except (FileNotFoundError, ValueError):
                            pass
                        except Exception as _e:
                            sys.stderr.write(
                                f"[sessions] auto-rename ai-title write "
                                f"failed for {sid}: {type(_e).__name__}: {_e}\n")
                _save_index(idx)
                return
