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

# Same rationale as _INDEX_LOCK, but for the per-session sidecar files
# (annotations + pending attachments). set_message_annotation /
# append_pending_attachments / consume_one_pending_attachments each do
# _load_sidecar → mutate → _save_sidecar; FastAPI runs sync handlers in a
# threadpool, so a turn-done cost-annotation write can interleave with a
# heartbeat GET /sessions/{sid} that runs consume_one_pending — the second
# save would clobber the first's mutation (lost annotation / attachment).
# atomic_write_text only guarantees a single write isn't torn; it can't stop
# a lost update across the read-modify-write. One coarse lock is fine — the
# sidecars are tiny and the critical sections are sub-millisecond.
_SIDECAR_LOCK = threading.Lock()


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
# without staling user-visible state for more than ~0.5s. Internal mutations
# (bump_session / rename / delete / pin) call `invalidate_sessions_cache()`
# via `_save_index` so muselab-driven changes appear immediately; only
# external JSONL writes (rare in muselab context) wait for the TTL.
#
# TTL was 2.0s until 2026-05-28 — multi-device + external CLI use cases
# (running `claude --resume xxx` in a terminal while muselab is open in a
# browser tab) noticed the new turns missing from the list for up to 2s
# after each external write. 0.5s feels live without sacrificing the
# refresh-storm dedup (a typical storm completes in ~50 ms anyway).
#
# 2026-06-07: raised 0.5s → 30s. The 0.5s TTL meant the 10s foreground poll
# ALWAYS missed the cache and paid the full ~400ms cold rebuild every time
# (sdk_list_sessions walks every JSONL for metadata — measured 0.38-0.43s on a
# 330-session archive). Since EVERY muselab-internal mutation (bump_session /
# rename / delete / pin / create) calls invalidate_sessions_cache() via
# _save_index, the cache is already correct for muselab-driven changes — the
# only staleness window is an EXTERNAL `claude --resume` write, which now takes
# up to 30s to surface in the list (acceptable; rare workflow). The live
# `active` streaming dots are computed per-request OUTSIDE the cache (from
# _active_turns in chat.py), so they stay real-time regardless of this TTL.

_LIST_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "gen": 0}
_LIST_CACHE_TTL_S = 30.0
_LIST_CACHE_LOCK = threading.Lock()


# Single-flight flag for the stale-while-revalidate background rebuild.
_LIST_REFRESHING: dict[str, bool] = {"v": False}


def _refresh_list_cache_bg() -> None:
    """Background single-flight rebuild for stale-while-revalidate. Builds a
    fresh snapshot via _build_sessions_list() and installs it unless an
    invalidation happened mid-build (data=None) — in that case the next
    caller must rebuild synchronously to see the post-mutation state, so we
    must not overwrite the invalidation with our possibly-pre-mutation
    snapshot."""
    try:
        result = _build_sessions_list()
        now = time.time()
        with _LIST_CACHE_LOCK:
            if _LIST_CACHE["data"] is not None:
                _LIST_CACHE["data"] = result
                _LIST_CACHE["at"] = now
                _LIST_CACHE["gen"] += 1
    except Exception as e:
        sys.stderr.write(f"[sessions] bg list refresh failed: "
                         f"{type(e).__name__}: {e}\n")
    finally:
        _LIST_REFRESHING["v"] = False


def list_sessions_generation() -> int:
    """Monotonic counter bumped on every fresh list_sessions() rebuild and
    on invalidation. Lets callers cache values derived from the list (e.g.
    the /sessions ETag digest) keyed on this instead of re-hashing the same
    snapshot on every poll."""
    with _LIST_CACHE_LOCK:
        return _LIST_CACHE["gen"]


def invalidate_sessions_cache() -> None:
    """Drop the cached list_sessions() snapshot. Call after any mutation that
    changes index.json or adds/removes a session sidecar."""
    with _LIST_CACHE_LOCK:
        _LIST_CACHE["at"] = 0.0
        _LIST_CACHE["data"] = None
        _LIST_CACHE["gen"] += 1
    _META_CACHE.clear()


