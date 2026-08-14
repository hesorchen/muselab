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
  - session-level: name, model, permission, plan_return_permission,
    auto_named flag,
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
import contextlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

# SDK-native session enumeration. CLI's JSONL is the truth for transcript +
# last-modified + custom_title; muselab index.json is the truth for
# model / permission / auto_named flag and for "pre-first-query" sessions
# (CLI doesn't create the JSONL until the first query, but UI needs to show
# the session immediately after create_session).
from claude_agent_sdk import list_sessions as sdk_list_sessions
from claude_agent_sdk import get_session_info as sdk_get_session_info
from claude_agent_sdk.types import PermissionMode
from .settings import ROOT, atomic_write_text
from .workspaces import registry as workspace_registry


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


_DEFAULT_SESS_DIR = Path(__file__).resolve().parent.parent / "sessions"
_configured_sess_dir = os.environ.get("MUSELAB_SESSIONS_DIR", "").strip()
SESS_DIR = (
    Path(_configured_sess_dir).expanduser().resolve()
    if _configured_sess_dir
    else _DEFAULT_SESS_DIR
)
SESS_DIR.mkdir(parents=True, exist_ok=True)
INDEX = SESS_DIR / "index.json"


def ensure_private_session_storage() -> None:
    """Create/repair MuseLab-owned session state with private permissions.

    This is deliberately invoked from application startup instead of module
    import.  Test fixtures replace ``SESS_DIR`` after importing this module;
    an import-time recursive chmod would therefore mutate the real deployment
    while a hermetic test suite was merely being collected.
    """
    SESS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        SESS_DIR.chmod(0o700)

    # Repair files created by older releases under a permissive umask.  Skip
    # symlinks so session storage can never be used to chmod workspace files.
    for private_path in SESS_DIR.rglob("*"):
        with contextlib.suppress(OSError):
            if private_path.is_symlink():
                continue
            if private_path.is_dir():
                private_path.chmod(0o700)
            elif private_path.is_file():
                private_path.chmod(0o600)

# A Plan-mode process still needs a concrete permission mode to return to after
# ExitPlanMode.  Keep that launch contract beside the visible session
# permission because the Claude JSONL transcript does not persist MuseLab's
# per-session selection.  Derive the allowlist from the installed SDK so new
# non-Plan modes (for example "auto") do not require a second hand-maintained
# list here.
_VALID_PLAN_RETURN_PERMISSIONS = (
    frozenset(get_args(PermissionMode)) - {"plan"}
)
_PLAN_RETURN_PERMISSION_FALLBACK = "default"


_STRICT_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _normalize_iso_utc(value: Any) -> str:
    """Return one strict, timezone-aware ISO timestamp in canonical UTC.

    Runtime rollover timestamps cross the index/sidecar boundary and are later
    compared with CLI JSONL timestamps.  Accepting naive datetimes (or the very
    permissive forms ``datetime.fromisoformat`` otherwise tolerates) makes that
    comparison depend on the host timezone.  Reject those inputs and expose one
    stable spelling with an explicit ``Z`` suffix and microsecond precision.
    """
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not _STRICT_ISO_DATETIME_RE.fullmatch(candidate):
            return ""
        try:
            parsed = datetime.fromisoformat(
                candidate[:-1] + "+00:00"
                if candidate.endswith("Z") else candidate
            )
        except ValueError:
            return ""
    else:
        return ""
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ""
    try:
        utc = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return ""
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_runtime_fork_boundary_at(value: Any) -> str:
    """Public normalizer for the timestamp captured at SDK fork creation.

    The chat runtime records this value before registering the successor.  Keep
    the permissive-to-empty behavior here so callers can decide whether an
    absent SDK timestamp should abort a rollover, while ``register_session``
    still rejects an explicitly supplied malformed value.
    """
    return _normalize_iso_utc(value)


def _normalize_plan_return_permission(permission: Any, value: Any) -> str:
    """Return the safe Plan-exit mode for a persisted session or queue item.

    The field is meaningful only while ``permission == "plan"``.  Legacy rows
    have no field at all, and hand-edited/older data may contain an invalid
    value (including ``"plan"`` itself); all of those fail closed to
    ``"default"`` instead of inheriting a global/browser/runtime bypass mode.
    """
    current = permission.strip() if isinstance(permission, str) else ""
    if current != "plan":
        return ""
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate in _VALID_PLAN_RETURN_PERMISSIONS:
        return candidate
    return _PLAN_RETURN_PERMISSION_FALLBACK


def _normalize_session_permission_fields(row: dict) -> dict:
    """Return a copy with all per-session launch invariants explicit."""
    normalized = dict(row)
    normalized["plan_return_permission"] = _normalize_plan_return_permission(
        normalized.get("permission"),
        normalized.get("plan_return_permission"),
    )
    # `""` was the historical spelling of automatic effort. Keep reads
    # backward-compatible but expose one canonical value to API consumers.
    normalized["effort"] = (
        str(normalized.get("effort") or "").strip() or "auto"
    )
    normalized["service_tier"] = (
        "fast" if normalized.get("service_tier") == "fast" else ""
    )
    normalized["activity_hidden"] = bool(
        normalized.get("activity_hidden", False)
    )
    # Runtime profiles are deliberately closed rather than free-form.  The
    # side-question profile narrows the SDK tool surface to public web lookup;
    # an unknown/legacy value must fall back to the ordinary session runtime.
    normalized["runtime_profile"] = (
        "side_question"
        if normalized.get("runtime_profile") == "side_question"
        else ""
    )
    # Runtime rollover keeps an SDK-owned background task on its original
    # process while the visible conversation continues in a point-in-time
    # fork.  These links are MuseLab-only metadata: the predecessor remains
    # addressable for task controls, but is hidden from the ordinary picker.
    normalized["runtime_shadow"] = bool(
        normalized.get("runtime_shadow", False)
    )
    normalized["runtime_successor"] = str(
        normalized.get("runtime_successor") or ""
    )
    normalized["runtime_predecessor"] = str(
        normalized.get("runtime_predecessor") or ""
    )
    normalized["runtime_boundary_message_id"] = str(
        normalized.get("runtime_boundary_message_id") or ""
    )
    normalized["runtime_fork_boundary_at"] = _normalize_iso_utc(
        normalized.get("runtime_fork_boundary_at")
    )
    return normalized


def _sidecar_path(sid: str) -> Path:
    return SESS_DIR / f"{sid}.sidecar.json"