# Short-TTL per-sid metadata cache. A single GET /sessions/{sid} request can
# call get_session_meta up to 3 times (meta + cost + ctx paths), and EACH
# call does a full index.json read plus an SDK get_session_info JSONL probe.
# 2s is short enough that externally-visible staleness is negligible (the
# sessions LIST already tolerates 30s), and any muselab-side mutation goes
# through _save_index → invalidate_sessions_cache which clears this too.
_META_CACHE: dict[str, tuple[float, dict]] = {}
_META_CACHE_TTL_S = 2.0
_META_CACHE_MAX = 256


# Parsed-sidecar cache keyed by sid → (mtime, size, dict). Sidecars are
# re-read + json.loads'd on EVERY GET /sessions/{sid} (annotations), every
# ctx-window read, etc., and can reach MBs when they hold base64 thumbs.
# (mtime, size) keying means an external edit (or our own _save_sidecar)
# is picked up on the next read. Cached dicts are returned as-is: callers
# that mutate them do so under _SIDECAR_LOCK and immediately _save_sidecar
# (which drops the cache entry), so mutation never leaks a stale snapshot.
_SIDECAR_CACHE: dict[str, tuple[float, int, dict]] = {}
_SIDECAR_CACHE_MAX = 64


def _load_sidecar(sid: str, *, use_cache: bool = True) -> dict:
    """Read + parse the sidecar JSON.

    ``use_cache=False`` (mutator paths) always returns a FRESH parse:
    read-modify-write callers mutate the returned dict in place before
    _save_sidecar, and handing them the cached object would leak those
    in-flight mutations to concurrent readers (and persist them in the
    cache even if the save never happens)."""
    p = _sidecar_path(sid)
    try:
        st = p.stat()
    except OSError:
        return {"messages": {}}
    sig = (st.st_mtime, st.st_size)
    if use_cache:
        hit = _SIDECAR_CACHE.get(sid)
        if hit is not None and hit[0] == sig[0] and hit[1] == sig[1]:
            return hit[2]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("messages", {})
    except Exception:
        return {"messages": {}}
    if use_cache:
        if len(_SIDECAR_CACHE) >= _SIDECAR_CACHE_MAX and sid not in _SIDECAR_CACHE:
            _SIDECAR_CACHE.pop(next(iter(_SIDECAR_CACHE)), None)
        _SIDECAR_CACHE[sid] = (sig[0], sig[1], d)
    return d


def _save_sidecar(sid: str, data: dict) -> None:
    atomic_write_text(_sidecar_path(sid), json.dumps(data, ensure_ascii=False))
    # Drop rather than refresh: the next _load_sidecar re-stats and caches
    # the just-written file, keeping cache state derived purely from disk.
    _SIDECAR_CACHE.pop(sid, None)


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
        # muselab-local knobs the SDK doesn't know about. MUST be merged in
        # here or they vanish the moment a JSONL exists on disk (the SDK path
        # wins over the raw index entry), silently reverting the per-session
        # override to defaults. effort: "" = SDK adaptive. thinking: extended
        # thinking on/off — default True so existing sessions keep reasoning.
        "effort": m.get("effort", ""),
        "thinking": bool(m.get("thinking", True)),
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


def set_pin(sid: str, val: bool) -> bool:
    """Set the `pinned` flag on a session to a specific value. The entire
    load-mutate-save sequence runs under _INDEX_LOCK to prevent races.
    Returns the new state (== val). If no index entry exists yet, a
    minimal stub is created so the flag survives the first bump_session."""
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["pinned"] = bool(val)
                _save_index(idx)
                return bool(val)
        # No muselab index entry yet — create a minimal stub.
        now = time.time()
        idx.append({
            "id": sid, "name": "", "model": "", "system_prompt": "",
            "created_at": now, "updated_at": now,
            "message_count": 0, "auto_named": True, "pinned": bool(val),
        })
        _save_index(idx)
        return bool(val)


def list_sessions() -> list[dict]:
    """List sessions, preferring SDK truth (CLI JSONL last_modified +
    custom_title) and falling back to muselab index for muselab-specific
    fields and pre-first-query sessions.

    Cached for `_LIST_CACHE_TTL_S` seconds — see cache block in this module.
    Mutations call `invalidate_sessions_cache()` so cache staleness only
    affects external-to-muselab JSONL writes.

    Stale-while-revalidate: when the TTL has expired but a snapshot still
    exists, return the stale snapshot immediately and rebuild in a single
    background thread (single-flight via _LIST_REFRESHING). The caller
    never blocks on the ~400ms sdk_list_sessions walk; the refreshed data
    lands for the NEXT call. invalidate_sessions_cache() drops the data
    outright, so muselab-driven mutations still rebuild synchronously on
    the next call (immediate consistency preserved)."""
    now = time.time()
    with _LIST_CACHE_LOCK:
        cached = _LIST_CACHE.get("data")
        if cached is not None:
            fresh = (now - _LIST_CACHE["at"]) < _LIST_CACHE_TTL_S
            if not fresh and not _LIST_REFRESHING["v"]:
                _LIST_REFRESHING["v"] = True
                threading.Thread(
                    target=_refresh_list_cache_bg, daemon=True,
                ).start()
            # Return a shallow copy of the list so callers that mutate-in-place
            # (e.g. add a transient field for rendering) don't poison the cache.
            # Inner dicts are still shared — read-only callers won't notice.
            return list(cached)
    result = _build_sessions_list()
    with _LIST_CACHE_LOCK:
        _LIST_CACHE["data"] = result
        _LIST_CACHE["at"] = now
        _LIST_CACHE["gen"] += 1
    # Return a shallow copy so caller mutations don't bleed back into cache.
    return list(result)


def _build_sessions_list() -> list[dict]:
    """The uncached list build: SDK walk + index merge + sort. Called by
    list_sessions() (sync, cache miss) and _refresh_list_cache_bg() (async,
    stale-while-revalidate)."""
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
    return sorted(
        out,
        key=lambda s: (1 if s.get("pinned") else 0, s.get("updated_at", 0)),
        reverse=True,
    )


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
        # Idempotent: if this id is already registered (client retry / keepalive
        # resend of an optimistic-create POST, or a fork that re-registers),
        # return the existing row instead of appending a duplicate. Duplicate
        # ids would break list dedupe and x-for :key bindings on the frontend.
        existing = next((s for s in idx if s.get("id") == sid), None)
        if existing is not None:
            return existing
        idx.append(meta)
        _save_index(idx)
    try:
        _save_sidecar(sid, {"messages": {}})
    except Exception as e:
        sys.stderr.write(f"[sessions] warning: sidecar write failed for {sid}: {e}\n")
    return meta


def get_session_meta(sid: str) -> dict | None:
    """Returns just the session-level metadata. For full session view (with
    transcript), use chat.py's combined read path that pulls from SDK.

    Merges SDK truth (custom_title, last_modified, created_at, tag) with
    muselab index (model, system_prompt, auto_named). Falls back to
    index-only if SDK can't see the session (e.g. CLI hasn't created
    the JSONL yet) or if SDK is unavailable.

    Cached per-sid for _META_CACHE_TTL_S; "not found" (None) is never
    cached so a just-created session is visible immediately."""
    now = time.time()
    hit = _META_CACHE.get(sid)
    if hit is not None and (now - hit[0]) < _META_CACHE_TTL_S:
        return hit[1]
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
    meta = _merge_sdk_with_index(info, m or {}) if info is not None else m
    if meta is not None:
        if len(_META_CACHE) >= _META_CACHE_MAX and sid not in _META_CACHE:
            _META_CACHE.pop(next(iter(_META_CACHE)), None)
        _META_CACHE[sid] = (now, meta)
    return meta


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
    q = _queue_path(sid)
    if q.exists():
        try:
            q.unlink()
        except OSError:
            pass
    return True