def _load_index() -> list[dict]:
    try:
        raw = INDEX.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A corrupt/unreadable index is not an empty index.  Returning [] here
        # lets the next read-modify-write silently replace every session's
        # metadata with one fresh row.  Fail closed so the original file stays
        # available for repair or recovery.
        raise RuntimeError(f"cannot parse session index: {INDEX}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"session index must contain a list: {INDEX}")
    return data


def _save_index(items: list[dict]) -> None:
    # Canonicalize legacy permission metadata on the next ordinary mutation.
    # Reads normalize in memory only, so merely opening an old session never
    # rewrites index.json.
    canonical = [
        _normalize_session_permission_fields(item)
        if isinstance(item, dict) else item
        for item in items
    ]
    atomic_write_text(
        INDEX, json.dumps(canonical, ensure_ascii=False, indent=2), mode=0o600)
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

# Explicit deletion and SDK-only queue adoption must be one linearizable
# lifecycle operation.  Fixed striping avoids an unbounded lock registry while
# keeping unrelated sessions concurrent in the common case.  Lock order is:
# lifecycle stripe -> queue -> index/sidecar.
_SESSION_LIFECYCLE_LOCKS = tuple(threading.RLock() for _ in range(64))


@contextlib.contextmanager
def session_lifecycle_lock(sid: str):
    lock = _SESSION_LIFECYCLE_LOCKS[hash(sid) % len(_SESSION_LIFECYCLE_LOCKS)]
    with lock:
        yield

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
    except FileNotFoundError:
        return {"messages": {}}
    sig = (st.st_mtime, st.st_size)
    if use_cache:
        hit = _SIDECAR_CACHE.get(sid)
        if hit is not None and hit[0] == sig[0] and hit[1] == sig[1]:
            return hit[2]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("sidecar root must be an object")
        d.setdefault("messages", {})
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        # Mutators call this with use_cache=False.  Treating malformed data as
        # an empty sidecar would make their next save destroy annotations and
        # pending attachments.  Surface the corruption and preserve the file.
        raise RuntimeError(f"cannot parse session sidecar: {p}") from exc
    if use_cache:
        if len(_SIDECAR_CACHE) >= _SIDECAR_CACHE_MAX and sid not in _SIDECAR_CACHE:
            _SIDECAR_CACHE.pop(next(iter(_SIDECAR_CACHE)), None)
        _SIDECAR_CACHE[sid] = (sig[0], sig[1], d)
    return d


def _save_sidecar(sid: str, data: dict) -> None:
    atomic_write_text(
        _sidecar_path(sid), json.dumps(data, ensure_ascii=False), mode=0o600)
    # Drop rather than refresh: the next _load_sidecar re-stats and caches
    # the just-written file, keeping cache state derived purely from disk.
    _SIDECAR_CACHE.pop(sid, None)


def indexed_session_ids() -> set[str]:
    """Return every id in the raw index, including removed workspaces.

    This is intentionally different from list_sessions(), whose public view
    hides rows belonging to a workspace that is temporarily unregistered.
    Storage GC must use the durable index view or it can mistake those hidden
    sessions for deleted sessions and permanently remove their attachments.
    """
    with _INDEX_LOCK:
        return {
            str(row["id"])
            for row in _load_index()
            if isinstance(row, dict) and row.get("id")
        }


# ============================================================================
# Session-level CRUD (metadata only — no transcript handling)
# ============================================================================

def _merge_sdk_with_index(
    info: Any,
    m: dict,
    workspace: str | Path | None = None,
) -> dict:
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
        "permission": m.get("permission", ""),
        "plan_return_permission": _normalize_plan_return_permission(
            m.get("permission"),
            m.get("plan_return_permission"),
        ),
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
        # override to defaults. effort: auto = model default. thinking: extended
        # thinking on/off — default True so existing sessions keep reasoning.
        "effort": str(m.get("effort") or "").strip() or "auto",
        "service_tier": "fast" if m.get("service_tier") == "fast" else "",
        "thinking": bool(m.get("thinking", True)),
        # Fork lineage is muselab-only metadata. The SDK owns the copied
        # transcript but does not expose the source relationship when listing
        # sessions, so keep a lightweight backlink for the UI.
        "forked_from": m.get("forked_from", ""),
        "forked_from_name": m.get("forked_from_name", ""),
        "forked_from_message_id": m.get("forked_from_message_id", ""),
        # Lightweight side-question branches are real resumable sessions, but
        # they are deliberately absent from the global task ledger. Keep that
        # distinction in durable session metadata so reloads and later turns
        # do not accidentally promote them into Task Center.
        "activity_hidden": bool(m.get("activity_hidden", False)),
        "runtime_profile": (
            "side_question"
            if m.get("runtime_profile") == "side_question"
            else ""
        ),
        "runtime_shadow": bool(m.get("runtime_shadow", False)),
        "runtime_successor": str(m.get("runtime_successor") or ""),
        "runtime_predecessor": str(m.get("runtime_predecessor") or ""),
        "runtime_boundary_message_id": str(
            m.get("runtime_boundary_message_id") or ""
        ),
        "runtime_fork_boundary_at": _normalize_iso_utc(
            m.get("runtime_fork_boundary_at")
        ),
        # Every conversation is bound to the directory the Claude SDK was
        # started in.  Keep it in the sidecar so history, files and previews
        # switch as one workspace instead of silently falling back to ROOT.
        "cwd": str(
            m.get("cwd")
            if workspace_registry.contains(m.get("cwd"))
            else (workspace or ROOT)
        ),
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
            "id": sid, "name": "", "model": "",
            "permission": "", "plan_return_permission": "",
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
            "id": sid, "name": "", "model": "",
            "permission": "", "plan_return_permission": "",
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
    # A cancellation-resistant SDK writer can finish after DELETE has removed
    # the transcript.  Snapshot the process tombstones and filter both SDK and
    # index rows so that late disk activity cannot resurrect a deleted session
    # in the UI while the process is still alive.
    with _QUEUE_LOCK:
        deleted_ids = set(_DELETED_SESSION_IDS)
    index = [s for s in _load_index() if s.get("id") not in deleted_ids]
    index_by_id = {s["id"]: s for s in index}
    sdk_list: list[tuple[Any, Path]] = []
    for workspace in workspace_registry.paths():
        try:
            sdk_list.extend(
                (info, workspace)
                for info in sdk_list_sessions(directory=str(workspace))
            )
        except Exception as e:
            sys.stderr.write(
                f"[sessions] sdk_list_sessions failed for {workspace}, "
                f"falling back to index.json for that workspace: "
                f"{type(e).__name__}: {e}\n")
    out: list[dict] = []
    seen: set[str] = set()
    for info, workspace in sdk_list:
        if info.session_id in seen or info.session_id in deleted_ids:
            continue
        m = index_by_id.get(info.session_id, {})
        merged = _merge_sdk_with_index(info, m, workspace)
        if not merged.get("runtime_shadow"):
            out.append(merged)
        seen.add(info.session_id)
    # Append muselab-only entries (no JSONL on disk yet — usually because
    # the user just created the session but hasn't sent the first message).
    for s in index:
        if s["id"] not in seen:
            # Rows owned by a workspace that has since been removed stay in
            # index.json (so re-registering the directory restores them), but
            # must not leak into the active session list meanwhile.  Legacy
            # rows without cwd belong to the primary workspace.
            if s.get("cwd") and not workspace_registry.contains(s.get("cwd")):
                continue
            row = dict(s)
            row = _normalize_session_permission_fields(row)
            if row.get("runtime_shadow"):
                continue
            # Legacy muselab releases persisted per-session system prompts.
            # Keep the inert key on disk for non-destructive compatibility,
            # but never expose or execute it.
            row.pop("system_prompt", None)
            row.setdefault("cwd", str(ROOT))
            out.append(row)
    # Sort: pinned sessions first (descending), then by updated_at desc.
    return sorted(
        out,
        key=lambda s: (1 if s.get("pinned") else 0, s.get("updated_at", 0)),
        reverse=True,
    )


def create_session(
    name: str = "",
    model: str = "",
    cwd: str | Path | None = None,
    permission: str = "",
    plan_return_permission: str | None = None,
    activity_hidden: bool = False,
    runtime_profile: str = "",
) -> dict:
    return register_session(str(uuid.uuid4()), name=name, model=model,
                            permission=permission,
                            plan_return_permission=plan_return_permission,
                            auto_named=True, cwd=cwd,
                            activity_hidden=activity_hidden,
                            runtime_profile=runtime_profile)


def register_session(sid: str, *, name: str = "", model: str = "",
                     permission: str = "",
                     plan_return_permission: str | None = None,
                     auto_named: bool = True,
                     message_count: int = 0,
                     turn_count: int | None = None,
                     effort: str = "auto",
                     service_tier: str = "",
                     thinking: bool = True,
                     forked_from: str = "",
                     forked_from_name: str = "",
                     forked_from_message_id: str = "",
                     activity_hidden: bool = False,
                     runtime_profile: str = "",
                     runtime_predecessor: str = "",
                     runtime_fork_boundary_at: str | datetime = "",
                     cwd: str | Path | None = None) -> dict:
    """Add a session that already has a UUID (e.g. one minted by SDK
    fork_session) to the muselab index. Same shape as create_session
    but without generating a fresh UUID."""
    now = time.time()
    workspace = workspace_registry.resolve(cwd)
    if runtime_profile not in ("", "side_question"):
        raise ValueError("invalid runtime profile")
    normalized_runtime_fork_boundary_at = _normalize_iso_utc(
        runtime_fork_boundary_at
    )
    if runtime_fork_boundary_at and not normalized_runtime_fork_boundary_at:
        raise ValueError("runtime_fork_boundary_at must be timezone-aware ISO 8601")
    meta = {
        "id": sid,
        "name": name or _default_session_name(),
        "model": model,
        "permission": permission,
        "plan_return_permission": _normalize_plan_return_permission(
            permission,
            plan_return_permission,
        ),
        "created_at": now,
        "updated_at": now,
        "message_count": message_count,
        "turn_count": (
            turn_count
            if turn_count is not None
            else max(0, message_count // 2)
        ),
        "auto_named": auto_named,
        "effort": str(effort or "").strip() or "auto",
        "service_tier": "fast" if service_tier == "fast" else "",
        "thinking": bool(thinking),
        "activity_hidden": bool(activity_hidden),
        "runtime_profile": runtime_profile,
        "runtime_predecessor": str(runtime_predecessor or ""),
        "runtime_shadow": False,
        "runtime_successor": "",
        "runtime_boundary_message_id": "",
        "runtime_fork_boundary_at": normalized_runtime_fork_boundary_at,
        "cwd": str(workspace),
    }
    if forked_from:
        meta["forked_from"] = forked_from
        meta["forked_from_name"] = forked_from_name
        meta["forked_from_message_id"] = forked_from_message_id
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            with _INDEX_LOCK:
                idx = _load_index()
                # Idempotent: if this id is already registered (client retry /
                # keepalive resend of an optimistic-create POST, or a fork that
                # re-registers), return the existing row instead of appending a
                # duplicate. Duplicate ids break list dedupe and x-for keys.
                existing = next((s for s in idx if s.get("id") == sid), None)
                if existing is not None:
                    public_existing = _normalize_session_permission_fields(existing)
                    public_existing.pop("system_prompt", None)
                    return public_existing
                # A stale optimistic-create retry may arrive after DELETE has
                # fenced the id but before its final disk sweep. Never let that
                # retry clear the process tombstone and admit a new turn into
                # the deletion window. Fresh sessions always use a new UUID.
                if sid in _DELETED_SESSION_IDS:
                    raise ValueError("session is being deleted")
                idx.append(meta)
                _save_index(idx)
        # Keep lifecycle ownership through the final sidecar write. Otherwise
        # DELETE can remove the new index row in the gap and this write would
        # leave an orphan metadata file after deletion already returned.
        try:
            _save_sidecar(sid, {"messages": {}})
        except Exception as e:
            sys.stderr.write(
                f"[sessions] warning: sidecar write failed for {sid}: {e}\n")
    return meta


def get_session_meta(sid: str) -> dict | None:
    """Returns just the session-level metadata. For full session view (with
    transcript), use chat.py's combined read path that pulls from SDK.

    Merges SDK truth (custom_title, last_modified, created_at, tag) with
    muselab index (model, permission, auto_named). Falls back to
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
    candidates: tuple[Path, ...]
    if m and workspace_registry.contains(m.get("cwd")):
        candidates = (workspace_registry.resolve(m.get("cwd")),)
    else:
        candidates = workspace_registry.paths()
    for workspace in candidates:
        try:
            info = sdk_get_session_info(sid, directory=str(workspace))
            if info is not None:
                break
        except Exception as e:
            sys.stderr.write(
                f"[sessions] sdk_get_session_info({sid}, {workspace}) failed: "
                f"{type(e).__name__}: {e}\n")
    if info is not None:
        meta = _merge_sdk_with_index(info, m or {}, workspace)
    elif m is not None:
        meta = _normalize_session_permission_fields(
            {**m, "cwd": str(m.get("cwd") or ROOT)}
        )
        # Read-only compatibility for indexes written by older releases.
        meta.pop("system_prompt", None)
    else:
        meta = None
    if meta is not None:
        if len(_META_CACHE) >= _META_CACHE_MAX and sid not in _META_CACHE:
            _META_CACHE.pop(next(iter(_META_CACHE)), None)
        _META_CACHE[sid] = (now, meta)
    return meta


# Back-compat alias — some code calls get_session() expecting metadata.
get_session = get_session_meta


def get_session_for_queue(sid: str) -> tuple[dict | None, bool]:
    """Return queue-launch metadata plus an explicit SDK-truth flag.

    Queue enqueue cannot use :func:`get_session_meta` here: its merged/cache
    result does not reveal whether the session came from the durable MuseLab
    index or from an SDK transcript.  That distinction matters when pruning
    races an enqueue.  A stale index snapshot must never be allowed to recreate
    a row that prune already removed; only a session that was *already*
    SDK-only at this preflight may self-heal its missing index row.

    This helper deliberately bypasses the metadata cache.  Callers run it in a
    worker thread because the SDK probe can touch multiple workspace files.
    """
    with _INDEX_LOCK:
        indexed = next(
            (
                dict(row)
                for row in _load_index()
                if isinstance(row, dict) and row.get("id") == sid
            ),
            None,
        )
    if indexed is not None:
        meta = _normalize_session_permission_fields(
            {**indexed, "cwd": str(indexed.get("cwd") or ROOT)}
        )
        meta.pop("system_prompt", None)
        return meta, False

    for workspace in workspace_registry.paths():
        try:
            info = sdk_get_session_info(sid, directory=str(workspace))
        except Exception as exc:
            sys.stderr.write(
                f"[sessions] sdk queue preflight failed for {sid}: "
                f"{type(exc).__name__}: {exc}\n"
            )
            continue
        if info is not None:
            return _merge_sdk_with_index(info, {}, workspace), True
    return None, False


def session_workspace(sid: str) -> Path:
    """Return the registered working directory owned by ``sid``.

    Legacy rows created before multi-workspace support intentionally resolve
    to the primary ``MUSELAB_ROOT``.  A removed/unavailable workspace also
    fails closed to the primary root instead of accepting an arbitrary path
    from stale metadata.
    """
    meta = get_session_meta(sid)
    cwd = meta.get("cwd") if meta else None
    try:
        return workspace_registry.resolve(cwd)
    except ValueError:
        return workspace_registry.resolve()


def begin_session_delete(sid: str) -> None:
    """Prevent any later queue mutation from recreating ``sid`` this process."""
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            _DELETED_SESSION_IDS.add(sid)


def delete_session(sid: str) -> bool:
    """Atomically remove MuseLab metadata and queue state for ``sid``.

    Caller is responsible for deleting the SDK transcript.  The lifecycle and
    queue locks make this linearizable with enqueue/claim/ack/release: a queue
    mutation either lands before deletion and is then removed, or observes the
    process tombstone and cannot recreate an orphan sidecar afterward.
    """
    removed = False
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            _DELETED_SESSION_IDS.add(sid)
            with _INDEX_LOCK:
                idx = _load_index()
                new = [s for s in idx if s.get("id") != sid]
                if len(new) != len(idx):
                    _save_index(new)
                    removed = True
            # Sidecar readers may also bind a pending attachment as part of a
            # GET. Serialize its unlink with every sidecar RMW so an old
            # snapshot cannot be saved after deletion and recreate private
            # attachment metadata.
            with _SIDECAR_LOCK:
                sidecar = _sidecar_path(sid)
                if sidecar.exists():
                    try:
                        sidecar.unlink()
                        _SIDECAR_CACHE.pop(sid, None)
                        removed = True
                    except OSError:
                        pass
            for path in (
                SESS_DIR / f"{sid}.transcript-index.json",
                _queue_path(sid),
            ):
                if path.exists():
                    try:
                        path.unlink()
                        removed = True
                    except OSError:
                        pass
    return removed


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
    # Data-loss guard: never delete a session that has an on-disk transcript,
    # regardless of its cached message_count. A stale message_count=0 (older
    # imports, transcripts written outside muselab's turn path) would
    # otherwise let this prune a session full of real messages. Sessions the
    # SDK can enumerate HAVE a JSONL → treat as non-empty and skip. Only
    # truly transcript-less index stubs (created-but-never-sent) are eligible.
    transcript_ids: set[str] = set()
    for workspace in workspace_registry.paths():
        try:
            transcript_ids.update(
                info.session_id
                for info in sdk_list_sessions(directory=str(workspace))
            )
        except Exception:
            # If we can't confirm which sessions have transcripts, fail SAFE:
            # delete nothing rather than risk nuking real history.
            return []
    # Queue state and its sidecar deletion share one lock. An enqueue that
    # commits before this section is therefore visible and protects its
    # session; none can slip between the final queue check and file removal.
    # A malformed/unreadable queue also fails SAFE: uncertainty is work, so
    # preserve the session rather than deleting a possibly accepted message.
    with _QUEUE_LOCK:
        with _INDEX_LOCK:
            idx = _load_index()
            to_delete = []
            for row in idx:
                sid = row["id"]
                if not (
                    row.get("message_count", 0) == 0
                    and sid not in transcript_ids
                    and not row.get("pinned")
                    and row.get("auto_named", True)
                    and row.get("created_at", 0) > cutoff
                    and sid not in keep
                ):
                    continue
                try:
                    queue = _load_queue(sid, strict=True)
                except RuntimeError:
                    continue
                if queue.get("items") or queue.get("inflight"):
                    continue
                to_delete.append(sid)
            if not to_delete:
                return []
            to_delete_set = set(to_delete)
            _save_index([s for s in idx if s["id"] not in to_delete_set])

        # A late queue task may have captured this sid before the prune. The
        # tombstone makes every later queue save a no-op instead of recreating
        # the just-removed sidecar.
        _DELETED_SESSION_IDS.update(to_delete)

        # Remove MuseLab-owned state before releasing the queue lock. This
        # keeps the check + index mutation + queue-sidecar removal one atomic
        # transaction with respect to every queue mutator.
        for sid in to_delete:
            with _SIDECAR_LOCK:
                p = _sidecar_path(sid)
                if p.exists():
                    try:
                        p.unlink()
                        _SIDECAR_CACHE.pop(sid, None)
                    except OSError:
                        pass
            q = _queue_path(sid)
            if q.exists():
                try:
                    q.unlink()
                except OSError:
                    pass
            transcript_index = SESS_DIR / f"{sid}.transcript-index.json"
            if transcript_index.exists():
                try:
                    transcript_index.unlink()
                except OSError:
                    pass

    # SDK JSONLs are independent of the MuseLab queue lock and can be slow to
    # remove. Keep this best-effort cleanup outside both local locks.
    for sid in to_delete:
        try:
            workspace = next(
                (s.get("cwd") for s in idx if s.get("id") == sid),
                str(ROOT),
            )
            sdk_delete_session(
                sid,
                directory=str(workspace_registry.resolve(workspace)),
            )
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


def set_runtime_background_boundary(sid: str, message_id: str) -> bool:
    """Persist the last ordinary assistant record safe for runtime rollover.

    A background-task notification can later append an implicit user record
    and an automatic assistant continuation to the source transcript.  Forking
    at this explicit boundary keeps those source-only records out of the new
    interactive runtime.
    """
    boundary = str(message_id or "")
    if not boundary:
        return False
    with _INDEX_LOCK:
        idx = _load_index()
        for row in idx:
            if row.get("id") != sid:
                continue
            if row.get("runtime_boundary_message_id") == boundary:
                return True
            row["runtime_boundary_message_id"] = boundary
            _save_index(idx)
            return True
    return False


def link_runtime_successor(source_sid: str, successor_sid: str) -> bool:
    """Atomically mark ``source`` as a hidden runtime owned by ``successor``."""
    if not source_sid or not successor_sid or source_sid == successor_sid:
        return False
    with _INDEX_LOCK:
        idx = _load_index()
        source = next(
            (row for row in idx if row.get("id") == source_sid), None)
        successor = next(
            (row for row in idx if row.get("id") == successor_sid), None)
        if source is None or successor is None:
            return False
        existing = str(source.get("runtime_successor") or "")
        if existing and existing != successor_sid:
            return False
        predecessor = str(successor.get("runtime_predecessor") or "")
        if predecessor and predecessor != source_sid:
            return False
        source["runtime_shadow"] = True
        source["runtime_successor"] = successor_sid
        successor["runtime_predecessor"] = source_sid
        successor["runtime_shadow"] = False
        _save_index(idx)
        return True


def unlink_runtime_successor(source_sid: str, successor_sid: str) -> bool:
    """Best-effort rollback for a rollover that failed after metadata link."""
    with _INDEX_LOCK:
        idx = _load_index()
        source = next(
            (row for row in idx if row.get("id") == source_sid), None)
        successor = next(
            (row for row in idx if row.get("id") == successor_sid), None)
        if source is None:
            return False
        if str(source.get("runtime_successor") or "") != successor_sid:
            return False
        source["runtime_shadow"] = False
        source["runtime_successor"] = ""
        if successor is not None and str(
                successor.get("runtime_predecessor") or "") == source_sid:
            successor["runtime_predecessor"] = ""
        _save_index(idx)
        return True


_RUNTIME_LINEAGE_MAX = 32


def _runtime_lineage_from_rows(sid: str, rows: list[dict]) -> list[str]:
    """Return the linked runtime chain containing ``sid``, oldest first."""
    sid = str(sid or "")
    by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }
    if not sid or sid not in by_id:
        return []

    predecessors: list[str] = []
    seen = {sid}
    current = sid
    while len(seen) < _RUNTIME_LINEAGE_MAX:
        predecessor = str(
            by_id.get(current, {}).get("runtime_predecessor") or ""
        )
        if not predecessor or predecessor in seen or predecessor not in by_id:
            break
        predecessors.append(predecessor)
        seen.add(predecessor)
        current = predecessor

    lineage = list(reversed(predecessors)) + [sid]
    current = sid
    while len(lineage) < _RUNTIME_LINEAGE_MAX:
        successor = str(
            by_id.get(current, {}).get("runtime_successor") or ""
        )
        if not successor or successor in seen or successor not in by_id:
            break
        lineage.append(successor)
        seen.add(successor)
        current = successor
    return lineage


def runtime_lineage(sid: str) -> list[str]:
    """Return the durable rollover lineage containing ``sid``, oldest first.

    One index snapshot is used for the entire walk.  Reading predecessor and
    successor links through separate ``get_session_meta`` calls could otherwise
    splice together two generations while a rollover commits or rolls back.
    Broken links and cycles are bounded defensively rather than followed beyond
    the indexed chain.
    """
    with _INDEX_LOCK:
        return _runtime_lineage_from_rows(sid, _load_index())


def update_model(sid: str, model: str) -> None:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["model"] = model
                _save_index(idx)
                return


def update_runtime_controls(
    sid: str, *, model: str, effort: str, service_tier: str,
) -> bool:
    """Persist the coupled model/effort/service selection in one index write.

    The API validates the complete target combination before calling this
    function. Keeping the three fields in one locked mutation prevents a
    rejected model switch from leaving only effort or Fast changed on disk.
    """
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["model"] = model
                s["effort"] = effort
                s["service_tier"] = service_tier
                _save_index(idx)
                return True
        return False


def update_permission(
    sid: str,
    permission: str,
    *,
    plan_return_permission: str | None = None,
) -> bool:
    """Atomically persist the visible mode and Plan's eventual return mode.

    Entering Plan captures the previous non-Plan mode unless the caller
    supplies an explicit return mode.  Re-applying Plan preserves the existing
    return mode.  Leaving Plan clears the now-inert field.
    """
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                previous = (
                    s.get("permission", "").strip()
                    if isinstance(s.get("permission"), str)
                    else ""
                )
                if permission == "plan":
                    if plan_return_permission is not None:
                        candidate = plan_return_permission
                    elif previous == "plan":
                        candidate = s.get("plan_return_permission")
                    else:
                        candidate = previous
                    s["plan_return_permission"] = (
                        _normalize_plan_return_permission("plan", candidate)
                    )
                else:
                    s["plan_return_permission"] = ""
                s["permission"] = permission
                _save_index(idx)
                return True
        return False


def commit_plan_exit(
    sid: str,
    permission: str,
    *,
    expected_plan_return: str | None = None,
) -> bool:
    """Compare-and-set a completed ExitPlanMode transition.

    Approval can overlap a PATCH from another browser/device. Only the SDK
    runtime that is still durably in Plan Mode, with the same return contract
    it was launched under, may commit its chosen exit mode. If a newer user
    action already changed/re-entered Plan Mode, leave that choice untouched
    and let the caller discard the now-stale runtime.
    """
    if permission == "plan" or permission not in _VALID_PLAN_RETURN_PERMISSIONS:
        return False
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] != sid:
                continue
            current = (
                s.get("permission", "").strip()
                if isinstance(s.get("permission"), str)
                else ""
            )
            if current != "plan":
                return False
            if expected_plan_return is not None:
                current_return = _normalize_plan_return_permission(
                    "plan", s.get("plan_return_permission"))
                expected_return = _normalize_plan_return_permission(
                    "plan", expected_plan_return)
                if current_return != expected_return:
                    return False
            s["permission"] = permission
            s["plan_return_permission"] = ""
            _save_index(idx)
            return True
        return False


# effort is canonical auto | low | medium | high | xhigh | max | ultra.
# `auto` asks Gateway to retain the chosen model's catalog default. Stored per
# session so a deep-research effort on one tab doesn't leak into others.
def update_effort(sid: str, effort: str) -> None:
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["effort"] = effort
                _save_index(idx)
                return


def update_service_tier(sid: str, service_tier: str) -> None:
    """Persist the user-facing service tier (empty/default or ``fast``)."""
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                s["service_tier"] = service_tier
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


# ============================================================================
# Per-message annotations (cost, model, images, custom UI markers)
# ============================================================================

def get_message_annotations(sid: str) -> dict[str, dict]:
    """Per-message metadata keyed by message UUID. Empty dict if no sidecar."""
    return _load_sidecar(sid).get("messages", {})


def get_runtime_task_overlays(sid: str) -> dict[str, dict]:
    """Return UI-only background task cards keyed by task id.

    Runtime-rollover children never resume the predecessor's CLI process.  The
    overlay lets their copied tool card reflect that process's lifecycle while
    remaining completely absent from model context.
    """
    raw = _load_sidecar(sid).get("runtime_task_overlays") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(task_id): dict(value)
        for task_id, value in raw.items()
        if task_id and isinstance(value, dict)
    }


_RUNTIME_TASK_TERMINAL_STATES = frozenset({
    "completed", "failed", "stopped",
})
_RUNTIME_TASK_CROSS_COPY_IDENTITY_FIELDS = frozenset({
    "tool_use_id", "description",
})


def _normalized_runtime_task_state(value: Any) -> Any:
    """Normalize legacy terminal spellings without coercing unknown values."""
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if normalized == "done":
        return "completed"
    if normalized == "killed":
        return "stopped"
    return normalized


def set_runtime_task_overlay(
    sid: str,
    task_id: str,
    *,
    owner_session_id: str | None = None,
    **fields: Any,
) -> bool:
    """Merge one durable UI-only task status into a session sidecar.

    The first writer establishes the process owner.  Copies in rollover
    successors retain that owner forever, and a task that has reached one
    terminal state can neither be revived nor rewritten as a different
    terminal outcome by a delayed notification.  The return value says whether
    durable state actually changed.
    """
    task_id = str(task_id or "")
    if not task_id:
        return False
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
                return False
        with _SIDECAR_LOCK:
            data = _load_sidecar(sid, use_cache=False)
            overlays = data.setdefault("runtime_task_overlays", {})
            if not isinstance(overlays, dict):
                overlays = {}
                data["runtime_task_overlays"] = overlays
            stored = overlays.get(task_id)
            current = dict(stored) if isinstance(stored, dict) else {
                "task_id": task_id,
            }
            existing_owner = str(current.get("owner_session_id") or "")
            requested_owner = str(owner_session_id or "")
            if existing_owner and requested_owner not in ("", existing_owner):
                return False
            effective_owner = existing_owner or requested_owner or sid

            incoming = {
                key: value for key, value in fields.items()
                if value is not None
            }
            if "state" in incoming:
                incoming["state"] = _normalized_runtime_task_state(
                    incoming["state"]
                )
            current_state = _normalized_runtime_task_state(
                current.get("state")
            )
            incoming_state = incoming.get("state")

            # At-least-once delivery can reorder terminal TaskNotification
            # records.  Once terminal, a different terminal result is never a
            # harmless enrichment: rejecting the complete patch keeps summary,
            # usage and timestamps tied to the authoritative outcome.
            if (
                current_state in _RUNTIME_TASK_TERMINAL_STATES
                and incoming_state in _RUNTIME_TASK_TERMINAL_STATES
                and incoming_state != current_state
            ):
                return False

            # Task lifecycle delivery is at-least-once and can be reordered:
            # a delayed launch/backfill patch may arrive after the terminal
            # TaskNotification. Terminality is durable truth, so that stale
            # ``running`` state may enrich immutable card identity only.  It
            # must not replace terminal summary/usage/timing metadata.
            late_running_patch = (
                current_state in _RUNTIME_TASK_TERMINAL_STATES
                and incoming_state == "running"
            )
            if late_running_patch:
                incoming = {
                    key: value for key, value in incoming.items()
                    if (
                        key in _RUNTIME_TASK_CROSS_COPY_IDENTITY_FIELDS
                        and not current.get(key)
                    )
                }

            updated = dict(current)
            updated["owner_session_id"] = effective_owner
            for key, value in incoming.items():
                updated[key] = value
            if current_state in _RUNTIME_TASK_TERMINAL_STATES:
                updated["state"] = current_state
            elif "state" in updated:
                updated["state"] = _normalized_runtime_task_state(
                    updated["state"]
                )
            updated["task_id"] = task_id
            if isinstance(stored, dict) and stored == updated:
                return False
            overlays[task_id] = updated
            _save_sidecar(sid, data)
            return True


def _authoritative_runtime_task_snapshot(
    task_id: str,
    lineage: list[str],
    overlays_by_sid: dict[str, dict[str, dict]],
) -> tuple[str, dict] | None:
    """Resolve a task once, from its earliest durable lineage appearance.

    Successor overlays are replicated UI views.  Their later lifecycle state
    is never allowed to overrule the process owner's record; they may only fill
    missing card identity fields.
    """
    candidates: list[tuple[int, str, dict]] = []
    for position, row_sid in enumerate(lineage):
        value = overlays_by_sid.get(row_sid, {}).get(task_id)
        if isinstance(value, dict):
            candidates.append((position, row_sid, value))
    if not candidates:
        return None

    first_position, first_sid, first = candidates[0]
    declared_owner = str(first.get("owner_session_id") or "")
    try:
        declared_position = lineage.index(declared_owner)
    except ValueError:
        declared_position = -1
    # A copied task may first survive in a child while correctly naming an
    # earlier owner whose sidecar was lost.  A claimed future owner is not a
    # valid provenance edge and falls back to the first durable appearance.
    owner_sid = (
        declared_owner
        if 0 <= declared_position <= first_position
        else first_sid
    )
    owner_record = overlays_by_sid.get(owner_sid, {}).get(task_id)
    authority = (
        owner_record if isinstance(owner_record, dict) else first
    )
    canonical = dict(authority)
    canonical["task_id"] = task_id
    canonical["owner_session_id"] = owner_sid
    if "state" in canonical:
        canonical["state"] = _normalized_runtime_task_state(
            canonical["state"]
        )
    for _position, _row_sid, candidate in candidates:
        for key in _RUNTIME_TASK_CROSS_COPY_IDENTITY_FIELDS:
            if not canonical.get(key) and candidate.get(key) is not None:
                canonical[key] = candidate[key]
    return owner_sid, canonical


def get_authoritative_runtime_task_overlays(sid: str) -> dict[str, dict]:
    """Return lineage-visible task cards resolved from their true owner.

    A source runtime cannot see tasks launched by a later successor.  A child
    can see predecessor-owned tasks even if a crash interrupted the physical
    overlay copy, because those owners precede it in the durable lineage.
    """
    lineage = runtime_lineage(sid)
    if not lineage or sid not in lineage:
        return get_runtime_task_overlays(sid)
    current_position = lineage.index(sid)
    visible_lineage = lineage[:current_position + 1]
    overlays_by_sid = {
        row_sid: get_runtime_task_overlays(row_sid)
        for row_sid in lineage
    }
    visible_task_ids = {
        task_id
        for row_sid in visible_lineage
        for task_id in overlays_by_sid.get(row_sid, {})
    }
    authoritative: dict[str, dict] = {}
    for task_id in visible_task_ids:
        resolved = _authoritative_runtime_task_snapshot(
            task_id, lineage, overlays_by_sid
        )
        if resolved is not None:
            authoritative[task_id] = resolved[1]
    return authoritative


def _replace_runtime_task_overlay_snapshot(
    sid: str,
    task_id: str,
    snapshot: dict,
) -> bool:
    """Repair helper that replaces one copied snapshot exactly."""
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
                return False
        with _SIDECAR_LOCK:
            data = _load_sidecar(sid, use_cache=False)
            overlays = data.setdefault("runtime_task_overlays", {})
            if not isinstance(overlays, dict):
                overlays = {}
                data["runtime_task_overlays"] = overlays
            replacement = dict(snapshot)
            replacement["task_id"] = task_id
            if overlays.get(task_id) == replacement:
                return False
            overlays[task_id] = replacement
            _save_sidecar(sid, data)
            return True


def reconcile_runtime_task_overlay_chains() -> int:
    """Repair owner snapshots across every indexed runtime rollover chain.

    Reconciliation is intended for startup before orphan settlement.  It
    restores copied descendants from the earliest owner snapshot, then the
    ordinary stale-task pass can settle only the genuine owner state and
    replicate that result without mistaking a successor for a new process.
    """
    with _INDEX_LOCK:
        rows = _load_index()
    indexed_ids = [
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]
    repaired = 0
    visited: set[str] = set()
    for indexed_sid in indexed_ids:
        if indexed_sid in visited:
            continue
        lineage = _runtime_lineage_from_rows(indexed_sid, rows)
        if not lineage:
            continue
        visited.update(lineage)
        overlays_by_sid = {
            row_sid: get_runtime_task_overlays(row_sid)
            for row_sid in lineage
        }
        task_ids = {
            task_id
            for overlay in overlays_by_sid.values()
            for task_id in overlay
        }
        for task_id in task_ids:
            resolved = _authoritative_runtime_task_snapshot(
                task_id, lineage, overlays_by_sid
            )
            if resolved is None:
                continue
            owner_sid, canonical = resolved
            try:
                owner_position = lineage.index(owner_sid)
            except ValueError:
                continue
            # Child-owned tasks do not exist in predecessor context.  Copy the
            # canonical owner record only forward from its launch runtime.
            for target_sid in lineage[owner_position:]:
                if _replace_runtime_task_overlay_snapshot(
                    target_sid, task_id, canonical
                ):
                    repaired += 1
                    overlays_by_sid.setdefault(target_sid, {})[task_id] = dict(
                        canonical
                    )
    return repaired


def copy_runtime_task_overlays(source_sid: str, target_sid: str) -> int:
    """Copy source task-card state to a rollover child, preserving owner id."""
    overlays = get_runtime_task_overlays(source_sid)
    for task_id, overlay in overlays.items():
        fields = {key: value for key, value in overlay.items()
                  if key != "task_id"}
        set_runtime_task_overlay(target_sid, task_id, **fields)
    return len(overlays)


def stop_stale_runtime_task_overlays() -> int:
    """Settle persisted running cards whose process-local owner was restarted.

    Runtime task readers, CLI clients and watcher pins are deliberately kept
    in memory.  When a new backend process starts, every durable ``running``
    overlay is therefore an orphan from the previous process, not evidence of
    a task that can still report a terminal notification to this process.
    """
    stopped = 0
    now_ms = int(time.time() * 1000)
    for sid in indexed_session_ids():
        for task_id, overlay in get_runtime_task_overlays(sid).items():
            if overlay.get("state") != "running":
                continue
            changed = set_runtime_task_overlay(
                sid,
                task_id,
                state="stopped",
                updated_at=now_ms,
                restart_recovered=True,
            )
            if changed:
                stopped += 1
    return stopped


def copy_message_annotations(
    source_sid: str,
    target_sid: str,
    uuid_mapping: dict[str, str],
) -> int:
    """Copy fork-visible annotations, re-keyed to the fork's fresh UUIDs."""
    if not uuid_mapping:
        return 0
    with session_lifecycle_lock(target_sid):
        with _QUEUE_LOCK:
            if target_sid in _DELETED_SESSION_IDS:
                return 0
        with _SIDECAR_LOCK:
            source = _load_sidecar(source_sid)
            target = _load_sidecar(target_sid, use_cache=False)
            source_messages = source.get("messages") or {}
            target_messages = target.setdefault("messages", {})
            copied = 0
            for old_uuid, new_uuid in uuid_mapping.items():
                annotation = source_messages.get(old_uuid)
                if not isinstance(annotation, dict):
                    continue
                target_messages[new_uuid] = dict(annotation)
                copied += 1
            if source.get("context_max_tokens"):
                target["context_max_tokens"] = source["context_max_tokens"]
            _save_sidecar(target_sid, target)
            return copied


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
    # Linearize terminal footer writes with explicit deletion. A Result worker
    # every worker arriving after the tombstone is a no-op and cannot recreate
    # the deleted sidecar.
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
                return
        with _SIDECAR_LOCK:
            data = _load_sidecar(sid, use_cache=False)
            msgs = data.setdefault("messages", {})
            cur = msgs.setdefault(msg_uuid, {})
            # Explicit user cancellation is monotonic truth.  A force-stopped
            # CLI can append its AssistantMessage/ResultMessage late; generic
            # terminal bookkeeping then attempts to write ``completed``.
            sticky_cancelled = (
                cur.get("turn_status") == "cancelled"
                and fields.get("turn_status") not in (None, "cancelled")
            )
            for k, v in fields.items():
                if v is None:
                    continue
                if sticky_cancelled and k in {"turn_status", "ts", "elapsed_s"}:
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
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
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
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
                return
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
    with session_lifecycle_lock(sid):
        with _QUEUE_LOCK:
            if sid in _DELETED_SESSION_IDS:
                return None
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


def set_message_count(sid: str, message_count: int,
                       turn_count: int | None = None) -> None:
    """Patch ONLY the cached message_count / turn_count for a session,
    WITHOUT touching updated_at (so it never reorders the session list).

    Why this exists separately from bump_session: bump_session always
    stamps updated_at (it's the "a turn just happened" signal), so it
    can't be used to lazily back-fill a stale count on a plain session
    OPEN — that would float every session you merely glance at to the
    top. This setter is the side-effect-free counterpart used by the
    self-heal in chat.get_session_api: some sessions (older imports,
    transcripts written outside muselab's turn path) carry a stale
    message_count=0 in the index despite having a real transcript on
    disk, which made the session list report "0 messages" for non-empty
    sessions. Writing the real count back here fixes the display and
    keeps prune_empty_sessions honest.

    Creates a minimal index stub if the session has no entry yet (an
    SDK-only session whose JSONL exists but was never registered) — same
    pattern as toggle_pin — so the corrected count actually persists.
    Idempotent: a no-op when the stored values already match.
    """
    with _INDEX_LOCK:
        idx = _load_index()
        for s in idx:
            if s["id"] == sid:
                changed = False
                if s.get("message_count") != message_count:
                    s["message_count"] = message_count
                    changed = True
                if turn_count is not None and s.get("turn_count") != turn_count:
                    s["turn_count"] = turn_count
                    changed = True
                if changed:
                    _save_index(idx)
                return
        # No index entry yet — create a minimal stub carrying the count.
        now = time.time()
        stub = {
            "id": sid, "name": "", "model": "",
            "permission": "", "plan_return_permission": "",
            "created_at": now, "updated_at": now,
            "message_count": message_count, "auto_named": True,
        }
        if turn_count is not None:
            stub["turn_count"] = turn_count
        idx.append(stub)
        _save_index(idx)


# ============================================================================
# Per-session message queue (server-side — drives autonomous draining)
# ============================================================================
# Stored in its OWN file (`{sid}.queue.json`), not the annotations sidecar,
# because the annotations sidecar is rewritten on every turn-done and every
# pending-attachment consume; mixing the queue in would widen the lost-update
# window between a queue mutation and an annotation write. A dedicated file +
# lock keeps the two independent.
#
# Shape: {"items": [{"id","text","display_text","selection_quotes",
#                    "image_ids","permission",
#                    "plan_return_permission","enqueued_at"}], "paused": bool}
#   - items: FIFO; head is sent next by the drain trigger in chat.py
#   - paused: set True when a queued turn errors / hits ask_user_question /
#     is user-cancelled; auto-drain stops until the user resumes
#
# Attachment caveat: image_ids reference the in-memory _image_store in chat.py
# which expires entries after 10 min and is empty after a restart.  The drain
# validates every referenced id before starting a turn; if one is unavailable,
# it atomically restores + pauses the item instead of silently sending text
# without the attachment.  The queue endpoint then exposes unavailable ids so
# the browser can offer an explicit edit/reattach recovery path.
_QUEUE_LOCK = threading.Lock()
_QUEUE_MAX = 10   # mirror the frontend cap
# Process-local deletion fence. It is always read/written under _QUEUE_LOCK.
# Disk deletion alone is insufficient: a cancelled drain can retain an old
# queue snapshot and otherwise write it back after unlink.
_DELETED_SESSION_IDS: set[str] = set()


def session_is_deleting(sid: str) -> bool:
    """Return whether explicit deletion has fenced this session in-process."""
    with _QUEUE_LOCK:
        return sid in _DELETED_SESSION_IDS


def _queue_path(sid: str) -> Path:
    return SESS_DIR / f"{sid}.queue.json"


def _empty_queue() -> dict:
    return {"revision": 0, "items": [], "inflight": None, "paused": False}


def _load_queue(sid: str, *, strict: bool = False) -> dict:
    if sid in _DELETED_SESSION_IDS:
        return _empty_queue()
    p = _queue_path(sid)
    if not p.exists():
        return _empty_queue()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("queue root must be an object")
        d.setdefault("revision", 0)
        d.setdefault("items", [])
        d.setdefault("inflight", None)
        d.setdefault("paused", False)
        try:
            revision = int(d["revision"])
            if strict and (
                isinstance(d["revision"], bool)
                or revision < 0
                or not isinstance(d["revision"], int)
            ):
                raise ValueError("queue revision must be a non-negative integer")
            d["revision"] = max(0, revision)
        except (TypeError, ValueError):
            if strict:
                raise ValueError("queue revision must be a non-negative integer") from None
            d["revision"] = 0
        if not isinstance(d["items"], list):
            if strict:
                raise ValueError("queue items must be a list")
            d["items"] = []
        else:
            if strict and not all(
                isinstance(item, dict) for item in d["items"]
            ):
                raise ValueError("queue items must be objects")
            # Compatibility is read-only here: legacy Plan items immediately
            # behave as default-on-exit, while GET does not rewrite the file.
            d["items"] = [
                _normalize_session_permission_fields(item)
                if isinstance(item, dict) else item
                for item in d["items"]
            ]
            if strict:
                item_ids = [str(item.get("id") or "") for item in d["items"]]
                if any(not item_id for item_id in item_ids):
                    raise ValueError("queue items must have ids")
                if len(set(item_ids)) != len(item_ids):
                    raise ValueError("queue item ids must be unique")
        inflight = d.get("inflight")
        if (
            not isinstance(inflight, dict)
            or not isinstance(inflight.get("item"), dict)
        ):
            if strict and inflight is not None:
                raise ValueError("queue inflight must contain an item object")
            d["inflight"] = None
        else:
            inflight_item = _normalize_session_permission_fields(inflight["item"])
            inflight_id = str(inflight_item.get("id") or "")
            if strict and not inflight_id:
                raise ValueError("queue inflight item must have an id")
            if strict and any(
                str(item.get("id") or "") == inflight_id for item in d["items"]
            ):
                raise ValueError("queue inflight id duplicates a waiting item")
            d["inflight"] = {
                "item": inflight_item,
                "turn_id": str(inflight.get("turn_id") or ""),
                "claimed_at": int(inflight.get("claimed_at") or 0),
            }
        if strict and not isinstance(d.get("paused"), bool):
            raise ValueError("queue paused must be a boolean")
        # ``paused`` only governs work still waiting in ``items``. The claimed
        # item is already owned by a turn and must be acknowledged by that turn.
        if not d["items"]:
            d["paused"] = False
        return d
    except Exception as exc:
        if strict:
            raise RuntimeError(f"cannot parse queue sidecar: {p}") from exc
        return _empty_queue()


def _bump_queue_revision(data: dict) -> None:
    data["revision"] = max(0, int(data.get("revision") or 0)) + 1


def _save_queue(sid: str, data: dict, *, bump: bool = True) -> bool:
    if sid in _DELETED_SESSION_IDS:
        data.clear()
        data.update(_empty_queue())
        return False
    canonical = dict(data)
    items = canonical.get("items")
    if isinstance(items, list):
        canonical["items"] = [
            _normalize_session_permission_fields(item)
            if isinstance(item, dict) else item
            for item in items
        ]
    inflight = canonical.get("inflight")
    if isinstance(inflight, dict) and isinstance(inflight.get("item"), dict):
        canonical["inflight"] = {
            "item": _normalize_session_permission_fields(inflight["item"]),
            "turn_id": str(inflight.get("turn_id") or ""),
            "claimed_at": int(inflight.get("claimed_at") or 0),
        }
    else:
        canonical["inflight"] = None
    if not canonical.get("items"):
        canonical["paused"] = False
    if bump:
        _bump_queue_revision(canonical)
    else:
        canonical["revision"] = max(0, int(canonical.get("revision") or 0))
    # Keep an empty revision tombstone. A late GET that started before the last
    # removal must not resurrect a consumed item in the browser mirror; the
    # monotonically increasing revision lets the frontend reject that response.
    atomic_write_text(
        _queue_path(sid),
        json.dumps(canonical, ensure_ascii=False),
        mode=0o600,
    )
    data.clear()
    data.update(canonical)
    return True


def get_queue(sid: str) -> dict:
    """Return the public queue snapshot; inflight is exposed separately."""
    with _QUEUE_LOCK:
        return _load_queue(sid)


def migrate_queue(source_sid: str, target_sid: str) -> dict:
    """Atomically move all not-yet-executing queue work to a runtime child.

    A rollover happens after the ordinary turn is complete, so a source claim
    should either be absent or still unbound.  Bound claims may already have
    side effects and therefore fail closed instead of being duplicated.  Item
    ids are retained and de-duplicated, making a retried migration idempotent.
    """
    if not source_sid or not target_sid or source_sid == target_sid:
        return {
            "migrated": 0,
            "source": get_queue(source_sid),
            "target": get_queue(target_sid),
        }
    with _QUEUE_LOCK:
        if source_sid in _DELETED_SESSION_IDS:
            raise ValueError("source session deleted")
        if target_sid in _DELETED_SESSION_IDS:
            raise ValueError("target session deleted")
        source = _load_queue(source_sid, strict=True)
        target = _load_queue(target_sid, strict=True)

        source_inflight = source.get("inflight") or {}
        inflight_item = source_inflight.get("item") or {}
        if inflight_item and str(source_inflight.get("turn_id") or ""):
            raise ValueError("source queue item is already executing")

        moving: list[dict] = []
        if inflight_item:
            moving.append(inflight_item)
        moving.extend(source.get("items") or [])
        if not moving:
            return {"migrated": 0, "source": source, "target": target}

        target_inflight = target.get("inflight") or {}
        active_count = len(target.get("items") or []) + int(bool(target_inflight))
        target_ids = {
            str(item.get("id") or "") for item in target.get("items") or []
        }
        target_inflight_id = str(
            ((target_inflight.get("item") or {}).get("id") or "")
        )
        if target_inflight_id:
            target_ids.add(target_inflight_id)
        unique_moving = [
            item for item in moving
            if str(item.get("id") or "") not in target_ids
        ]
        if active_count + len(unique_moving) > _QUEUE_MAX:
            raise ValueError("target queue is full")

        combined = [*(target.get("items") or []), *unique_moving]
        # Requests accepted before the rollover stay ahead of later child
        # requests even if a retry migrates them after the child has been used.
        combined.sort(key=lambda item: int(item.get("enqueued_at") or 0))
        target["items"] = combined
        target["paused"] = bool(target.get("paused") or source.get("paused"))
        source["items"] = []
        source["inflight"] = None
        source["paused"] = False
        _save_queue(target_sid, target)
        _save_queue(source_sid, source)
        return {
            "migrated": len(unique_moving),
            "source": source,
            "target": target,
        }


def list_queue_session_ids() -> list[str]:
    """Return session ids that currently have a persistent queue sidecar."""
    result = []
    for path in SESS_DIR.glob("*.queue.json"):
        name = path.name
        if name.endswith(".queue.json"):
            result.append(name[:-len(".queue.json")])
    return result


def _queue_session_not_found() -> dict:
    return {
        "ok": False,
        "error": "session_not_found",
        "queue": _empty_queue(),
    }


def _enqueue_message_locked(
    sid: str,
    text: str,
    image_ids: str,
    permission: str,
    display_text: str,
    selection_quotes: list[dict] | None,
    plan_return_permission: str | None,
) -> dict:
    """Append one item while the caller owns ``_QUEUE_LOCK``."""
    if sid in _DELETED_SESSION_IDS:
        return _queue_session_not_found()
    # A malformed sidecar can contain already-accepted work. Never coerce it
    # to an empty queue and overwrite it during an ordinary enqueue.
    data = _load_queue(sid, strict=True)
    active_count = len(data["items"]) + int(bool(data.get("inflight")))
    if active_count >= _QUEUE_MAX:
        return {"ok": False, "error": "queue_full", "queue": data}
    item = {
        "id": "q-" + uuid.uuid4().hex[:8],
        "text": text or "",
        "display_text": display_text or "",
        "selection_quotes": selection_quotes or [],
        "image_ids": image_ids or "",
        "permission": permission or "",
        "plan_return_permission": _normalize_plan_return_permission(
            permission,
            plan_return_permission,
        ),
        "enqueued_at": int(time.time() * 1000),
    }
    data["items"].append(item)
    _save_queue(sid, data)
    return {"ok": True, "item": item, "queue": data}


def enqueue_message(sid: str, text: str, image_ids: str = "",
                    permission: str = "",
                    display_text: str = "",
                    selection_quotes: list[dict] | None = None,
                    plan_return_permission: str | None = None,
                    *, require_session: bool = False,
                    existing_session: dict | None = None,
                    sdk_verified: bool = False) -> dict:
    """Append a message to the session's queue. Returns
    {'ok': bool, 'item'?: dict, 'queue': dict, 'error'?: str}. Rejects past
    _QUEUE_MAX (mirrors frontend cap).

    `permission` and `plan_return_permission` snapshot the sender's complete
    launch contract at enqueue time so the headless drain neither falls back
    to a server default nor reads a newer session selection."""

    with _QUEUE_LOCK:
        if require_session:
            # Match prune's lock order and hold both through the queue commit.
            # If prune wins, the missing index row rejects the enqueue; if this
            # path wins, prune observes the committed queue and preserves it.
            with _INDEX_LOCK:
                idx = _load_index()
                if not any(row.get("id") == sid for row in idx):
                    if (
                        not sdk_verified
                        or not existing_session
                        or existing_session.get("id") != sid
                    ):
                        return _queue_session_not_found()
                    # SDK-only sessions are first-class in MuseLab. Self-heal a
                    # minimal durable index row under the same locks as the
                    # queue commit so prune cannot remove it in between.
                    now = time.time()
                    candidate = {
                        key: existing_session.get(key)
                        for key in (
                            "id", "name", "model", "permission",
                            "plan_return_permission", "created_at", "updated_at",
                            "message_count", "turn_count", "auto_named", "effort",
                            "service_tier", "thinking", "activity_hidden",
                            "runtime_profile", "cwd", "pinned", "tag",
                            "forked_from", "forked_from_name",
                            "forked_from_message_id",
                            "runtime_shadow", "runtime_successor",
                            "runtime_predecessor",
                            "runtime_boundary_message_id",
                            "runtime_fork_boundary_at",
                        )
                        if existing_session.get(key) is not None
                    }
                    candidate.update({
                        "id": sid,
                        "name": str(candidate.get("name") or _default_session_name()),
                        "created_at": candidate.get("created_at") or now,
                        "updated_at": candidate.get("updated_at") or now,
                        "message_count": int(candidate.get("message_count") or 0),
                        "turn_count": int(candidate.get("turn_count") or 0),
                        "auto_named": bool(candidate.get("auto_named", True)),
                        "cwd": str(candidate.get("cwd") or ROOT),
                    })
                    idx.append(_normalize_session_permission_fields(candidate))
                    _save_index(idx)
                return _enqueue_message_locked(
                    sid,
                    text,
                    image_ids,
                    permission,
                    display_text,
                    selection_quotes,
                    plan_return_permission,
                )
        return _enqueue_message_locked(
            sid,
            text,
            image_ids,
            permission,
            display_text,
            selection_quotes,
            plan_return_permission,
        )


def enqueue_existing_message(
    sid: str,
    text: str,
    image_ids: str = "",
    permission: str = "",
    display_text: str = "",
    selection_quotes: list[dict] | None = None,
    plan_return_permission: str | None = None,
) -> dict:
    """Atomically resolve an existing session and append one queued message.

    Indexed sessions take the queue -> index fast path: one index read and no
    lifecycle stripe.  DELETE and prune use the same lock order, so the enqueue
    still linearizes wholly before removal (and is removed with it) or wholly
    after the deletion tombstone (and is rejected).

    Only an index miss takes the slower lifecycle-owned SDK probe.  That lock
    spans SDK-only discovery and the local queue commit, so an old SDK snapshot
    can never recreate a session after deletion has already won.
    """
    # Nearly every browser enqueue targets an already-indexed MuseLab session.
    # Resolve and commit it in one queue/index transaction instead of taking a
    # lifecycle stripe, reading index.json once in get_session_for_queue(),
    # then reading it again in enqueue_message(require_session=True).
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            return _queue_session_not_found()
        with _INDEX_LOCK:
            indexed = next(
                (
                    dict(row)
                    for row in _load_index()
                    if isinstance(row, dict) and row.get("id") == sid
                ),
                None,
            )
            if indexed is not None:
                current = _normalize_session_permission_fields(
                    {**indexed, "cwd": str(indexed.get("cwd") or ROOT)}
                )
                if permission == "plan" and plan_return_permission is None:
                    plan_return_permission = (
                        current.get("plan_return_permission")
                        if current.get("permission") == "plan"
                        else current.get("permission")
                    )
                return _enqueue_message_locked(
                    sid,
                    text,
                    image_ids,
                    permission,
                    display_text,
                    selection_quotes,
                    plan_return_permission,
                )

    # An SDK-only session needs a potentially slow workspace probe and a
    # durable index self-heal.  Keep the existing lifecycle transaction for
    # this exceptional path; explicit DELETE uses the same stripe.
    with session_lifecycle_lock(sid):
        current, sdk_verified = get_session_for_queue(sid)
        if current is None:
            return _queue_session_not_found()
        if permission == "plan" and plan_return_permission is None:
            plan_return_permission = (
                current.get("plan_return_permission")
                if current.get("permission") == "plan"
                else current.get("permission")
            )
        return enqueue_message(
            sid,
            text,
            image_ids,
            permission=permission,
            display_text=display_text,
            selection_quotes=selection_quotes,
            plan_return_permission=plan_return_permission,
            require_session=True,
            existing_session=current,
            sdk_verified=sdk_verified,
        )


def claim_queue_message(sid: str) -> dict | None:
    """Atomically move the FIFO head into a durable inflight slot.

    The item remains persisted until its actual assistant turn finishes and
    calls ``ack_queue_message``. A process restart restores an unbound claim
    to the queue head; a bound claim is reconciled by the owning turn id.
    """
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            return None
        data = _load_queue(sid, strict=True)
        if data.get("inflight") or data.get("paused") or not data["items"]:
            return None
        item = data["items"].pop(0)
        data["inflight"] = {
            "item": item,
            "turn_id": "",
            "claimed_at": int(time.time() * 1000),
        }
        _save_queue(sid, data)
        return item


def bind_queue_turn(sid: str, item_id: str, turn_id: str) -> dict:
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            raise ValueError("session deleted")
        data = _load_queue(sid, strict=True)
        inflight = data.get("inflight") or {}
        item = inflight.get("item") or {}
        if str(item.get("id") or "") != str(item_id or ""):
            raise ValueError("queue inflight item changed")
        current_turn = str(inflight.get("turn_id") or "")
        if current_turn and current_turn != str(turn_id or ""):
            raise ValueError("queue inflight turn changed")
        inflight["turn_id"] = str(turn_id or "")
        data["inflight"] = inflight
        _save_queue(sid, data)
        return data


def ack_queue_message(sid: str, item_id: str, turn_id: str) -> bool:
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            return False
        data = _load_queue(sid, strict=True)
        inflight = data.get("inflight") or {}
        item = inflight.get("item") or {}
        if (str(item.get("id") or "") != str(item_id or "")
                or str(inflight.get("turn_id") or "") != str(turn_id or "")):
            return False
        data["inflight"] = None
        _save_queue(sid, data)
        return True


def release_queue_claim(
    sid: str,
    item_id: str,
    *,
    turn_id: str = "",
    pause: bool = False,
) -> bool:
    """Return an uncompleted inflight item to the FIFO head exactly once."""
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            return False
        data = _load_queue(sid, strict=True)
        inflight = data.get("inflight") or {}
        item = inflight.get("item") or {}
        if str(item.get("id") or "") != str(item_id or ""):
            return False
        bound_turn = str(inflight.get("turn_id") or "")
        if bound_turn != str(turn_id or ""):
            return False
        data["inflight"] = None
        if not any(str(row.get("id") or "") == str(item_id)
                   for row in data["items"]):
            data["items"].insert(0, item)
        data["paused"] = bool(pause) and bool(data["items"])
        _save_queue(sid, data)
        return True


def recover_queue_inflight(sid: str) -> dict:
    """Reconcile and conservatively pause queued work after process restart.

    A bound claim may already have performed external side effects.  An
    unbound claim is safe from duplicate execution, but the process restart
    still erased the preceding turn's in-memory terminal truth and all staged
    attachments.  Restore either claim exactly once, then pause every surviving
    item so only an explicit user review/resume can execute it.
    """
    with _QUEUE_LOCK:
        if sid in _DELETED_SESSION_IDS:
            return _empty_queue()
        data = _load_queue(sid, strict=True)
        inflight = data.get("inflight") or {}
        item = inflight.get("item") or {}
        changed = False
        if item:
            item_id = str(item.get("id") or "")
            data["inflight"] = None
            if item_id and not any(str(row.get("id") or "") == item_id
                                   for row in data["items"]):
                data["items"].insert(0, item)
            changed = True
        should_pause = bool(data["items"])
        if bool(data.get("paused")) != should_pause:
            data["paused"] = should_pause
            changed = True
        if changed:
            _save_queue(sid, data)
        return data


# Backward-compatible helper names for callers/tests outside the drain path.
def dequeue_message(sid: str) -> dict | None:
    return claim_queue_message(sid)


def requeue_head(sid: str, item: dict) -> dict:
    item_id = str(item.get("id") or "")
    release_queue_claim(sid, item_id)
    with _QUEUE_LOCK:
        data = _load_queue(sid, strict=True)
        if not any(str(row.get("id") or "") == item_id
                   for row in data["items"]):
            data["items"].insert(0, _normalize_session_permission_fields(item))
            _save_queue(sid, data)
        return data


def remove_queue_item(sid: str, item_id: str) -> dict:
    """Remove one item by id. Returns the updated queue snapshot.

    Emptying the queue also clears ``paused``. The flag means "there is queued
    work that stopped auto-draining because a turn errored" — with no items
    left it has no referent, and leaving it set is a silent trap: the next
    enqueue lands in a queue whose ``dequeue_message`` returns None forever, so
    the message sits there through every subsequent completed turn with no
    banner and no error. Observed 2026-07-25: a 30-min-cap abort paused a queue
    of 2, the user deleted both stale items, enqueued 2 fresh ones, and they
    never sent. It also unblocks _save_queue's empty-file cleanup, which skips
    deletion while ``paused`` is true — sessions/ had zombie
    ``{items: [], paused: true}`` files dating back a week."""
    with _QUEUE_LOCK:
        data = _load_queue(sid, strict=True)
        data["items"] = [it for it in data["items"] if it.get("id") != item_id]
        inflight = data.get("inflight") or {}
        if str((inflight.get("item") or {}).get("id") or "") == item_id:
            raise ValueError("queue item is currently executing")
        if not data["items"]:
            data["paused"] = False
        _save_queue(sid, data)
        return data


def clear_queue(sid: str) -> dict:
    """Drop waiting items without stealing ownership from a running turn."""
    with _QUEUE_LOCK:
        current = _load_queue(sid, strict=True)
        current["items"] = []
        current["paused"] = False
        _save_queue(sid, current)
        return current


def set_queue_paused(sid: str, paused: bool) -> dict:
    """Set the paused flag. Returns the updated queue snapshot. Resuming
    (paused=False) does NOT itself drain — the caller kicks the drain."""
    with _QUEUE_LOCK:
        data = _load_queue(sid, strict=True)
        data["paused"] = bool(paused) and bool(data["items"])
        _save_queue(sid, data)
        return data


def pause_queue_if_nonempty(sid: str) -> dict:
    """Atomically pause ``sid`` only when queued work still exists.

    The interrupt path uses this before asking the SDK to stop. Sharing the
    queue lock with ``dequeue_message`` closes the race where turn completion
    could otherwise pop the next item between a separate get/pause pair.
    """
    with _QUEUE_LOCK:
        data = _load_queue(sid, strict=True)
        if data["items"] and not data.get("paused"):
            data["paused"] = True
            _save_queue(sid, data)
        return data


def reorder_queue(sid: str, order: list[str]) -> dict:
    """Reorder items to match `order` (list of item ids). Ids not present in
    `order` are appended in their existing relative order (defensive)."""
    with _QUEUE_LOCK:
        data = _load_queue(sid, strict=True)
        by_id = {it["id"]: it for it in data["items"]}
        new = [by_id[i] for i in order if i in by_id]
        for it in data["items"]:
            if it["id"] not in order:
                new.append(it)
        data["items"] = new
        _save_queue(sid, data)
        return data