def prune_empty_sessions(keep_ids: tuple | list = ()) -> list[str]:
    """Delete all sessions with message_count == 0 that are not pinned.
    `keep_ids` — session IDs to skip regardless (e.g. the one just created).
    Returns the list of deleted session IDs. Safe to call concurrently;
    the index is patched under _INDEX_LOCK in one shot.

    Disabled by default since 2026-05-24 — the magic disappearance of
    sessions the user hadn't explicitly deleted was surprising and made
    "did I lose work?" anxiety more common than "thanks for cleaning up".
    Opt in by exporting MUSELAB_PRUNE_EMPTY_SESSIONS=true if you want
    the old behaviour back (still subject to all the same safety gates:
    only sessions < 2h old, never-renamed, no pins, no messages).
    """
    import os as _os
    if _os.environ.get("MUSELAB_PRUNE_EMPTY_SESSIONS", "false").lower() != "true":
        return []
    import time as _time
    from claude_agent_sdk import delete_session as sdk_delete_session
    keep = set(keep_ids)
    cutoff = _time.time() - 2 * 3600  # 2 小时
    with _INDEX_LOCK:
        idx = _load_index()
        to_delete = [
            s["id"] for s in idx
            if s.get("message_count", 0) == 0
            and not s.get("pinned")
            and s.get("auto_named", True)
            and s.get("created_at", 0) > cutoff  # 只删 2 小时内的空会话
            and s["id"] not in keep
        ]
        if not to_delete:
            return []
        to_delete_set = set(to_delete)
        _save_index([s for s in idx if s["id"] not in to_delete_set])
    # Outside the lock: remove sidecar files + SDK JSOBLs (best-effort).
    for sid in to_delete:
        p = _sidecar_path(sid)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        q = _queue_path(sid)
        if q.exists():
            try:
                q.unlink()
            except OSError:
                pass
        if ROOT is not None:
            try:
                sdk_delete_session(sid, directory=str(ROOT))
            except Exception:
                pass  # JSONL may not exist yet — that's fine
    if to_delete:
        invalidate_sessions_cache()
    return to_delete


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
# The non-empty values mirror the SDK's EffortLevel literal; the authoritative
# gate lives in chat.py (_VALID_EFFORT = get_args(EffortLevel)). This comment is
# documentation only — keep it in sync if the SDK adds a tier.
def update_effort(sid: str, effort: str) -> None:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["effort"] = effort
                _save_index(idx)
                return


# thinking is a bool: True = extended thinking enabled (default), False =
# disabled for this session. Stored per-session so toggling it on one tab
# doesn't affect others. Disabling is the escape hatch for the CLI
# streaming-interleaving 400 ("thinking blocks ... cannot be modified") —
# a thinking-free session can't produce the interleaved blocks that trip it.
def update_thinking(sid: str, enabled: bool) -> None:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["thinking"] = bool(enabled)
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


def has_pending_attachments(sid: str) -> bool:
    """True when the sidecar holds unbound pending image/doc attachments.
    Cheap (cached sidecar read); lets read paths skip work that only
    matters while a binding is outstanding."""
    return bool(_load_sidecar(sid).get("pending_attachments"))


def sidecar_signature(sid: str) -> tuple[float, int] | None:
    """(mtime, size) of the sidecar file, or None when it doesn't exist.
    Cheap freshness probe for callers that cache anything derived from
    sidecar content (annotations / pending attachments)."""
    try:
        st = _sidecar_path(sid).stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def set_message_annotation(sid: str, msg_uuid: str, **fields: Any) -> None:
    """Update one message's annotations (cost, model, images, etc.).
    Fields with value None are skipped (use update with explicit empty
    if you want to clear). Atomic per-call write."""
    with _SIDECAR_LOCK:
        data = _load_sidecar(sid, use_cache=False)
        msgs = data.setdefault("messages", {})
        cur = msgs.setdefault(msg_uuid, {})
        for k, v in fields.items():
            if v is None:
                continue
            cur[k] = v
        _save_sidecar(sid, data)


def get_session_ctx_window(sid: str) -> int | None:
    """SDK-authoritative context window (maxTokens) last measured for this
    session via ClaudeSDKClient.get_context_usage(), persisted in the sidecar.

    Why this exists: the live `maxTokens` is only known while a turn streams
    and was kept in-memory only — lost on every muselab restart. After a
    restart the context meter fell back to the hardcoded MODEL_CONTEXT_LIMITS
    guess (e.g. 1M) which mismatched the CLI's real 200K window, making the
    ring read ~5x too low. Persisting the measured value lets the meter show
    the correct denominator immediately, no live client needed.

    Returns None when never measured so the caller falls back to the table."""
    v = _load_sidecar(sid).get("context_max_tokens")
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        return None
    return v or None


def set_session_ctx_window(sid: str, max_tokens: int) -> None:
    """Persist the SDK-measured context window for this session. No-op for
    non-positive values (never clobber a good value with 0) and when unchanged
    (avoids a sidecar rewrite on every turn)."""
    if not max_tokens or max_tokens <= 0:
        return
    with _SIDECAR_LOCK:
        data = _load_sidecar(sid, use_cache=False)
        if int(data.get("context_max_tokens") or 0) == int(max_tokens):
            return
        data["context_max_tokens"] = int(max_tokens)
        _save_sidecar(sid, data)


# Hard cap on pending_attachments to prevent unbounded sidecar growth.
# Without this, "upload image → cancel/refresh before send" silently
# accretes entries forever (consume only fires when a real user message
# matches). 50 is far more than any reasonable in-flight burst — a
# single message typically queues 1-3 attachments.
_PENDING_ATTACH_CAP = 50
# Entries older than this are pruned on every append. Counterpart to
# the cap: if the user uploads infrequently, the cap may not trigger
# but stale entries from weeks-old crashed sessions still go away.
_PENDING_ATTACH_TTL_MS = 24 * 60 * 60 * 1000   # 24 hours


def append_pending_attachments(sid: str, images: list[dict] | None = None,
                                docs: list[dict] | None = None) -> None:
    """Stash image/doc attachments before we know the user-message UUID.

    The SDK writes the user-message JSONL record asynchronously, so at
    image-upload time we don't yet have a uuid to set_message_annotation
    on. Previously we waited until stream-completion to find the matching
    user uuid and write the annotation then — but if the stream gets
    cancelled / errored / the user reloads, that write never happens and
    the attachment metadata (thumb + url) is lost.

    Pending entries are bound to user uuids by consume_one_pending_attachments
    when GET /sessions/{sid} encounters a user message with inline image
    refs but no annotation. FIFO match.

    Garbage collection: every append also drops entries older than
    _PENDING_ATTACH_TTL_MS, then truncates to _PENDING_ATTACH_CAP. Without
    this, "upload then cancel" silently bloats the sidecar JSON across
    months of usage."""
    if not images and not docs:
        return
    now_ms = int(__import__("time").time() * 1000)
    with _SIDECAR_LOCK:
        data = _load_sidecar(sid, use_cache=False)
        pend = data.setdefault("pending_attachments", [])
        # GC stale entries first (age them out by ts).
        cutoff = now_ms - _PENDING_ATTACH_TTL_MS
        if pend and any((p.get("ts") or 0) < cutoff for p in pend):
            pend = [p for p in pend if (p.get("ts") or 0) >= cutoff]
            data["pending_attachments"] = pend
        pend.append({
            "ts": now_ms,
            "images": images or [],
            "docs": docs or [],
        })
        # Hard cap — drop oldest (FIFO) so the freshest are kept for the
        # next consume call.
        if len(pend) > _PENDING_ATTACH_CAP:
            del pend[: len(pend) - _PENDING_ATTACH_CAP]
        _save_sidecar(sid, data)


def consume_one_pending_attachments(sid: str, msg_uuid: str) -> dict | None:
    """Pop the oldest pending bundle and bind it to `msg_uuid` as a
    normal annotation. Returns the bundle (or None if no pending /
    already bound). Idempotent."""
    with _SIDECAR_LOCK:
        data = _load_sidecar(sid, use_cache=False)
        msgs = data.setdefault("messages", {})
        cur = msgs.setdefault(msg_uuid, {})
        if cur.get("images") or cur.get("docs"):
            return None  # already bound elsewhere
        pend = data.get("pending_attachments") or []
        if not pend:
            return None
        first = pend[0]
        images = first.get("images") or []
        docs = first.get("docs") or []
        if images:
            cur["images"] = images
        if docs:
            cur["docs"] = docs
        data["pending_attachments"] = pend[1:]
        _save_sidecar(sid, data)
        return first


# ============================================================================
# Activity bumping — called after every stream turn
# ============================================================================

def bump_session(sid: str, message_count: int | None = None,
                  turn_count: int | None = None,
                  auto_rename_from: str | None = None) -> None:
    """Update updated_at and optionally message_count / turn_count;
    opportunistically write a local fallback `name` from the first
    substantive user message text.

    We deliberately do NOT call SDK rename_session here. CC CLI auto-
    generates a real `aiTitle` (Haiku-summarized, often higher quality
    than a first-line snippet) and writes it to the JSONL after each
    turn. SDK rename_session would write `customTitle`, which beats
    aiTitle in the merge — preempting CLI's AI summary forever. Instead
    we just stash a local snippet in the muselab index; the merge in
    `_merge_sdk_with_index` falls back to it via:
        info.custom_title (= customTitle OR aiTitle from CLI)
        or m.get("name")      ← us, the fallback
        or first-line snippet
    so the CLI-generated aiTitle naturally takes over once CLI writes it.

    Side effect of this change: `claude --resume` picker may briefly skip
    muselab-created sessions that haven't yet had CLI write an ai-title
    entry (picker filters on ai-title). The gap closes as soon as CLI
    runs aiTitle generation on the next turn — empty / first-turn-only
    sessions in the picker is the tradeoff for getting real AI summaries.
    """
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
                _save_index(idx)
                return


# ============================================================================
# Per-session message queue (server-side — drives autonomous draining)
# ============================================================================
# Stored in its OWN file (`{sid}.queue.json`), not the annotations sidecar,
# because the annotations sidecar is rewritten on every turn-done and every
# pending-attachment consume; mixing the queue in would widen the lost-update
# window between a queue mutation and an annotation write. A dedicated file +
# lock keeps the two independent.
#
# Shape: {"items": [{"id","text","image_ids","enqueued_at"}], "paused": bool}
#   - items: FIFO; head is sent next by the drain trigger in chat.py
#   - paused: set True when a queued turn errors / hits ask_user_question /
#     is user-cancelled; auto-drain stops until the user resumes
#
# Attachment caveat: image_ids reference the in-memory _image_store in chat.py
# which expires entries after 10 min. A long-queued item's attachments may be
# gone by drain time — _start_turn silently skips expired ids and sends the
# text alone. (Mirrors the frontend's prior choice not to persist attachment
# blobs in the queue.)
_QUEUE_LOCK = threading.Lock()
_QUEUE_MAX = 10   # mirror the frontend cap


def _queue_path(sid: str) -> Path:
    return SESS_DIR / f"{sid}.queue.json"


def _load_queue(sid: str) -> dict:
    p = _queue_path(sid)
    if not p.exists():
        return {"items": [], "paused": False}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("items", [])
        d.setdefault("paused", False)
        if not isinstance(d["items"], list):
            d["items"] = []
        return d
    except Exception:
        return {"items": [], "paused": False}


def _save_queue(sid: str, data: dict) -> None:
    # An empty, un-paused queue leaves no file behind (avoids littering
    # sessions/ with thousands of empty queue.json files over time).
    if not data.get("items") and not data.get("paused"):
        p = _queue_path(sid)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        return
    atomic_write_text(_queue_path(sid), json.dumps(data, ensure_ascii=False))


def get_queue(sid: str) -> dict:
    """Return the current queue snapshot: {'items': [...], 'paused': bool}."""
    with _QUEUE_LOCK:
        return _load_queue(sid)


def enqueue_message(sid: str, text: str, image_ids: str = "",
                    permission: str = "") -> dict:
    """Append a message to the session's queue. Returns
    {'ok': bool, 'item'?: dict, 'queue': dict, 'error'?: str}. Rejects past
    _QUEUE_MAX (mirrors frontend cap).

    `permission` snapshots the sender's permission mode at enqueue time so
    the headless drain starts the turn under the SAME mode the user had
    selected — without it, drained turns silently fell back to the server
    default (bypassPermissions), skipping tool approval the user expected."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        if len(data["items"]) >= _QUEUE_MAX:
            return {"ok": False, "error": "queue_full", "queue": data}
        item = {
            "id": "q-" + uuid.uuid4().hex[:8],
            "text": text or "",
            "image_ids": image_ids or "",
            "permission": permission or "",
            "enqueued_at": int(time.time() * 1000),
        }
        data["items"].append(item)
        _save_queue(sid, data)
        return {"ok": True, "item": item, "queue": data}


def dequeue_message(sid: str) -> dict | None:
    """Pop + return the head item (FIFO) IF the queue is non-empty and not
    paused; else None. Called by the drain trigger after a turn completes."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        if data.get("paused") or not data["items"]:
            return None
        item = data["items"].pop(0)
        _save_queue(sid, data)
        return item


def requeue_head(sid: str, item: dict) -> dict:
    """Re-insert a previously-dequeued item at the HEAD of the queue (FIFO
    restore). Used by the drain trigger when it loses the _active_turns race
    or fails to start the turn — so the item isn't silently dropped. Bypasses
    the _QUEUE_MAX cap (it's restoring an item that was already accepted)."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        data["items"].insert(0, item)
        _save_queue(sid, data)
        return data


def remove_queue_item(sid: str, item_id: str) -> dict:
    """Remove one item by id. Returns the updated queue snapshot."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        data["items"] = [it for it in data["items"] if it.get("id") != item_id]
        _save_queue(sid, data)
        return data


def clear_queue(sid: str) -> None:
    """Drop all items + clear the paused flag (removes the file)."""
    with _QUEUE_LOCK:
        _save_queue(sid, {"items": [], "paused": False})


def set_queue_paused(sid: str, paused: bool) -> dict:
    """Set the paused flag. Returns the updated queue snapshot. Resuming
    (paused=False) does NOT itself drain — the caller kicks the drain."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        data["paused"] = bool(paused)
        _save_queue(sid, data)
        return data


def reorder_queue(sid: str, order: list[str]) -> dict:
    """Reorder items to match `order` (list of item ids). Ids not present in
    `order` are appended in their existing relative order (defensive)."""
    with _QUEUE_LOCK:
        data = _load_queue(sid)
        by_id = {it["id"]: it for it in data["items"]}
        new = [by_id[i] for i in order if i in by_id]
        for it in data["items"]:
            if it["id"] not in order:
                new.append(it)
        data["items"] = new
        _save_queue(sid, data)
        return data
