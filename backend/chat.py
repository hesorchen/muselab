import os
import base64
import json
import asyncio
import re
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Response
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ThinkingBlock, ResultMessage,
    ToolUseBlock, ToolResultBlock, StreamEvent,
    get_session_messages,
    delete_session as sdk_delete_session,
    rename_session as sdk_rename_session,
    tag_session as sdk_tag_session,
    fork_session as sdk_fork_session,
)
from .auth import require_token_query, require_token
from .settings import ROOT, MODEL, MCP_CONFIG_PATH, atomic_write_text
from . import sessions as sess
from . import endpoints
from .ask_user_question import (
    build_server_for_session, register_session_queue,
    unregister_session_queue, submit_answer,
)
from . import permission_request as perm

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Clients keyed by (session_id, model) — model is part of the key so that
# switching model mid-session creates a fresh client for that model (which
# uses resume=session_id to inherit the conversation history from disk).
_clients: dict[tuple[str, str], ClaudeSDKClient] = {}
# Tracks the permission_mode currently active on each cached client. SDK
# doesn't expose a getter, so we shadow what we asked for. Lets a cached
# client whose mode no longer matches the request swap via
# client.set_permission_mode() instead of needing a full rebuild.
_client_permission: dict[tuple[str, str], str] = {}


class TurnBroadcast:
    """Fan-out for an in-flight assistant turn.

    Why: the SSE event_gen used to be the sole consumer of SDK output
    via merge_q; when the browser closed, the generator unwound and
    cancelled pump_claude, killing the in-progress reply.

    Now event_gen runs as a detached background task that PUBLISHES
    every SSE event it would have yielded to this broadcast. The HTTP
    endpoint is just a SUBSCRIBER — it replays the existing buffer +
    streams new events. A reconnecting browser becomes a new subscriber
    and gets the full reply via replay + live tail, with no extra logic
    on the SDK side. Up to 30 min per turn (asyncio.wait_for at the
    background-task level). Removed from `_active_turns` when finished.
    """
    def __init__(self, session_id: str, model: str = ""):
        self.session_id = session_id
        self.model = model
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.started_at = time.time()
        # User-side context for this turn — populated when the SSE
        # endpoint kicks off a new turn. Needed because SDK CLI only
        # flushes the session JSONL at turn completion; mid-turn reloads
        # would see an empty session unless we reconstruct the message
        # list from broadcast state. The user message itself isn't in
        # `events` (those are server→client SSE events; user prompt is
        # a separate input channel) so we keep it on the broadcast.
        self.user_text: str = ""
        self.user_images: list[dict] = []
        self.user_docs: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)   # sentinel — subscribers stop yielding
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


# In-flight turns by session id. Lookup target for reconnect.
_active_turns: dict[str, TurnBroadcast] = {}
# LRU bookkeeping. Each CLI subprocess holds ~30-50 MB RSS; without a cap
# muselab leaks memory as users open more sessions. New clients append to
# the tail; on cache miss with len > cap, oldest gets disconnected.
_client_lru: list[tuple[str, str, str]] = []   # (session_id, model, effort)
_CLIENT_POOL_CAP = int(os.environ.get("MUSELAB_CLIENT_POOL_CAP", "3"))
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# In-flight turn persistence (survives muselab process restart)
# ---------------------------------------------------------------------------
# Why: `_active_turns` is in-memory only. If muselab restarts mid-turn
# (systemd OOM-kill / manual restart / crash / `systemctl --user restart`),
# the user's prompt is lost and they may not even realize the turn never
# replied. We write a tiny sidecar JSON to disk per in-flight turn, delete
# it on clean completion, and on process startup scan for orphans to tell
# the frontend "you had N unfinished turns last session."
#
# Design choices:
# - Sidecar lives under `sessions/active_turns/<sid>.json`, not `~/.muselab/`,
#   because SESS_DIR already exists, is gitignored, and is the natural sibling
#   for per-session state.
# - We do NOT auto-resume. Auto-resume would burn tokens on conversations the
#   user has already abandoned and bypass their "should I rephrase?" judgment.
#   Frontend gets the list + sids and toasts the user — they decide.
# - File presence == status "in_flight". Don't bother with a status field;
#   deletion is the only terminal action.
# - No periodic touch / last_event_ts. Adding background touch task per turn
#   means N file writes per second across active turns — not worth the
#   complexity for "stale by 30s vs 30min" UX granularity. `started_at` is
#   enough to show "5 min ago" in the toast.

_ACTIVE_TURN_DIR = sess.SESS_DIR / "active_turns"
_ACTIVE_TURN_DIR.mkdir(exist_ok=True)


def _active_turn_path(sid: str) -> Path:
    return _ACTIVE_TURN_DIR / f"{sid}.json"


def _write_active_turn_sidecar(bc: TurnBroadcast) -> None:
    """Persist the in-flight turn so a restart can surface it to the UI.
    Best-effort: a failure here must NOT abort the turn (we'd rather run
    the user's prompt without a recovery breadcrumb than refuse to run)."""
    try:
        raw = bc.user_text or ""
        first_line = raw.strip().splitlines()[0] if raw.strip() else ""
        preview = first_line if len(first_line) <= 200 else first_line[:199] + "…"
        atomic_write_text(
            _active_turn_path(bc.session_id),
            json.dumps({
                "sid": bc.session_id,
                "user_text": raw,
                "user_text_preview": preview,
                "model": bc.model,
                "started_at": bc.started_at,
            }, ensure_ascii=False),
        )
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(
            f"[chat] failed to write active-turn sidecar sid={bc.session_id}: "
            f"{type(e).__name__}: {e}\n")
        _sys.stderr.flush()


def _delete_active_turn_sidecar(sid: str) -> None:
    """Called on clean turn termination (success / error / timeout). The
    only case where we leave it on disk is when the process dies before
    reaching this — exactly the case we want startup scan to catch."""
    try:
        p = _active_turn_path(sid)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _scan_interrupted_turns_at_startup() -> dict[str, dict]:
    """Read all sidecars left over from a previous process. Runs once at
    module import. Keeps the files on disk until the user dismisses each
    one — that way two browsers can both see the notification, and a
    second muselab restart still surfaces undismissed entries."""
    out: dict[str, dict] = {}
    if not _ACTIVE_TURN_DIR.exists():
        return out
    for p in _ACTIVE_TURN_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            sid = data.get("sid") or p.stem
            out[sid] = data
        except Exception as e:
            import sys as _sys
            _sys.stderr.write(
                f"[chat] skipping malformed active-turn sidecar {p.name}: "
                f"{type(e).__name__}: {e}\n")
    return out


# Snapshot taken once at process startup. Endpoints serve from this dict;
# starting a new turn for an sid here auto-dismisses (the new turn supersedes
# the old in-flight). Don't re-scan disk on each request — once consumed by a
# browser dismiss, the user has acknowledged.
_interrupted_at_startup: dict[str, dict] = _scan_interrupted_turns_at_startup()


async def _evict_lru_if_needed():
    """If pool overflows, disconnect the oldest non-active client.
    Caller must hold _lock OR call this when no active stream depends on
    the evicted client (callers do — get_client adds AFTER any current
    stream finishes acquiring its handle)."""
    while len(_client_lru) > _CLIENT_POOL_CAP:
        old_key = _client_lru.pop(0)
        c = _clients.pop(old_key, None)
        _client_permission.pop(old_key, None)
        if c is None:
            continue
        try:
            await c.disconnect()
        except Exception as e:
            import sys as _sys
            _sys.stderr.write(
                f"[client-pool] evict {old_key} disconnect err: {e}\n")

# Global aggregate stats (across all sessions). cache_read / cache_creation
# come from the Anthropic prompt cache — high cache_read ratio means subsequent
# turns are much cheaper. Surfacing this in the UI lets the user see the value
# of long sessions vs constantly opening new ones.
_stats = {"total_cost_usd": 0.0, "total_messages": 0,
          "total_input_tokens": 0, "total_output_tokens": 0,
          "total_cache_read_tokens": 0, "total_cache_creation_tokens": 0}

# Per-session current state — populated from the LATEST ResultMessage of each
# session. The model's `input_tokens` on a turn ≈ current context window size,
# so tracking the most-recent value gives a meaningful "context meter".
_session_usage: dict[str, dict] = {}     # sid -> {input_tokens, output_tokens,
                                          #         cache_read_tokens,
                                          #         cache_creation_tokens,
                                          #         total_cost_usd, last_turn_at}

# Per-model context windows. Used as the meter's denominator when a SDK
# get_context_usage() truth isn't available (first turn of a session, or
# any third-party model where CLI's tokenizer / window inference is
# unreliable). Numbers verified 2026-05-18 from each vendor's docs:
#   - Anthropic:   tygartmedia.com / anthropic.com (Opus/Sonnet 4.6+ default
#                  to 1M on Pro/Max/Enterprise; Haiku 4.5 stays 200K)
#   - DeepSeek V4: api-docs.deepseek.com (V4 series ships 1M native context)
#   - Zhipu GLM:   glm-5.org / docs.z.ai (GLM-5 + GLM-4.7 both 200K context)
#   - MiniMax:     platform.minimax.io (M2.5 / M2.7 both 204_800, cline #10007
#                  PR fixed the prior 192K/245K misinformation)
MODEL_CONTEXT_LIMITS = {
    # Anthropic — 1M on Pro/Max plans (auto-upgrade, no flag needed). Haiku
    # stayed at the older 200K since it's the speed/cost tier.
    "claude-opus-4-7":            1_000_000,
    "claude-sonnet-4-6":          1_000_000,
    "claude-haiku-4-5-20251001":    200_000,
    # DeepSeek V4 series — 1M native, all SKUs.
    "deepseek-v4-pro":            1_000_000,
    "deepseek-v4-flash":          1_000_000,
    # DeepSeek V3 chat/reasoner SKUs — older 128K window kept.
    "deepseek-chat":                128_000,
    "deepseek-reasoner":            128_000,
    # Zhipu GLM 5 series — 200K context, 128K output cap.
    "glm-5":                        200_000,
    "glm-5-air":                    200_000,
    "glm-4.7":                      200_000,
    "glm-4-plus":                   128_000,   # older 4-plus stayed 128K
    # MiniMax — 204_800 exactly, per platform.minimax.io spec.
    "minimax-m2.7":                 204_800,
    "minimax-m2.7-highspeed":       204_800,
    "minimax-m2.5":                 204_800,
}
DEFAULT_CONTEXT_LIMIT = 128_000

# Soft budget. If set (via MUSELAB_BUDGET_USD env or PUT /api/settings),
# usage endpoint flags overrun so the UI can color the cost badge red.
def _is_real_user_prompt(sm: Any) -> bool:
    """True if ``sm`` is a user message the human actually typed.

    SDK 0.2.82's get_session_messages doesn't really filter tool-use
    sidechain frames — every wrapped tool_result still comes back as
    ``type="user"`` with ``parent_tool_use_id=None``, contrary to the
    docstring. So we discriminate by content shape: real user prompts
    contain text (string content, or a list with at least one non-
    tool_result block); pure-tool_result frames are sidechain echoes
    and don't count as a turn.

    Without this filter a session with 45 prompts + heavy agent tool
    use shows up as 300+ turns in the picker.
    """
    if sm is None or getattr(sm, "type", None) != "user":
        return False
    if getattr(sm, "parent_tool_use_id", None):
        return False
    msg = getattr(sm, "message", None)
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        # If any block is non-tool_result (text / image / etc.) → real prompt.
        for b in content:
            if isinstance(b, dict) and b.get("type") != "tool_result":
                return True
        return False
    # Unknown shape — default to "real" so we don't under-count.
    return True


def _budget_usd() -> float:
    try:
        return float(os.environ.get("MUSELAB_BUDGET_USD", "0") or 0)
    except ValueError:
        return 0.0


_MEMORY_DIR_PATH = f"~/.claude/projects/{str(ROOT).replace('/', '-')}/memory/"

SYSTEM_PROMPT = f"""\
You are Muse, a personal assistant running inside muselab — a self-hosted AI
workspace on the user's own machine. The user's files live at the archive root
{ROOT} (path varies per install). You can browse and edit anything under that
root via the available tools.

# Who Muse is
- One assistant, not split personalities. You hold the user's information
  across whichever life dimensions they've put in the archive
  (health / work / money / people / notes / …) and reason across them.
- The user may have written a CLAUDE.md (at the archive root or in
  ~/.claude/) describing who they are, what they care about, and how
  they want you to respond. Treat it as ground truth about *them*.

# Defaults
- Be concise. Lead with the conclusion, then the supporting detail.
- Reply in the same language as the user's last message.
- Tables and bullet lists beat long paragraphs for comparing options.
  Code blocks for code, with the language tag.
- No "As an AI assistant…", no "I'd be happy to…", no apologizing for
  things you didn't do. Skip the preamble, answer the question.

# Tools
- Read / Grep / Glob / Bash to explore the archive freely before
  answering. Don't guess file contents — read them.
- Edit / Write for changes. For non-trivial edits, show the diff intent
  before touching the file.
- `mcp__muselab__ask_user_question`: use this when you need the user to
  pick from 2–4 mutually exclusive options. The UI renders clickable
  buttons — faster than asking in plain text. NOT for open-ended
  questions; for those, ask in plain text.

# Memory (cross-conversation long-term memory)
Claude Code keeps a file-based memory at `{_MEMORY_DIR_PATH}`.
`MEMORY.md` in that dir is the index; its first 200 lines (or 25KB)
load automatically at session start.

When you learn something that should survive across conversations —
a stable user preference, a personal fact, a behavior correction, an
ongoing-project context — save a memory file via Write / Edit, then
add a one-line entry to `MEMORY.md`.

Naming conventions (mirror what's already there if the dir is
non-empty):
- `user_*.md` — identity, persistent facts about the user
- `feedback_*.md` — behavior rules the user has corrected you on
- `project_*.md` — context for an ongoing initiative
- `reference_*.md` — pointers to authoritative files in the archive

Don't memorize:
- Trivial facts that change daily
- Things already in archive files (just reference them with a
  `reference_*.md` pointer)
- Anything the user asked you NOT to remember

When something changes, update the existing entry — don't duplicate.
When in doubt, ask the user "should I remember this?" before writing.

# When the user has a CLAUDE.md
That document is the user's own rules for how you should behave around
them. Follow it. If it conflicts with anything above, the user's
CLAUDE.md wins — they wrote it on purpose.
"""


async def get_client(session_id: str, model: str, permission: str = "bypassPermissions",
                     effort: str = "") -> ClaudeSDKClient:
    """Create or fetch a ClaudeSDKClient for a (session, model, effort) triple.
    Switching model OR effort in the UI yields a fresh client; resume=session_id
    loads the same on-disk conversation history into the new client.

    effort: "" (SDK adaptive) / "low" / "medium" / "high" / "xhigh" / "max".
    Anything else is ignored — invalid values fall back to the SDK default."""
    key = (session_id, model, effort)
    async with _lock:
        if key not in _clients:
            # Use session's custom system prompt if set, else fall back to muselab default.
            sess_data = sess.get_session(session_id) or {}
            custom_sp = (sess_data.get("system_prompt") or "").strip()
            sp = f"{custom_sp}\n\n---\n\n{SYSTEM_PROMPT}" if custom_sp else SYSTEM_PROMPT
            # New CLI rule: session_id + resume/continue conflict unless fork_session
            # is set. So we use resume alone — it both loads existing state AND
            # implies the session id. Falls back to session_id-only for new sessions.
            # SDK default max_buffer_size is 1 MB. A single tool_use JSON message
            # (Edit on a large file, or Read of a long file) can blow past that
            # and kill the message reader silently — the chat then "hangs forever"
            # because no more chunks arrive. Bump to 32 MB; configurable via env.
            max_buf = int(os.environ.get("MUSELAB_MAX_BUFFER_SIZE", str(32 * 1024 * 1024)))
            # Critical SDK option distinction:
            #   `session_id=X`  → force a NEW session to use UUID X (fails if
            #                     CLI already has a JSONL for X)
            #   `resume=X`      → resume an EXISTING session by UUID X
            # If we always use `resume` for un-streamed sessions, CLI generates
            # a fresh UUID and orphans ours. If we always use `session_id`,
            # any session that's ever streamed errors with "already in use".
            # Detect JSONL existence by RECURSIVELY scanning the CLI's projects
            # root — SDK's _find_project_dir relies on path-hash matching that
            # was unreliable on Windows in practice (user's CLI saw the JSONL
            # but the SDK helper didn't).
            jsonl_exists = False
            try:
                projects_root = Path.home() / ".claude" / "projects"
                if projects_root.exists():
                    # Glob is faster than recursive walk and matches any sub-
                    # project dir layout. Fine for muselab's session counts.
                    for hit in projects_root.glob(f"*/{session_id}.jsonl"):
                        if hit.is_file():
                            jsonl_exists = True
                            break
            except Exception as e:
                import sys as _sys
                _sys.stderr.write(f"[muselab] jsonl_exists check failed for {session_id}: {e}\n")
            # CLI stderr capture — without this, ProcessError just says
            # "Check stderr output for details" with no actual details and
            # we can't tell whether the CLI rejected --session-id, hit an
            # auth error, or something else. Pipe every line into muselab's
            # stderr.log so the next failure is debuggable.
            import sys as _sys
            def _cli_stderr(line: str) -> None:
                _sys.stderr.write(f"[cli-stderr sid={session_id[:8]}] {line}\n")
                _sys.stderr.flush()

            opts_kwargs = dict(
                cwd=str(ROOT),
                model=model,
                permission_mode=permission,
                system_prompt=sp,
                max_buffer_size=max_buf,
                stderr=_cli_stderr,
                # Block harness-only tools the SDK exposes by default. AskUserQuestion
                # is intentionally NOT blocked: we re-implement it via in-process MCP
                # (mcp__muselab__ask_user_question) — see backend/ask_user_question.py.
                # The system prompt tells the model to use that name. The built-in
                # version is left enabled too as a fallback if the model forgets the
                # MCP name; the frontend renders both shapes.
                disallowed_tools=[
                    "ExitPlanMode",           # plan-mode handshake — no UI yet
                    "ScheduleWakeup",         # /loop dynamic mode — Claude Code only
                    "CronCreate", "CronDelete", "CronList",
                    "EnterPlanMode", "EnterWorktree", "ExitWorktree",
                    "Monitor", "PushNotification", "RemoteTrigger",
                    "ShareOnboardingGuide",
                ],
                # Load CLAUDE.md from user (~/.claude/CLAUDE.md), project
                # (cwd/CLAUDE.md → the user's archive), and local (.claude/
                # within cwd). Also enables skill discovery from the same scopes.
                setting_sources=["user", "project", "local"],
                # Bind THIS session to muselab's chosen UUID — either as a new
                # session (session_id=) or by resuming the existing one (resume=).
                **({"resume": session_id} if jsonl_exists else {"session_id": session_id}),
                # Token-level streaming: SDK emits StreamEvent for each delta
                # the model produces (text / thinking). Without this, we only
                # see full blocks at the end → user waits for the whole reply
                # before seeing anything. With this, each token shows up.
                include_partial_messages=True,
            )
            # Skills get attached to the system prompt as JSON tool defs.
            # Claude handles a large skill catalog fine; third-party vendors
            # (DeepSeek / GLM / MiniMax) often time out or 400 on the bigger
            # payload. So default to skills only on Claude; opt-out via env.
            is_third_party = endpoints.is_third_party(model)
            skills_off = os.environ.get("MUSELAB_DISABLE_SKILLS", "").lower() in ("1", "true", "yes")
            if not is_third_party and not skills_off:
                opts_kwargs["skills"] = "all"
            # Optional model params from env (UI-editable via /api/settings).
            mt = int(os.environ.get("MUSELAB_MAX_TURNS", "0") or 0)
            if mt > 0:
                opts_kwargs["max_turns"] = mt
            # For non-Claude models, point the SDK at the vendor's own
            # Anthropic-compatible endpoint (DeepSeek / GLM / MiniMax).
            # This way the SDK's full agent loop (tools, MCP, skills, CLAUDE.md)
            # works uniformly across providers — no router process needed.
            # Claude models still go direct so Pro OAuth keeps working.
            env_ovr = endpoints.env_override(model)
            if env_ovr is not None:
                opts_kwargs["env"] = env_ovr
            else:
                # No env_override == this is Claude (or unknown model). CLI needs
                # ONE of: ~/.claude/.credentials.json (Pro OAuth), ANTHROPIC_API_KEY
                # in env, or ANTHROPIC_AUTH_TOKEN. If none of those are present, CLI
                # exits 1 with "Not logged in" BEFORE producing any stderr — leaving
                # only a useless ProcessError. Pre-check and raise a clean message
                # so the UI can surface "请先配置 Anthropic API key 或运行 claude login"
                # instead of a generic stream-failure.
                from claude_agent_sdk._errors import ClaudeSDKError as _SDKErr
                cred_file = Path.home() / ".claude" / ".credentials.json"
                if not cred_file.exists() and not os.environ.get("ANTHROPIC_API_KEY") \
                        and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                    raise _SDKErr(
                        f"Claude model '{model}' requires auth: either run "
                        f"`claude login` (Pro/Max) or set ANTHROPIC_API_KEY in "
                        f"Settings. CLI would exit 1 silently otherwise."
                    )
                # Capture CLI stderr so vendor 401 / network errors surface
                # in /tmp/muselab-restart.log instead of vanishing silently.
                import sys as _sys
                def _stderr_logger(line: str) -> None:
                    _sys.stderr.write(f"[SDK-CLI:{model}] {line}\n")
                    _sys.stderr.flush()
                opts_kwargs["stderr"] = _stderr_logger
            # MCP servers: always register the in-process muselab server (for
            # ask_user_question). If user has mcp.json with external servers, merge.
            mcp_dict: dict = {"muselab": build_server_for_session(session_id)}
            if MCP_CONFIG_PATH.exists():
                try:
                    cfg = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
                    for name, spec in (cfg.get("mcpServers") or {}).items():
                        # Skip explicitly disabled servers (UI toggle).
                        if isinstance(spec, dict) and spec.get("disabled"):
                            continue
                        # Strip our muselab-local `disabled` key before handing to SDK.
                        clean = {k: v for k, v in spec.items() if k != "disabled"} \
                                if isinstance(spec, dict) else spec
                        mcp_dict[name] = clean
                except Exception:
                    pass  # fall through with just muselab
            opts_kwargs["mcp_servers"] = mcp_dict
            # Always enable extended thinking. Without an explicit config the
            # model uses adaptive thinking and silently pauses mid-reply for
            # invisible reasoning — user perceives "few chars + stall + dump".
            # With ThinkingConfigEnabled the reasoning is streamed visibly via
            # thinking_delta events, which the FE renders as a collapsible
            # block. (Old `show_thinking` toggle removed 2026-05-20 — was
            # always-on for years; nobody passes it any more.)
            from claude_agent_sdk import ThinkingConfigEnabled
            budget = int(os.environ.get("MUSELAB_THINKING_BUDGET", "4000") or 4000)
            # ThinkingConfigEnabled is a TypedDict — instantiating without
            # `type` produces a dict missing that key, and the SDK then raises
            # KeyError('type') in subprocess_cli.py:376. Set it explicitly.
            opts_kwargs["thinking"] = ThinkingConfigEnabled(
                type="enabled", budget_tokens=budget)
            # Effort knob (SDK 0.2.82+). Anthropic Opus 4.7's adaptive thinking
            # picks an effort automatically; this override lets users force a
            # deeper budget on specific tabs (e.g. xhigh for research). SDK
            # rejects unknown strings, so guard the literal set.
            _VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")
            if effort and effort in _VALID_EFFORT:
                opts_kwargs["effort"] = effort
            # When permission_mode is not bypassPermissions, wire the SDK's
            # can_use_tool callback to muselab's permission_request UI bridge.
            # NOTE: tried wiring this unconditionally for SDK-native
            # AskUserQuestion routing — SDK 0.2.82's bundled CLI does not yet
            # surface the trained-in tool through can_use_tool, and the
            # required PreToolUse keepalive hook added an extra control-channel
            # round-trip per token that buffered streaming output ("first char
            # then long pause then a flood" symptom). AskUserQuestion still
            # works via the MCP route (mcp__muselab__ask_user_question), which
            # the system prompt above guides the model toward.
            if permission != "bypassPermissions":
                opts_kwargs["can_use_tool"] = perm.build_callback_for_session(session_id)
            try:
                client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts_kwargs))
                await client.connect()
            except Exception as e:
                # Two failure modes we recover from by swapping session_id ⇔ resume:
                #   - tried `resume=` but CLI has no on-disk session for it
                #     → swap to `session_id=` (create fresh tied to our UUID)
                #   - tried `session_id=` but CLI reports "already in use"
                #     (its internal lock leaked, or a JSONL appeared between
                #     our glob check and the spawn) → swap to `resume=`
                err_text = str(e).lower()
                used_session_id = "session_id" in opts_kwargs
                if used_session_id and "already in use" in err_text:
                    opts_kwargs.pop("session_id", None)
                    opts_kwargs["resume"] = session_id
                else:
                    opts_kwargs.pop("resume", None)
                    opts_kwargs["session_id"] = session_id
                # The fallback can ALSO hit "already in use" — happens when
                # a prior CLI subprocess for the same sid is still flushing
                # its session JSONL after a vendor switch (GLM → MiniMax).
                # SDK transport.close() waits up to 5s for graceful exit, but
                # the OS-level file lock can persist a bit longer. Retry with
                # exponential backoff (up to ~3s total) before surfacing the
                # error to the user.
                last_err: Exception | None = None
                for attempt in range(4):
                    try:
                        client = ClaudeSDKClient(
                            options=ClaudeAgentOptions(**opts_kwargs))
                        await client.connect()
                        last_err = None
                        if attempt > 0:
                            import sys as _sys
                            _sys.stderr.write(
                                f"[chat] sid={session_id[:8]} connect retry "
                                f"succeeded on attempt {attempt + 1}\n")
                            _sys.stderr.flush()
                        break
                    except Exception as e2:
                        last_err = e2
                        if "already in use" not in str(e2).lower():
                            raise
                        # Backoff: 200ms, 400ms, 800ms, 1600ms (~3s total).
                        import sys as _sys
                        _sys.stderr.write(
                            f"[chat] sid={session_id[:8]} attempt {attempt + 1} "
                            f"hit 'already in use', backing off "
                            f"{200 * (2 ** attempt)}ms\n")
                        _sys.stderr.flush()
                        await asyncio.sleep(0.2 * (2 ** attempt))
                if last_err is not None:
                    raise last_err
            _clients[key] = client
            _client_permission[key] = permission
            _client_lru.append(key)
            await _evict_lru_if_needed()
        else:
            # Touch LRU — most-recently-used moves to the tail.
            if key in _client_lru:
                _client_lru.remove(key)
            _client_lru.append(key)
        # Cached client may have been created with a different permission_mode
        # (the cache key is (sid, model), not (sid, model, permission)). Sync
        # to the requested mode via SDK's set_permission_mode rather than
        # rebuilding — saves a CLI subprocess restart.
        current_perm = _client_permission.get(key)
        if current_perm != permission:
            try:
                await _clients[key].set_permission_mode(permission)
                _client_permission[key] = permission
            except Exception as e:
                import sys as _sys
                _sys.stderr.write(
                    f"[chat] set_permission_mode {current_perm}→{permission} "
                    f"failed for {key}: {type(e).__name__}: {e}\n")
        return _clients[key]


async def disconnect_client(session_id: str) -> None:
    """Disconnect every cached client for this session (across all models)."""
    async with _lock:
        keys = [k for k in _clients if k[0] == session_id]
        for k in keys:
            c = _clients.pop(k, None)
            _client_permission.pop(k, None)
            if k in _client_lru:
                _client_lru.remove(k)
            if c is not None:
                try:
                    await c.disconnect()
                except Exception:
                    pass


# ====== sessions REST ======

class CreateReq(BaseModel):
    name: str | None = None
    model: str | None = None


@router.get("/sessions", dependencies=[Depends(require_token)])
def list_sessions_api() -> dict:
    return {"sessions": sess.list_sessions()}


@router.post("/sessions", dependencies=[Depends(require_token)])
def create_session_api(req: CreateReq) -> dict:
    meta = sess.create_session(name=req.name or "", model=req.model or MODEL)
    return meta


@router.post("/sessions/organize", dependencies=[Depends(require_token)])
def create_organize_session_api(req: CreateReq | None = None) -> dict:
    """Create a session preconfigured with the archive-curator system
    prompt. Returns the session metadata + an initial_message the
    frontend should immediately send to kick off the curator workflow.
    See backend/prompts.py for the actual prompt + bilingual seed."""
    from .prompts import CURATOR_SYSTEM_PROMPT, CURATOR_INITIAL_MESSAGE
    import datetime as _dt
    name = (req.name if req else None) or (
        "[整理档案] " + _dt.datetime.now().strftime("%m-%d %H:%M"))
    model = (req.model if req else None) or MODEL
    meta = sess.create_session(
        name=name, model=model, system_prompt=CURATOR_SYSTEM_PROMPT)
    return {**meta, "initial_message": CURATOR_INITIAL_MESSAGE}


@router.post("/sessions/profile-intake", dependencies=[Depends(require_token)])
def create_profile_intake_session_api(req: CreateReq | None = None) -> dict:
    """Create a session preconfigured for CLAUDE.md profile intake —
    Muse asks questions conversationally and Edits the file as the
    user answers. Side-effect: if CLAUDE.md doesn't exist yet at the
    archive root, seed it from the template (locale-aware) so the
    chat workflow has something to Read on its first tool call.

    See backend/prompts.py for the system prompt + bilingual seed."""
    from .prompts import PROFILE_INTAKE_SYSTEM_PROMPT, PROFILE_INTAKE_INITIAL_MESSAGE
    import datetime as _dt
    import shutil as _shutil

    # If no CLAUDE.md yet, drop the template in so the agent's first
    # Read tool call succeeds. Locale-aware: zh if any of LANG / LC_ALL /
    # LC_MESSAGES env vars contains "zh" (matches the install/intake
    # scripts' detection logic — see scripts/install-linux.sh).
    project_claude_md = ROOT / "CLAUDE.md"
    if not project_claude_md.exists():
        lang_env = (
            os.environ.get("LANG", "")
            + os.environ.get("LC_ALL", "")
            + os.environ.get("LC_MESSAGES", "")
        )
        is_zh = "zh" in lang_env.lower()
        repo_root = Path(__file__).resolve().parent.parent
        tpl_name = "default-CLAUDE.md" if is_zh else "default-CLAUDE.en.md"
        tpl_path = repo_root / "scripts" / "templates" / tpl_name
        if tpl_path.exists():
            content = tpl_path.read_text(encoding="utf-8")
            content = content.replace(
                "%DATE%", _dt.datetime.now().strftime("%Y-%m-%d"))
            try:
                project_claude_md.write_text(content, encoding="utf-8")
            except OSError as e:
                # Don't block session creation — agent will fail more
                # informatively when it tries to Read a non-existent file.
                import sys as _sys
                _sys.stderr.write(
                    f"[profile-intake] couldn't seed CLAUDE.md: {e}\n")
                _sys.stderr.flush()

        # Also drop archive-skeleton subdirs so the user's first
        # interaction has the right shape on disk. Skip ones that exist.
        skel_root = repo_root / "scripts" / "templates" / "archive-skeleton"
        readme_src = "README.md" if is_zh else "README.en.md"
        for sub in ("health", "work", "money", "people", "notes", "archives"):
            sd = ROOT / sub
            if not sd.exists():
                try:
                    sd.mkdir(parents=True, exist_ok=True)
                    src = skel_root / sub / readme_src
                    if src.exists():
                        _shutil.copy(src, sd / "README.md")
                except OSError:
                    pass

    # Session label follows the same locale check used for the template.
    # English users were seeing "[设置档案] 05-22 14:30" in their tab strip
    # because we always used the Chinese label.
    lang_env_for_name = (
        os.environ.get("LANG", "")
        + os.environ.get("LC_ALL", "")
        + os.environ.get("LC_MESSAGES", "")
    )
    is_zh_for_name = "zh" in lang_env_for_name.lower()
    default_label = "[设置档案] " if is_zh_for_name else "[Set up profile] "
    name = (req.name if req else None) or (
        default_label + _dt.datetime.now().strftime("%m-%d %H:%M"))
    model = (req.model if req else None) or MODEL
    meta = sess.create_session(
        name=name, model=model, system_prompt=PROFILE_INTAKE_SYSTEM_PROMPT)
    return {**meta, "initial_message": PROFILE_INTAKE_INITIAL_MESSAGE}


def _extract_searchable_text(content: Any) -> str:
    """Extract plain text from a JSONL message.content field for search.
    Handles both string content and list-of-blocks. Skips tool_use /
    tool_result blocks because their inputs/outputs are usually noisy
    JSON and not what users mean when they search."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
        elif btype == "thinking":
            t = block.get("thinking")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def _make_snippet(text: str, idx: int, qlen: int, *,
                   ctx: int = 60, max_len: int = 200) -> str:
    """Build a search-result snippet centered on a match. Caller passes the
    match position so we don't have to find() twice. Result is capped at
    max_len chars with leading/trailing ellipses if truncated."""
    start = max(0, idx - ctx)
    end = min(len(text), idx + qlen + ctx)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    # Collapse whitespace runs so multi-line transcripts render compactly
    # in the search result list.
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > max_len:
        snippet = snippet[:max_len - 1] + "…"
    return snippet


@router.get("/search", dependencies=[Depends(require_token)])
def search_sessions_api(q: str = Query(default="", min_length=0, max_length=200),
                         limit: int = Query(default=30, ge=1, le=100)) -> dict:
    """Cross-session full-text search. Scans CLI JSONL files for user /
    assistant text matching `q` (case-insensitive substring). Returns
    hits sorted by timestamp desc. Each hit:
        {sid, name, uuid, role, snippet, ts}
    Implementation: line-by-line JSON parse of every JSONL under the
    project's CLI directory. For ~200 sessions of typical size (< 1MB
    each) this runs in <500ms — switch to SQLite FTS5 if it grows."""
    query = q.strip()
    if not query:
        return {"hits": [], "total": 0}
    qlower = query.lower()
    if ROOT is None:
        return {"hits": [], "total": 0}
    projects_root = Path.home() / ".claude" / "projects"
    cwd_key = str(ROOT).replace("/", "-")
    proj_dir = projects_root / cwd_key
    if not proj_dir.exists():
        return {"hits": [], "total": 0}

    name_map = {s["id"]: s.get("name", "") for s in sess.list_sessions()}

    hits: list[dict] = []
    PER_SESSION_CAP = 5   # avoid one chatty session swamping results
    for jsonl in proj_dir.glob("*.jsonl"):
        sid = jsonl.stem
        per_sess = 0
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    if qlower not in line.lower():
                        continue   # fast reject before JSON parse
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if entry.get("type") not in ("user", "assistant"):
                        continue
                    msg = entry.get("message") or {}
                    text = _extract_searchable_text(msg.get("content"))
                    if not text:
                        continue
                    # CLI's slash-command wrapper round-trips as user
                    # text — strip before matching so e.g. searching
                    # "compact" doesn't surface every /compact invocation.
                    text = _strip_cli_slash_wrapper(text) or text
                    pos = text.lower().find(qlower)
                    if pos < 0:
                        continue
                    hits.append({
                        "sid": sid,
                        "name": name_map.get(sid, ""),
                        "uuid": entry.get("uuid", ""),
                        "role": entry.get("type"),
                        "snippet": _make_snippet(text, pos, len(query)),
                        "ts": entry.get("timestamp", ""),
                    })
                    per_sess += 1
                    if per_sess >= PER_SESSION_CAP:
                        break
        except OSError:
            continue

    hits.sort(key=lambda h: h["ts"], reverse=True)
    return {"hits": hits[:limit], "total": len(hits)}


# CLI wraps slash commands as pseudo-user messages with these tags so it can
# round-trip through the conversation log. They're internal protocol detail
# and should never reach the user's chat UI as a regular bubble.
_CLI_SLASH_TAGS_RE = re.compile(
    r"<(command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr)>.*?</\1>",
    re.DOTALL,
)


def _strip_cli_slash_wrapper(text: str) -> str:
    """Remove CLI slash-command protocol tags. Returns cleaned text (may be
    empty — caller should skip rendering when empty)."""
    if not text:
        return text
    return _CLI_SLASH_TAGS_RE.sub("", text).strip()


def _sdk_messages_to_ui(sm_list: list, annotations: dict[str, dict],
                          compact_uuids: set[str] | None = None) -> list[dict]:
    """Convert SDK SessionMessage list into muselab's flat UI message list.
    Each SessionMessage may explode into multiple UI bubbles because the
    frontend renders tool_use / tool_result / thinking blocks as separate
    messages from the text bubble. Annotations (cost, model, images) attach
    by message UUID to the primary text bubble. UUIDs in `compact_uuids`
    get an `_is_compact_summary` flag so the UI can render a "📦 已压缩" pill."""
    compact_uuids = compact_uuids or set()
    out: list[dict] = []
    for sm in sm_list:
        ann = annotations.get(sm.uuid, {})
        is_compact = sm.uuid in compact_uuids
        msg = sm.message or {}
        content = msg.get("content")

        # Simple shape: content is a single string.
        if isinstance(content, str):
            text = _strip_cli_slash_wrapper(content)
            # CLI's slash-command wrapper (<command-name>/compact</command-name>
            # …) round-trips through the conversation log as a "user" turn;
            # hide it from the UI rather than rendering a confusing bubble.
            if not text:
                continue
            entry = {"role": sm.type, "text": text, "uuid": sm.uuid}
            if is_compact:
                entry["_is_compact_summary"] = True
            entry.update(ann)   # cost / model / images / etc.
            out.append(entry)
            continue
        if not isinstance(content, list):
            continue

        text_buf = ""
        image_refs = []   # placeholder for inline image blocks (if any in JSONL)

        def flush_text():
            nonlocal text_buf, image_refs
            # Strip CLI slash wrapper before deciding if there's anything to
            # render. Pure-wrapper messages produce empty text + no images
            # → drop the bubble entirely.
            cleaned = _strip_cli_slash_wrapper(text_buf)
            if not cleaned and not image_refs:
                text_buf = ""
                image_refs = []
                return
            entry = {"role": sm.type, "text": cleaned, "uuid": sm.uuid}
            if is_compact:
                entry["_is_compact_summary"] = True
            if image_refs:
                # CLI JSONL stores image source dicts; convert minimal info for UI.
                # If sidecar has full base64 (uploaded via muselab), it wins
                # — already merged via ann["images"].
                entry.setdefault("images", image_refs)
            entry.update(ann)
            out.append(entry)
            text_buf = ""
            image_refs = []

        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text_buf += block.get("text", "")
            elif bt == "thinking":
                flush_text()
                # Anthropic Opus 4.x extended-thinking blocks come back
                # redacted in the final transcript — `thinking` is "" and
                # only the `signature` survives. The plain-text content is
                # ONLY visible live via thinking_delta events during streaming.
                # Surface a placeholder so the UI doesn't show an empty
                # block on reload — reads as "model thought here but the
                # text isn't retained" rather than a broken render.
                th_text = block.get("thinking", "") or ""
                if not th_text.strip() and block.get("signature"):
                    th_text = "[已加密推理 · 仅 streaming 期间可见明文]"
                out.append({"role": "thinking", "text": th_text,
                             "uuid": sm.uuid})
            elif bt == "tool_use":
                flush_text()
                tu = {
                    "role": "tool_use",
                    "uuid": sm.uuid,
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                    # Compact summary that the frontend usually shows in the bubble
                    "summary": _summarize_tool_input(block.get("name"), block.get("input") or {}),
                }
                out.append(tu)
            elif bt == "tool_result":
                flush_text()
                tr_text = ""
                tr_content = block.get("content")
                if isinstance(tr_content, str):
                    tr_text = tr_content
                elif isinstance(tr_content, list):
                    parts = []
                    for p in tr_content:
                        if isinstance(p, dict):
                            parts.append(p.get("text", str(p)))
                        else:
                            parts.append(str(p))
                    tr_text = "\n".join(parts)
                out.append({
                    "role": "tool_result", "uuid": sm.uuid,
                    "id": block.get("tool_use_id"),
                    "preview": tr_text[:500],
                    "truncated": len(tr_text) > 500,
                    "is_error": bool(block.get("is_error", False)),
                })
            elif bt == "image":
                # Inline image block in user content — record a reference.
                # Real base64 lives in the sidecar (annotations["images"]) for
                # images the user uploaded via muselab's upload flow.
                src = block.get("source") or {}
                image_refs.append({"mime": src.get("media_type") or ""})
            # Other block types (server_tool_use, etc.) — skip silently for now.
        flush_text()
    # Propagate the turn-completion ts (stored on the LAST sm.uuid of
    # the turn via set_message_annotation in chat_stream's tail) onto
    # EVERY ui entry that shares that uuid — thinking / tool_use /
    # tool_result / assistant text. The frontend renders turn-footer
    # under whichever entry is the turn tail; making sure all of them
    # carry ts means whatever block ends up at the tail can display
    # the time. Cheap O(N) — annotations is already a dict lookup.
    for entry in out:
        u = entry.get("uuid")
        if not u:
            continue
        ts = annotations.get(u, {}).get("ts")
        if ts is not None and "ts" not in entry:
            entry["ts"] = ts
    return out


def _summarize_tool_input(name: str | None, inp: dict) -> str:
    """Same summarization used by _render_tool_use, factored to also work for
    raw dict input (post-JSONL parse, no ToolUseBlock instance)."""
    if not name:
        return ""
    if name in ("Read", "Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Bash":
        return (inp.get("command") or "")[:200]
    if name in ("Glob", "Grep"):
        return (inp.get("pattern") or "") + (f"  in {inp.get('path','')}" if inp.get("path") else "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "TodoWrite":
        return f"{len(inp.get('todos') or [])} todos"
    return ""


def _compact_summary_uuids(sid: str) -> set[str]:
    """Scan raw CLI JSONL for entries with isCompactSummary:true and return
    their UUIDs. SDK get_session_messages strips this flag, so to render a
    "📦 已压缩" indicator we have to detect it ourselves at the JSONL level.

    Glob-based JSONL lookup — same pattern as get_client's existence check.
    SDK's _find_project_dir was unreliable on Windows."""
    try:
        projects_root = Path.home() / ".claude" / "projects"
        if not projects_root.exists():
            return set()
        jsonl_path = None
        for hit in projects_root.glob(f"*/{sid}.jsonl"):
            if hit.is_file():
                jsonl_path = hit
                break
        if jsonl_path is None:
            return set()
        uuids: set[str] = set()
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                if '"isCompactSummary":true' not in line and '"isCompactSummary": true' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("isCompactSummary") and entry.get("uuid"):
                    uuids.add(entry["uuid"])
        return uuids
    except Exception:
        return set()


@router.get("/sessions/{sid}", dependencies=[Depends(require_token)])
def get_session_api(sid: str) -> dict:
    """Read session: metadata from muselab sidecar + transcript from CLI JSONL
    via SDK. Merges per-message annotations (cost, model, images) into the
    transcript so the UI gets one flat list of bubbles.

    Mid-turn fallback: SDK CLI only writes the JSONL on turn completion,
    so a reload while a reply is streaming would otherwise return an
    empty list. When an active TurnBroadcast exists for `sid`, we
    reconstruct the in-flight messages from its event buffer (user
    prompt + every SSE event yielded so far) so the user sees the
    partial reply instead of a blank session."""
    meta = sess.get_session_meta(sid)
    if meta is None:
        raise HTTPException(404, "session not found")
    try:
        sdk_msgs = get_session_messages(sid, directory=str(ROOT))
    except Exception:
        sdk_msgs = []
    annotations = sess.get_message_annotations(sid)
    compact_uuids = _compact_summary_uuids(sid)
    messages = _sdk_messages_to_ui(sdk_msgs, annotations, compact_uuids)
    # Mid-turn merge: SDK CLI writes the JSONL incrementally — the
    # user prompt lands immediately when the turn starts, but the
    # assistant reply (text/thinking/tool blocks) only commits when
    # the whole turn finishes. So a reload during streaming sees the
    # user msg but no reply. The active TurnBroadcast has the live
    # event stream → reconstruct an in-progress view from it and
    # splice it in place of the last (incomplete) user msg the SDK
    # returned. When the turn finishes, the active broadcast is
    # popped and this branch becomes inert; the SDK JSONL alone is
    # the source of truth again.
    # NOTE: deliberately NOT layering broadcast rebuild on top of the
    # SDK transcript here. The frontend's _checkActiveTurn fires SSE
    # reconnect when the backend says active=true, and the reconnect
    # endpoint replays the broadcast buffer + streams live events.
    # If we rebuilt the in-flight portion here too, the user would
    # either:
    #  a) see static partial content with no further streaming
    #     (frontend skips reconnect because messages already ends in
    #     assistant), or
    #  b) see duplicated content (SDK partial + broadcast replay).
    # Keeping this path SDK-only lets reconnect be the sole live-tail
    # mechanism. The user briefly sees just the user msg, then SSE
    # fills in everything via replay → live.
    return {**meta, "messages": messages}


def _broadcast_to_ui_messages(bc: "TurnBroadcast") -> list[dict]:
    """Reconstruct a UI-shaped message list from an in-flight broadcast.
    Lossy by design: this is shown only mid-turn while SDK JSONL is
    empty. Once the turn finishes the regular SDK→UI path takes over
    and we drop this view.

    Events fold like the streaming-handler's openAsst/closeAsst dance:
    consecutive 'text' deltas form one assistant bubble; thinking
    deltas accumulate into one thinking message; tool_use / tool_result
    push their own messages. Non-render events (done / error / etc.)
    are ignored here — the UI's `done` handler only matters in live
    streaming, not in a reload-rebuild."""
    out: list[dict] = []
    if bc.user_text or bc.user_images or bc.user_docs:
        out.append({
            "role": "user",
            "text": bc.user_text,
            "images": bc.user_images,
            "docs": bc.user_docs,
        })
    cur_text_msg: dict | None = None
    cur_thinking_msg: dict | None = None
    for ev in bc.events:
        kind = ev.get("event") or ""
        data_str = ev.get("data") or "{}"
        try:
            data = json.loads(data_str)
        except Exception:
            continue
        if kind == "text":
            cur_thinking_msg = None
            chunk = data.get("text", "")
            if cur_text_msg is None:
                cur_text_msg = {"role": "assistant", "text": chunk,
                                  "model": bc.model}
                out.append(cur_text_msg)
            else:
                cur_text_msg["text"] += chunk
        elif kind == "thinking":
            cur_text_msg = None
            chunk = data.get("text", "")
            if cur_thinking_msg is None:
                cur_thinking_msg = {"role": "thinking", "text": chunk}
                out.append(cur_thinking_msg)
            else:
                cur_thinking_msg["text"] += chunk
        elif kind == "tool_use":
            cur_text_msg = None
            cur_thinking_msg = None
            out.append({
                "role": "tool_use",
                "name": data.get("name"),
                "summary": data.get("summary"),
                "input": data.get("input"),
                **({"todos": data["todos"]} if "todos" in data else {}),
                **({"task": data["task"]} if "task" in data else {}),
                **({"plan": data["plan"]} if "plan" in data else {}),
            })
        elif kind == "tool_result":
            cur_text_msg = None
            cur_thinking_msg = None
            out.append({
                "role": "tool_result",
                "preview": data.get("preview"),
                "truncated": data.get("truncated"),
                "is_error": data.get("is_error"),
            })
        # ask_user_question / permission_request not reconstructed here —
        # they're interactive blocks whose answer state lives in the
        # ask/perm queues, not in the broadcast buffer.
    return out


@router.get("/sessions/{sid}/export", dependencies=[Depends(require_token_query)])
def export_session_markdown(sid: str) -> Response:
    """Render the transcript as a single Markdown file the user can save.

    Auth is via ?token=... rather than the header — file downloads from a
    plain anchor don't carry custom headers."""
    meta = sess.get_session_meta(sid)
    if meta is None:
        raise HTTPException(404, "session not found")
    try:
        sdk_msgs = get_session_messages(sid, directory=str(ROOT))
    except Exception:
        sdk_msgs = []
    annotations = sess.get_message_annotations(sid)
    compact_uuids = _compact_summary_uuids(sid)
    messages = _sdk_messages_to_ui(sdk_msgs, annotations, compact_uuids)

    name = meta.get("name") or "session"
    model = meta.get("model") or ""
    created = meta.get("created_at")
    created_str = (datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")
                    if created else "")
    lines: list[str] = [f"# {name}", ""]
    if created_str:
        lines.append(f"*Created: {created_str}*  ")
    if model:
        lines.append(f"*Model: {model}*  ")
    lines.append(f"*Messages: {len(messages)}*")
    lines.append("")
    for m in messages:
        role = m.get("role")
        text = (m.get("text") or "").strip()
        if not text or role in ("tool_use", "tool_result"):
            continue
        if role == "user":
            lines.append("---")
            lines.append("")
            lines.append("### 👤 User")
        elif role == "assistant":
            lines.append("### 🤖 Muse")
        else:
            lines.append(f"### {role}")
        lines.append("")
        lines.append(text)
        lines.append("")

    body = "\n".join(lines)
    # Filenames in Content-Disposition can't safely include CJK / spaces in all
    # browsers; fall back to a slug. RFC 5987 filename*=UTF-8 covers Unicode for
    # modern browsers; the bare filename is an ASCII fallback for older ones.
    safe_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "session"
    safe_slug = safe_slug[:60]
    encoded = urllib.parse.quote(name, safe="")
    headers = {
        "Content-Disposition":
            f'attachment; filename="{safe_slug}.md"; '
            f"filename*=UTF-8''{encoded}.md",
    }
    return Response(content=body, media_type="text/markdown; charset=utf-8",
                    headers=headers)


@router.delete("/sessions/{sid}", dependencies=[Depends(require_token)])
async def delete_session_api(sid: str) -> dict:
    await disconnect_client(sid)
    # SDK delete first (removes CLI JSONL); then muselab sidecar.
    try:
        sdk_delete_session(sid, directory=str(ROOT))
    except (FileNotFoundError, ValueError):
        pass   # JSONL never existed (session never streamed) — that's fine
    if not sess.delete_session(sid):
        raise HTTPException(404, "session not found")
    return {"ok": True}


class SessionPatchReq(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    # SDK-native session tag — written to CLI JSONL so other tools (and
    # manual `claude` CLI runs) see it. Pass empty string to clear.
    tag: str | None = None
    # Pin to top of the session picker. None = no change, True/False = set.
    pinned: bool | None = None
    # Reasoning effort knob — "" / "low" / "medium" / "high" / "xhigh" / "max".
    # Empty string clears the override (SDK picks adaptive). Changing effort
    # invalidates cached clients so the next turn rebuilds with the new value.
    effort: str | None = None


@router.patch("/sessions/{sid}", dependencies=[Depends(require_token)])
async def patch_session_api(sid: str, req: SessionPatchReq) -> dict:
    ok = False
    if req.name is not None:
        ok = sess.rename_session(sid, req.name) or ok
        # Also propagate to CLI's JSONL so list_sessions() / manual claude
        # CLI runs see the new title. Silent no-op if JSONL doesn't exist yet.
        try:
            sdk_rename_session(sid, req.name, directory=str(ROOT))
        except (FileNotFoundError, ValueError):
            pass
    if req.tag is not None:
        # Empty string → clear tag. SDK accepts None or str.
        try:
            sdk_tag_session(sid, req.tag or None, directory=str(ROOT))
            ok = True
        except (FileNotFoundError, ValueError) as e:
            # JSONL doesn't exist yet → tag has nowhere to live until first
            # query. Surface as a 409 so the FE can wait for first turn.
            raise HTTPException(409, f"cannot tag session before first turn: {e}")
    if req.pinned is not None:
        # Pin is muselab-local (not stored in CLI JSONL). Always idempotent.
        idx = sess._load_index()
        found = False
        for s in idx:
            if s["id"] == sid:
                s["pinned"] = bool(req.pinned)
                found = True
                break
        if not found:
            import time as _time
            idx.append({
                "id": sid, "name": "", "model": "", "system_prompt": "",
                "created_at": _time.time(), "updated_at": _time.time(),
                "message_count": 0, "auto_named": True,
                "pinned": bool(req.pinned),
            })
        sess._save_index(idx)
        ok = True
    if req.system_prompt is not None:
        ok = sess.update_system_prompt(sid, req.system_prompt) or ok
        # System prompt change invalidates cached SDK clients for this session.
        await disconnect_client(sid)
    if req.effort is not None:
        # Validate against SDK literal set; empty string is a deliberate
        # "clear override" signal so the user can revert to adaptive.
        valid = {"", "low", "medium", "high", "xhigh", "max"}
        if req.effort not in valid:
            raise HTTPException(400, f"invalid effort: {req.effort}")
        sess.update_effort(sid, req.effort)
        # Effort is baked into ClaudeAgentOptions at construction time, so a
        # change requires rebuilding the client. The next stream() call will
        # pick up the new value via sess.get_session().
        await disconnect_client(sid)
        ok = True
    if req.model is not None:
        # Model switch is allowed any time — including mid-session. The next
        # turn will use the new model (frontend captures `streamingModel`
        # per-request so old bubbles keep their original model badge).
        # Caveats (frontend warns about cross-vendor):
        #   - cross-vendor switches can hit thinking-signature errors on the
        #     next reply if the prior turn had thinking blocks
        #   - prompt cache resets when model changes (first turn slower)
        # If a turn is still streaming for this session, interrupt it
        # first. Otherwise the old CLI subprocess is still actively
        # writing to the session JSONL and disconnect_client below would
        # race with that — leading to "Session ID already in use" on the
        # next stream's CLI spawn (eg. GLM → MiniMax mid-reply).
        bc = _active_turns.get(sid)
        if bc is not None and not bc.done:
            async with _lock:
                live_clients = [c for k, c in _clients.items() if k[0] == sid]
            for c in live_clients:
                try:
                    await c.interrupt()
                except Exception as _e:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[chat] interrupt before model swap failed for "
                        f"{sid}: {type(_e).__name__}: {_e}\n")
        sess.update_model(sid, req.model)
        # SDK-native swap if same provider — `client.set_model()` reuses the
        # CLI subprocess (and its loaded CLAUDE.md / MCP / system prompt).
        # Cross-provider switch (e.g. Claude → DeepSeek) needs full rebuild
        # because env_override / base_url differ.
        async with _lock:
            live = [(k, c) for k, c in _clients.items() if k[0] == sid]
        pa = endpoints.lookup(req.model)
        same_provider = (
            len(live) == 1
            and ((pa is None and endpoints.lookup(live[0][0][1]) is None)
                 or (pa is not None
                     and endpoints.lookup(live[0][0][1]) is not None
                     and endpoints.lookup(live[0][0][1]).prefix == pa.prefix)))
        if same_provider:
            (old_key, client) = live[0]
            try:
                await client.set_model(req.model)
                async with _lock:
                    _clients.pop(old_key, None)
                    perm = _client_permission.pop(old_key, "bypassPermissions")
                    if old_key in _client_lru:
                        _client_lru.remove(old_key)
                    # Preserve the effort dimension when remapping under the
                    # new model — set_model() keeps the SDK options object,
                    # which still has the prior effort baked in.
                    new_key = (sid, req.model, old_key[2])
                    _clients[new_key] = client
                    _client_permission[new_key] = perm
                    _client_lru.append(new_key)
            except Exception as e:
                import sys as _sys
                _sys.stderr.write(
                    f"[chat] set_model {old_key[1]}→{req.model} failed: "
                    f"{type(e).__name__}: {e}; rebuilding on next turn\n")
                await disconnect_client(sid)
        else:
            # Cross-provider, or no/multiple live clients — disconnect; the
            # next send() rebuilds with the new model.
            await disconnect_client(sid)
        ok = True
    if not ok:
        raise HTTPException(404, "session not found or no changes")
    return {"ok": True}


# ====== usage / reset ======

@router.get("/usage", dependencies=[Depends(require_token)])
def usage() -> dict:
    cr = _stats.get("total_cache_read_tokens", 0)
    in_t = _stats.get("total_input_tokens", 0)
    cache_pct = round(cr / (cr + in_t) * 100, 1) if (cr + in_t) > 0 else 0
    return {**_stats, "model_default": MODEL,
            "active_sessions": list(_clients.keys()),
            "cache_hit_pct": cache_pct,
            "budget_usd": _budget_usd(),
            "budget_used_pct": (
                round(_stats["total_cost_usd"] / _budget_usd() * 100, 1)
                if _budget_usd() > 0 else 0
            )}


@router.get("/usage/{session_id}", dependencies=[Depends(require_token)])
def session_usage(session_id: str, model: str = "") -> dict:
    """Per-session context meter — what fraction of the model's window we're at.

    Note: this is the cheap path — reads cached per-turn usage values.
    For a true breakdown (per CLAUDE.md file, per MCP tool, per skill),
    use /context-breakdown/{session_id} which invokes
    ClaudeSDKClient.get_context_usage() against the live session."""
    u = _session_usage.get(session_id, {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "total_cost_usd": 0.0, "last_turn_at": 0.0,
        "context_used": 0, "context_used_pct": 0.0, "context_limit": 0,
    })
    m = model or MODEL
    # Take the MAX of (stored sess_u value, hardcoded table). This way when
    # MODEL_CONTEXT_LIMITS gets bumped (e.g. Opus 200K → 1M), an existing
    # session that stored the old 200K still picks up the new ceiling on the
    # next /usage poll — no need to wait for the user to send a new message.
    # SDK truth (from ResultMessage handler) overrides on next stream done.
    stored = int(u.get("context_limit", 0) or 0)
    hardcoded = MODEL_CONTEXT_LIMITS.get(m, DEFAULT_CONTEXT_LIMIT)
    limit = max(stored, hardcoded)
    # Diagnostic: every /usage call writes which value won. Lets you confirm
    # in stderr.log whether your meter is showing hardcoded fallback, stored
    # SDK value, or whether the model name even resolves in the table.
    import sys as _sys
    in_table = m in MODEL_CONTEXT_LIMITS
    _sys.stderr.write(
        f"[usage] sid={session_id[:8]} model={m!r} in_table={in_table} "
        f"stored={stored} hardcoded={hardcoded} → limit={limit}\n")
    _sys.stderr.flush()
    # Prefer SDK-authoritative numbers populated by the stream's ResultMessage
    # handler. Fall back to the legacy estimate only if no turn has completed
    # yet (in which case `context_used` is 0 anyway → 0% display, correct).
    if u.get("context_used"):
        ctx_used = int(u["context_used"])
        # Recompute pct against possibly-bumped limit so it doesn't show stale
        # high percentage (e.g. 14.2% if computed against 200K but limit is 1M).
        ctx_pct = round(ctx_used / limit * 100, 1) if limit else 0.0
    else:
        # Conservative fallback: per-turn input only (not summed with cache,
        # because cache_read/cache_creation in SDK usage are cumulative and
        # would inflate the meter — see ResultMessage handler comment).
        ctx_used = int(u.get("input_tokens", 0) or 0)
        ctx_pct = round(ctx_used / limit * 100, 1) if limit else 0
    return {
        **u,
        "model": m,
        "context_limit": limit,
        "context_used": ctx_used,
        "context_used_pct": ctx_pct,
    }


def _parse_cost(raw: Any) -> float:
    """Sidecar stores cost as the formatted string we showed in the UI
    (e.g. '$0.1993'). Parse back to a float for aggregation. Returns 0.0
    for missing / unparseable values."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip().lstrip("$").replace(",", "")
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def _empty_bucket() -> dict:
    """Per-time-bucket aggregator shape. Used by cost_dashboard to add
    up arbitrary turn slices. Cost comes from sidecar (vendor knows
    pricing); tokens come from JSONL (universal across all vendors)."""
    return {"input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "cost": 0.0, "turns": 0}


def _add_bucket(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if k in dst:
            dst[k] += v


@router.get("/cost-dashboard", dependencies=[Depends(require_token)])
def cost_dashboard(days: int = Query(default=30, ge=1, le=365),
                    tz_offset_minutes: int = Query(default=0, ge=-1440, le=1440)
                    ) -> dict:
    """Aggregate per-turn usage across all sessions, bucketed by local date
    and by model. JSONL is the truth for **token counts and model** (CLI
    writes `message.usage` per turn for every vendor — Anthropic, GLM,
    MiniMax, DeepSeek). Sidecar adds **cost in USD** where available
    (only Anthropic + a few others report it; third-party vendors
    typically report 0). All vendors get full token visibility.

    `tz_offset_minutes` lets the browser ask for buckets in its local
    timezone (e.g. Beijing = +480). Server stays UTC internally.

    Returns:
      {
        "window_days": int,
        "today" / "last_7d" / "last_30d" / "all_time": {
            input_tokens, output_tokens, cache_read_tokens,
            cache_creation_tokens, cost, turns
        },
        "by_day":   [{date, ...same fields}, ...]   # densified to `days`
        "by_model": [{model, ...same fields}, ...]  # all time
      }
    """
    import datetime as _dt
    from collections import defaultdict

    # 1) Sidecar costs by (sid, uuid) — optional overlay, may be sparse
    # or empty for third-party vendors. Walk it once so the JSONL scan
    # can do a cheap dict lookup per turn.
    cost_by_uuid: dict[str, dict[str, float]] = {}
    for sidecar in sess.SESS_DIR.glob("*.sidecar.json"):
        sid = sidecar.name.split(".sidecar.json")[0]
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        msgs = data.get("messages") or {}
        per_sess: dict[str, float] = {}
        for uuid_key, ann in msgs.items():
            if not isinstance(ann, dict):
                continue
            cost_val = _parse_cost(ann.get("cost"))
            if cost_val > 0:
                per_sess[uuid_key] = cost_val
        if per_sess:
            cost_by_uuid[sid] = per_sess

    # 2) Walk JSONL — the universal token source. Every vendor writes
    # message.usage on assistant turns in Anthropic-compatible shape
    # (CLI normalizes OpenAI-compatible vendors transparently).
    tz = _dt.timezone(_dt.timedelta(minutes=tz_offset_minutes))
    now = _dt.datetime.now(tz)
    today_str = now.date().isoformat()
    cutoff_day = (now.date() - _dt.timedelta(days=days - 1)).isoformat()
    cutoff_7d  = (now.date() - _dt.timedelta(days=6)).isoformat()

    all_total   = _empty_bucket()
    today_total = _empty_bucket()
    last_7d     = _empty_bucket()
    last_30d    = _empty_bucket()
    by_day:   dict[str, dict] = defaultdict(_empty_bucket)
    by_model: dict[str, dict] = defaultdict(_empty_bucket)

    # Discover all JSONLs for muselab-tracked sessions. SDK CLI keys
    # the projects dir by the cwd that ran the session, so a single
    # logical archive (one ROOT) can have JSONL spread across multiple
    # `~/.claude/projects/<encoded-cwd>/` dirs if ROOT (or working dir)
    # changed over time. We track every JSONL whose session id matches
    # something muselab knows about (sidecar OR sess.list_sessions()) —
    # this catches early sessions on prior cwds without picking up
    # totally unrelated projects.
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return _empty_dashboard_response(days, tz_offset_minutes, now)

    known_sids: set[str] = set()
    for sidecar in sess.SESS_DIR.glob("*.sidecar.json"):
        known_sids.add(sidecar.name.split(".sidecar.json")[0])
    try:
        for s in sess.list_sessions():
            sid = s.get("id")
            if sid:
                known_sids.add(sid)
    except Exception:
        pass

    jsonl_paths: list[Path] = []
    for proj_sub in projects_root.iterdir():
        if not proj_sub.is_dir():
            continue
        for jsonl in proj_sub.glob("*.jsonl"):
            if jsonl.stem in known_sids:
                jsonl_paths.append(jsonl)

    for jsonl in jsonl_paths:
        sid = jsonl.stem
        sid_costs = cost_by_uuid.get(sid, {})
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    # Cheap reject: only assistant turns carry usage.
                    if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not isinstance(usage, dict):
                        continue
                    in_t  = int(usage.get("input_tokens", 0) or 0)
                    out_t = int(usage.get("output_tokens", 0) or 0)
                    cr_t  = int(usage.get("cache_read_input_tokens", 0)
                                  or usage.get("cache_read_tokens", 0) or 0)
                    cc_t  = int(usage.get("cache_creation_input_tokens", 0)
                                  or usage.get("cache_creation_tokens", 0) or 0)
                    # Skip empty-usage entries (e.g. CLI-internal markers).
                    if in_t == 0 and out_t == 0 and cr_t == 0 and cc_t == 0:
                        continue
                    # A single "turn" = one user prompt + its assistant
                    # response chain. Inside that chain there can be many
                    # intermediate assistant lines for tool_use loops —
                    # those have stop_reason="tool_use". Only count the
                    # final completion (stop_reason="end_turn", "max_tokens",
                    # or sometimes None for legacy/streamed lines).
                    stop_reason = msg.get("stop_reason")
                    is_final = stop_reason in (None, "end_turn",
                                                  "max_tokens", "stop_sequence")
                    ts = entry.get("timestamp") or ""
                    if not ts:
                        continue
                    try:
                        dt_utc = _dt.datetime.fromisoformat(
                            ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    day_str = dt_utc.astimezone(tz).date().isoformat()
                    model_name = msg.get("model") or "unknown"
                    uuid_key = entry.get("uuid") or ""
                    cost_val = sid_costs.get(uuid_key, 0.0)

                    turn = {
                        "input_tokens": in_t,
                        "output_tokens": out_t,
                        "cache_read_tokens": cr_t,
                        "cache_creation_tokens": cc_t,
                        "cost": cost_val,
                        # Every assistant line contributes tokens (each
                        # tool-use loop iteration costs real compute), but
                        # only the final completion counts as a "turn"
                        # from the user's perspective.
                        "turns": 1 if is_final else 0,
                    }
                    _add_bucket(all_total, turn)
                    _add_bucket(by_model[model_name], turn)
                    if day_str >= cutoff_day:
                        _add_bucket(by_day[day_str], turn)
                        _add_bucket(last_30d, turn)
                    if day_str >= cutoff_7d:
                        _add_bucket(last_7d, turn)
                    if day_str == today_str:
                        _add_bucket(today_total, turn)
        except OSError:
            continue

    # Densify by_day so quiet days still get a zero bar.
    dense_days: list[dict] = []
    for i in range(days):
        d = (now.date() - _dt.timedelta(days=days - 1 - i)).isoformat()
        bucket = by_day.get(d, _empty_bucket())
        dense_days.append({"date": d, **_round_bucket(bucket)})

    by_model_list = sorted(
        [{"model": k, **_round_bucket(v)} for k, v in by_model.items()],
        key=lambda x: (x["input_tokens"] + x["output_tokens"]
                        + x["cache_read_tokens"] + x["cache_creation_tokens"]),
        reverse=True)

    return {
        "window_days": days,
        "tz_offset_minutes": tz_offset_minutes,
        "today":    _round_bucket(today_total),
        "last_7d":  _round_bucket(last_7d),
        "last_30d": _round_bucket(last_30d),
        "all_time": _round_bucket(all_total),
        "by_day":   dense_days,
        "by_model": by_model_list,
    }


def _round_bucket(b: dict) -> dict:
    return {**b, "cost": round(b["cost"], 4)}


def _empty_dashboard_response(days: int, tz_offset_minutes: int, now) -> dict:
    """Helper for the no-JSONL case — returns the same shape with all
    zeros + a densified by_day list so the frontend's chart doesn't
    crash on missing keys."""
    import datetime as _dt
    dense = [{"date": (now.date() - _dt.timedelta(days=days - 1 - i)).isoformat(),
                **_round_bucket(_empty_bucket())} for i in range(days)]
    return {
        "window_days": days,
        "tz_offset_minutes": tz_offset_minutes,
        "today":    _round_bucket(_empty_bucket()),
        "last_7d":  _round_bucket(_empty_bucket()),
        "last_30d": _round_bucket(_empty_bucket()),
        "all_time": _round_bucket(_empty_bucket()),
        "by_day":   dense,
        "by_model": [],
    }


@router.get("/context-breakdown/{session_id}", dependencies=[Depends(require_token)])
async def context_breakdown(session_id: str, model: str = "") -> dict:
    """Detailed context breakdown via SDK — answers "where did my 100K go?".
    Calls ClaudeSDKClient.get_context_usage() which returns the same data
    the CLI's /context command shows: tokens per category (memory files,
    MCP tools, agents, system tools, system prompt sections), with
    per-file and per-tool breakdowns.

    Returns 404 if the session doesn't have a live SDK client yet — that
    happens for newly-created sessions that haven't run a turn."""
    s = sess.get_session(session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    m = (model or s.get("model") or MODEL).strip()
    # The context-breakdown call is read-only and effort-independent — find
    # ANY live client for this (sid, model) pair regardless of effort key.
    matched = [k for k in _clients if k[0] == session_id and k[1] == m]
    if not matched:
        # No live client → can't ask CLI for breakdown. Surface this rather
        # than returning fake data; frontend can fall back to /usage.
        raise HTTPException(409, "no live client for this session — send a message first")
    key = matched[0]
    try:
        breakdown = await _clients[key].get_context_usage()
        # Pass through the SDK's response shape directly. Frontend can pick
        # whichever fields it wants to render.
        return dict(breakdown)
    except Exception as e:
        raise HTTPException(500, f"get_context_usage failed: {e}")


@router.post("/sessions/{sid}/native-compact", dependencies=[Depends(require_token)])
async def native_compact_session_api(sid: str) -> dict:
    """Compact a session using the CLI's native /compact slash command via SDK.
    Lossless — CLI writes compact_boundary + isCompactSummary into the session
    JSONL. Subsequent get_session_messages() returns the summary in place of
    pre-compaction history, so the UI automatically reflects the compacted
    state on next loadSession — no muselab-side marker needed.

    Session ID stays the same; tool_use history is preserved in the summary."""
    meta = sess.get_session_meta(sid)
    if meta is None:
        raise HTTPException(404, "session not found")
    model = (meta.get("model") or "").strip() or MODEL
    client = await get_client(sid, model, "bypassPermissions")
    try:
        await client.query("/compact")
        async for _ in client.receive_response():
            pass
    except Exception as e:
        raise HTTPException(500, f"native /compact failed: {e}")
    # Refresh message_count + turn_count so the sidebar reflects the
    # compacted size. turn_count uses the real-prompt filter — see the
    # comment on _is_real_user_prompt for why bare `type == "user"` over-
    # counts by 5-10× in tool-heavy sessions.
    try:
        new_msgs = get_session_messages(sid, directory=str(ROOT))
        n_turns = sum(1 for sm in new_msgs if _is_real_user_prompt(sm))
        sess.bump_session(sid, message_count=len(new_msgs),
                           turn_count=n_turns)
    except Exception:
        pass
    return {"ok": True}


class ForkReq(BaseModel):
    # Inclusive — fork copies the transcript up to and including this
    # message UUID. To branch BEFORE a user message (e.g. for an edit-and-
    # retry), pass the UUID of the previous assistant message.
    # Omit / null = no truncation, copy the full transcript.
    up_to_message_id: str | None = None
    title: str | None = None


@router.post("/sessions/{sid}/fork", dependencies=[Depends(require_token)])
def fork_session_api(sid: str, req: ForkReq) -> dict:
    """Branch a session at an arbitrary message UUID. SDK copies the JSONL
    transcript up to that point into a fresh session file with new UUIDs;
    muselab mirrors the new sid into index.json so it surfaces in the
    picker immediately. Use case: user edits one of their messages — UI
    forks at the previous assistant message, then resends the new text."""
    src_meta = sess.get_session_meta(sid)
    if src_meta is None:
        raise HTTPException(404, "session not found")
    try:
        result = sdk_fork_session(
            sid,
            directory=str(ROOT),
            up_to_message_id=req.up_to_message_id,
            title=req.title,
        )
    except FileNotFoundError:
        raise HTTPException(404, "source transcript not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"fork failed: {e}")
    new_sid = result.session_id
    new_name = req.title or ((src_meta.get("name") or "会话") + " (分支)")
    sess.register_session(
        new_sid,
        name=new_name,
        model=src_meta.get("model") or MODEL,
        system_prompt=src_meta.get("system_prompt") or "",
        auto_named=False,
    )
    return {"session_id": new_sid, "name": new_name}


class BudgetReq(BaseModel):
    budget_usd: float       # 0 = disabled


@router.get("/context-info", dependencies=[Depends(require_token)])
def context_info() -> dict:
    """Information about what Muse can see — used by the UI's onboarding
    hints (does the user have a CLAUDE.md? archive empty? skills loaded?
    has any working auth?). All paths relative to ROOT for safety.

    SDK options pass `setting_sources=["user", "project", "local"]`, so
    "Muse knows you" if EITHER the project-scope CLAUDE.md (ROOT/CLAUDE.md)
    OR the user-scope global one (~/.claude/CLAUDE.md) exists. We track
    both and surface the union — UI hides the "Muse doesn't know you yet"
    nag whenever any source is present."""
    project_claude_md = ROOT / "CLAUDE.md"
    user_claude_md = Path.home() / ".claude" / "CLAUDE.md"

    sources: list[dict] = []
    if project_claude_md.exists():
        try:
            sources.append({
                "scope": "project",
                "path": str(project_claude_md),
                "lines": sum(1 for _ in project_claude_md.open(encoding="utf-8", errors="replace")),
                "mtime": project_claude_md.stat().st_mtime,
            })
        except OSError:
            pass
    if user_claude_md.exists():
        try:
            sources.append({
                "scope": "user",
                "path": str(user_claude_md),
                "lines": sum(1 for _ in user_claude_md.open(encoding="utf-8", errors="replace")),
                "mtime": user_claude_md.stat().st_mtime,
            })
        except OSError:
            pass

    # Detect "do we have ANY working auth?" — needed so the chat-empty card
    # can warn "you have no provider set up; configure one before chatting".
    # Three valid Anthropic-side auth sources:
    #   1. Pro/Max OAuth (~/.claude/.credentials.json)
    #   2. ANTHROPIC_API_KEY  → x-api-key header
    #   3. ANTHROPIC_AUTH_TOKEN → Authorization: Bearer (OAuth/enterprise)
    # has_any_provider previously only checked #1 + third-party vendors,
    # so users who configured ANTHROPIC_API_KEY in Settings got a stuck
    # "no provider configured" warning (observed after clear-localStorage).
    claude_oauth = (Path.home() / ".claude" / ".credentials.json").exists()
    anthropic_api = bool(os.environ.get("ANTHROPIC_API_KEY")
                          or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    third_party_configured = [
        name for env_key, name in (
            ("DEEPSEEK_API_KEY", "DeepSeek"),
            ("ZHIPUAI_API_KEY",  "GLM"),
            ("MINIMAX_API_KEY",  "MiniMax"),
        ) if os.environ.get(env_key)
    ]
    # Back-compat: keep claude_md_exists / lines / mtime fields for any
    # consumer that hasn't migrated to the new claude_md_sources list.
    # Reflect "ANY source present" + union total lines + latest mtime so
    # the existing UI keeps working without changes.
    total_lines = sum(s["lines"] for s in sources)
    latest_mtime = max((s["mtime"] for s in sources), default=0.0)
    info: dict = {
        "archive_root": str(ROOT),
        "claude_md_exists": len(sources) > 0,
        "claude_md_lines": total_lines,
        "claude_md_mtime": latest_mtime,
        "claude_md_sources": sources,
        "archive_empty": True,
        "subdir_present": {},
        "has_claude_oauth": claude_oauth,
        "has_anthropic_api": anthropic_api,
        "third_party_configured": third_party_configured,
        "has_any_provider": (
            claude_oauth or anthropic_api or len(third_party_configured) > 0
        ),
    }
    # Subdirs the install scripts create — used to nudge "drop a doc into X"
    for sub in ("health", "work", "money", "people", "notes", "archives"):
        d = ROOT / sub
        present = d.exists() and d.is_dir()
        info["subdir_present"][sub] = present
        if present:
            # Count any file other than the README to decide "empty"
            try:
                non_readme = [p for p in d.iterdir()
                              if p.is_file() and p.name.lower() != "readme.md"]
                if non_readme:
                    info["archive_empty"] = False
            except OSError:
                pass
    # If the root itself has user docs (not a subdir-only setup), also count
    if info["archive_empty"]:
        try:
            for p in ROOT.iterdir():
                if p.is_file() and p.name not in ("CLAUDE.md",):
                    info["archive_empty"] = False
                    break
        except OSError:
            pass
    return info


@router.get("/probe/{model}", dependencies=[Depends(require_token)])
async def probe_provider(model: str) -> dict:
    """Hit the vendor's anthropic-compat endpoint with the configured key and
    return what the vendor said. Lets the user self-diagnose 401 / wrong-host
    / wrong-key issues WITHOUT pasting keys into chat. Always returns 200 on
    our side — the body carries vendor's status, headers, and partial body."""
    import httpx
    p = endpoints.lookup(model)
    if p is None:
        return {"ok": False, "reason": f"unknown model: {model}"}
    key = os.environ.get(p.env_key, "")
    if not key:
        return {"ok": False, "reason": f"{p.env_key} not configured (Settings → Provider API Keys)"}
    url = p.base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {"model": model, "max_tokens": 16,
             "messages": [{"role": "user", "content": "ping"}]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=body, headers=headers)
        snippet = r.text[:500]
        return {
            "ok": r.status_code == 200,
            "vendor": p.display, "model": model, "url": url,
            "status": r.status_code,
            "key_hint": f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "***",
            "vendor_response_excerpt": snippet,
        }
    except Exception as e:
        return {"ok": False, "reason": f"transport error: {type(e).__name__}: {e}",
                 "url": url}


@router.put("/budget", dependencies=[Depends(require_token)])
async def set_budget(req: BudgetReq) -> dict:
    """Set the soft budget cap. Stored in env (process-lifetime only — for a
    persistent cap, edit MUSELAB_BUDGET_USD in .env via /api/settings)."""
    if req.budget_usd < 0:
        raise HTTPException(400, "budget must be >= 0")
    os.environ["MUSELAB_BUDGET_USD"] = str(req.budget_usd)
    return {"ok": True, "budget_usd": req.budget_usd}


@router.get("/mcp", dependencies=[Depends(require_token)])
def mcp_status() -> dict:
    """Return configured MCP servers (parsed from mcp.json) for UI display."""
    if not MCP_CONFIG_PATH.exists():
        return {"configured": False, "servers": []}
    try:
        cfg = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        servers = cfg.get("mcpServers", {})
        return {
            "configured": True,
            "servers": [
                {"name": name, "command": s.get("command", ""), "args": s.get("args", [])}
                for name, s in servers.items()
            ],
        }
    except Exception as e:
        return {"configured": False, "servers": [], "error": str(e)}


@router.post("/interrupt", dependencies=[Depends(require_token_query)])
async def interrupt(session_id: str) -> dict:
    """Stop the current turn via SDK control protocol. Keeps the client
    connected so the next message continues the same conversation without
    re-spawning the CLI / re-loading CLAUDE.md / re-initializing MCP."""
    async with _lock:
        targets = [(k, c) for k, c in _clients.items() if k[0] == session_id]
    if not targets:
        return {"ok": True, "interrupted": [], "note": "no live client"}
    interrupted: list[str] = []
    for k, c in targets:
        try:
            await c.interrupt()
            interrupted.append(f"{k[0]}@{k[1]}")
        except Exception as e:
            import sys as _sys
            _sys.stderr.write(
                f"[chat-interrupt] {k} failed: {type(e).__name__}: {e}\n")
    return {"ok": True, "interrupted": interrupted}


@router.post("/reset", dependencies=[Depends(require_token_query)])
async def reset(session_id: str | None = None) -> dict:
    if session_id:
        await disconnect_client(session_id)
        return {"ok": True, "reset": [session_id]}
    async with _lock:
        keys = list(_clients.keys())
        for k in keys:
            c = _clients.pop(k, None)
            if c is not None:
                try:
                    await c.disconnect()
                except Exception:
                    pass
    return {"ok": True, "reset": [f"{s}@{m}" for s, m in keys]}


# ====== streaming ======

def _render_tool_use(block: ToolUseBlock) -> dict:
    inp = block.input or {}
    name = block.name
    if name in ("Read", "Edit", "Write"):
        summary = inp.get("file_path", "")
    elif name == "Bash":
        summary = (inp.get("command") or "")[:200]
    elif name in ("Glob", "Grep"):
        summary = (inp.get("pattern") or "") + (f"  in {inp.get('path','')}" if inp.get("path") else "")
    elif name == "WebFetch":
        summary = inp.get("url", "")
    elif name == "WebSearch":
        summary = inp.get("query", "")
    elif name == "TodoWrite":
        items = inp.get("todos") or []
        summary = f"{len(items)} todos"
    elif name == "Task":
        sub = inp.get("subagent_type") or "agent"
        desc = inp.get("description") or ""
        summary = f"[{sub}] {desc}"[:240]
    elif name == "ExitPlanMode":
        summary = (inp.get("plan") or "")[:240]
    elif name == "Skill":
        summary = inp.get("name") or inp.get("skill") or ""
    else:
        summary = json.dumps(inp, ensure_ascii=False)[:200]

    # Slim input — drop bulky fields (Write/Edit's `content`, `new_string`
    # etc. could be a whole file). FE uses `input.file_path` to drive the
    # clickable file-link chip and the preview auto-refresh on edit; without
    # this passthrough both silently no-op'd (toolFilePath always returned "",
    # _maybeReloadPreview never matched). 2026-05-18 audit fix.
    _SLIM_INPUT_FIELDS = {
        "file_path", "notebook_path", "path",
        "command", "pattern", "url", "query",
        "name", "skill", "subagent_type", "description", "todos",
    }
    slim_input = {k: v for k, v in inp.items() if k in _SLIM_INPUT_FIELDS}
    out: dict = {"name": name, "summary": summary, "id": block.id,
                  "input": slim_input}
    # Pass full structured payloads through for tools that have dedicated UIs.
    if name == "TodoWrite":
        out["todos"] = inp.get("todos") or []
    elif name == "Task":
        out["task"] = {
            "subagent_type": inp.get("subagent_type"),
            "description": inp.get("description"),
            "prompt": inp.get("prompt"),
        }
    elif name == "ExitPlanMode":
        out["plan"] = inp.get("plan") or ""
    return out


def _render_tool_result(block: ToolResultBlock) -> dict:
    text = ""
    if isinstance(block.content, str):
        text = block.content
    elif isinstance(block.content, list):
        parts = []
        for p in block.content:
            if isinstance(p, dict):
                parts.append(p.get("text", str(p)))
            else:
                parts.append(str(p))
        text = "\n".join(parts)
    return {"id": getattr(block, "tool_use_id", None),
            "preview": text[:500],
            "truncated": len(text) > 500,
            "is_error": bool(getattr(block, "is_error", False))}


# ====== attachment upload (images + documents) ======
#
# Multipart upload returns an attachment_id. Stream endpoint reads it (with
# TTL) and attaches as the right SDK block type:
#   - images (png/jpeg/gif/webp) → ImageBlock with base64 data
#   - PDFs → DocumentBlock with base64 data (Claude supports PDFs natively)
#   - text-ish docs (md / txt / csv / json / source code) → inline-text prefix
#     in the prompt so any model can consume them. Stored as utf-8 text.
# Stored in-memory; on restart pending uploads are lost (fine — re-attach).

_image_store: dict[str, dict] = {}     # id -> {kind, mime, b64|text, name, ts}
_IMAGE_TTL_S = 600
_IMAGE_MAX_BYTES = 10 * 1024 * 1024     # 10 MB per file
_IMAGE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_PDF_MIME = {"application/pdf"}
# text-ish formats we'll inline. Browsers send vague mimes — we also gate by
# extension below as a fallback.
_TEXT_MIME = {
    "text/plain", "text/markdown", "text/csv", "text/html", "text/css",
    "text/xml", "text/javascript", "text/typescript",
    "text/x-python", "text/x-yaml", "text/x-toml", "text/x-shellscript",
    "application/json", "application/xml", "application/yaml",
    "application/x-yaml", "application/toml",
}
_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".py", ".sh", ".bash", ".zsh", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".scss", ".xml", ".log", ".ini", ".conf", ".cfg",
    ".env.example", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp",
    ".rb", ".php", ".swift", ".kt", ".sql", ".dockerfile", ".gitignore",
}
# Spreadsheets — we pre-process these to CSV-style text via openpyxl so
# the model sees the data inline. Same "ends as `text` kind to the
# frontend" contract — frontend's _classifyFile maps these to "text"
# too so the chip is consistent.
_XLSX_EXTS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_TEXT_MAX_BYTES = 200 * 1024            # inline at most 200 KB as text
# Caps for xlsx inlining — same shape as the /api/files/xlsx preview
# endpoint, kept smaller because we're shoving this into the prompt
# context, not just rendering a table.
_XLSX_ATTACH_MAX_SHEETS = 5
_XLSX_ATTACH_MAX_ROWS = 200
_XLSX_ATTACH_MAX_COLS = 30
_XLSX_ATTACH_CELL_MAX_CHARS = 200


def _gc_images() -> None:
    """Drop entries older than TTL."""
    cutoff = time.time() - _IMAGE_TTL_S
    for k in list(_image_store.keys()):
        if _image_store[k]["ts"] < cutoff:
            del _image_store[k]


def _classify_attachment(mime: str, name: str) -> str:
    """Return one of: 'image' / 'pdf' / 'text' / 'xlsx' / '' (unsupported)."""
    mime = (mime or "").lower()
    if mime in _IMAGE_MIME:
        return "image"
    if mime in _PDF_MIME:
        return "pdf"
    if mime in _TEXT_MIME:
        return "text"
    # Fall back to extension check (browsers often send empty / octet-stream).
    lower = name.lower()
    for ext in _TEXT_EXTS:
        if lower.endswith(ext):
            return "text"
    if lower.endswith(".pdf"):
        return "pdf"
    for ext in _XLSX_EXTS:
        if lower.endswith(ext):
            return "xlsx"
    return ""


def _xlsx_to_text(body: bytes, name: str) -> str:
    """Read xlsx bytes and dump each sheet as `[Sheet: name]\\n<csv>` blocks.
    Capped by _XLSX_ATTACH_MAX_* so a 100k-row spreadsheet doesn't blow
    the prompt. Truncation is signaled inline so the model knows."""
    import openpyxl
    from io import BytesIO

    try:
        wb = openpyxl.load_workbook(BytesIO(body), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(
            422, f"failed to parse xlsx '{name}': {type(e).__name__}: {e}",
        )

    parts: list[str] = [f"# Spreadsheet: {name}"]
    sheets_total = len(wb.sheetnames)
    sheets_truncated = sheets_total > _XLSX_ATTACH_MAX_SHEETS
    try:
        for sheet_name in wb.sheetnames[:_XLSX_ATTACH_MAX_SHEETS]:
            ws = wb[sheet_name]
            parts.append("")
            parts.append(f"## Sheet: {sheet_name}")
            rows_emitted = 0
            cols_truncated = False
            rows_truncated = False
            for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                if r_idx >= _XLSX_ATTACH_MAX_ROWS:
                    rows_truncated = True
                    break
                cells: list[str] = []
                for c_idx, val in enumerate(row):
                    if c_idx >= _XLSX_ATTACH_MAX_COLS:
                        cols_truncated = True
                        break
                    if val is None:
                        cells.append("")
                    else:
                        s = str(val)
                        if len(s) > _XLSX_ATTACH_CELL_MAX_CHARS:
                            s = s[:_XLSX_ATTACH_CELL_MAX_CHARS] + "…"
                        # CSV-light: only quote/escape if a separator or
                        # quote actually appears (cheap heuristic — the
                        # model parses prose, not strict RFC 4180).
                        if "," in s or '"' in s or "\n" in s:
                            s = '"' + s.replace('"', '""') + '"'
                        cells.append(s)
                parts.append(",".join(cells))
                rows_emitted += 1
            if rows_truncated:
                parts.append(f"… (rows truncated at {_XLSX_ATTACH_MAX_ROWS})")
            if cols_truncated:
                parts.append(f"… (cols truncated at {_XLSX_ATTACH_MAX_COLS})")
            if rows_emitted == 0:
                parts.append("(empty sheet)")
    finally:
        wb.close()

    if sheets_truncated:
        parts.append("")
        parts.append(f"… (sheets truncated at {_XLSX_ATTACH_MAX_SHEETS} "
                     f"of {sheets_total})")

    return "\n".join(parts)


@router.post("/upload-image", dependencies=[Depends(require_token)])
async def upload_image(file: UploadFile = File(...)) -> dict:
    """Legacy endpoint name; now handles images + PDF + text-ish docs + xlsx."""
    import sys as _sys
    _t0 = time.perf_counter()
    _gc_images()
    mime = (file.content_type or "").lower()
    name = file.filename or "upload"
    kind = _classify_attachment(mime, name)
    if not kind:
        raise HTTPException(
            400,
            f"unsupported file type: {mime or 'unknown'} ({name}). "
            f"Accepted: images (png/jpg/gif/webp), PDF, text-based docs "
            f"(md/txt/csv/json/yaml/source code), or Excel (xlsx/xlsm).",
        )
    _t_read_start = time.perf_counter()
    body = await file.read()
    _t_read_end = time.perf_counter()
    if len(body) > _IMAGE_MAX_BYTES:
        raise HTTPException(413, f"file too large: {len(body)} bytes. "
                                  f"Max {_IMAGE_MAX_BYTES} bytes (~10MB)")
    aid = uuid.uuid4().hex
    entry: dict = {"kind": kind, "mime": mime, "name": name, "ts": time.time()}
    if kind == "text":
        if len(body) > _TEXT_MAX_BYTES:
            raise HTTPException(
                413,
                f"text file too large: {len(body)} bytes. Max "
                f"{_TEXT_MAX_BYTES} (~200 KB). Trim it or send as PDF.",
            )
        try:
            entry["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "text file is not valid UTF-8 — "
                                      "convert to UTF-8 or send as PDF") from None
    elif kind == "xlsx":
        # Convert to text up front; downstream chat code only inlines
        # entries whose kind is "text", so flip it before storing.
        entry["text"] = _xlsx_to_text(body, name)
        entry["kind"] = "text"
        if len(entry["text"].encode("utf-8")) > _TEXT_MAX_BYTES * 2:
            # Higher cap for converted spreadsheets — the per-cell + row
            # ceilings already bound output size, this is just a hard
            # safety rail. 400 KB ≈ 10k rows of 8 short cols.
            raise HTTPException(
                413,
                "spreadsheet too large after conversion. Reduce rows / "
                "cols and re-upload, or send a CSV of just the slice "
                "you need.",
            )
    else:
        entry["b64"] = base64.b64encode(body).decode("ascii")
    _image_store[aid] = entry
    # Diagnostic timing — logs to journalctl so we can cross-reference
    # against the frontend's console.log when uploads feel slow. Splits
    # into "read body" (multipart parse + transfer-out-of-uvicorn) vs
    # "total" (incl. base64 / dict insert) so we know where the time
    # actually went on the server side.
    _t_end = time.perf_counter()
    _sys.stderr.write(
        f"[upload] kind={kind} mime={mime} bytes={len(body)} "
        f"name={name!r} read_ms={(_t_read_end - _t_read_start)*1000:.0f} "
        f"total_ms={(_t_end - _t0)*1000:.0f}\n")
    _sys.stderr.flush()
    return {"id": aid, "mime": mime, "bytes": len(body),
             "kind": entry["kind"], "name": name}


@router.get("/stream", dependencies=[Depends(require_token_query)])
async def stream(
    prompt: str = Query(default=""),
    token: str = Query(...),
    session_id: str = Query(...),
    model: str = Query(default=""),
    permission: str = Query(default="bypassPermissions"),
    image_ids: str = Query(default=""),
):
    # RECONNECT MODE: empty prompt + an active in-flight turn on this
    # session = subscribe to the existing TurnBroadcast for replay +
    # live tail. Frontend uses this after loadSession discovers that
    # `/sessions/{sid}/active` is true. No new query is sent to the SDK.
    if not prompt.strip():
        existing = _active_turns.get(session_id)
        if existing is None:
            async def _no_active_gen():
                yield {"event": "error",
                        "data": json.dumps({"error": "no active turn"})}
            return EventSourceResponse(_no_active_gen())
        return EventSourceResponse(
            _subscribe_broadcast(existing),
            headers={
                "Content-Encoding": "identity",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # NEW-TURN MODE: refuse if there's already an unfinished turn on
    # this session — otherwise the second turn would overwrite the
    # broadcast and the user would lose visibility into the first.
    # Frontend should either reconnect (empty prompt) or wait.
    if session_id in _active_turns and not _active_turns[session_id].done:
        async def _busy_gen():
            yield {"event": "error",
                    "data": json.dumps({"error": "previous turn still running"})}
        return EventSourceResponse(_busy_gen())
    # One-session-one-model: if the session already has a locked model,
    # that wins over whatever the frontend's dropdown happens to say. This
    # prevents the "I tried to switch but it didn't take" class of bugs and
    # avoids cross-vendor thinking-signature corruption.
    s = sess.get_session(session_id) or {}
    locked = (s.get("model") or "").strip()
    if locked:
        model_to_use = locked
    else:
        # Virgin session — frontend's choice gets persisted on first send.
        model_to_use = model or MODEL
        sess.update_model(session_id, model_to_use)

    # Effort is per-session; read from metadata (settable via PATCH). Empty
    # string = SDK adaptive default, which is what the existing behavior was.
    effort_to_use = (s.get("effort") or "").strip()
    # Wrap get_client so SDK / auth pre-check errors surface as SSE error
    # events instead of bubbling up as a 500 (which the frontend can only
    # render as the generic "stream connection failed" toast).
    try:
        client = await get_client(session_id, model_to_use, permission,
                                    effort=effort_to_use)
    except Exception as e:
        err_msg = str(e) or f"{type(e).__name__}"
        async def _early_err_gen():
            yield {"event": "error", "data": json.dumps({"error": err_msg})}
        return EventSourceResponse(_early_err_gen())

    # Pull attachments from the in-memory store; build content blocks for the
    # SDK. Consume them — same attachment won't be re-sent on retry.
    img_blocks: list[dict] = []
    pdf_blocks: list[dict] = []
    text_attachments: list[tuple[str, str]] = []   # (name, content)
    persisted_imgs: list[dict] = []
    persisted_docs: list[dict] = []
    if image_ids:
        for aid in [x.strip() for x in image_ids.split(",") if x.strip()]:
            entry = _image_store.pop(aid, None)
            if entry is None:
                continue   # expired or unknown — silently skip
            kind = entry.get("kind", "image")
            if kind == "image":
                img_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": entry["mime"],
                        "data": entry["b64"],
                    },
                })
                persisted_imgs.append({"mime": entry["mime"]})
            elif kind == "pdf":
                pdf_blocks.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": entry["b64"],
                    },
                })
                persisted_docs.append({"name": entry.get("name", "doc.pdf"),
                                        "kind": "pdf"})
            elif kind == "text":
                text_attachments.append((entry.get("name", "file.txt"),
                                          entry["text"]))
                persisted_docs.append({"name": entry.get("name", "file.txt"),
                                        "kind": "text"})

    # Prepend inline text attachments to the prompt (any model can consume).
    if text_attachments:
        parts = [prompt] if prompt else []
        for name, body in text_attachments:
            parts.append(f"\n\n--- Attached file: {name} ---\n```\n{body}\n```\n--- end {name} ---")
        prompt = "\n".join(parts).lstrip()

    # New architecture: CLI's JSONL is the transcript source-of-truth. We no
    # longer accumulate `persisted` into a parallel local store. Instead, after
    # the stream completes we ask SDK for the latest message UUIDs and write
    # per-message annotations (cost / model / images) keyed by those UUIDs.
    assistant_acc = ""
    # Mirror of frontend's per-bubble `acc`. Reset on tool_use (FE
    # closeAsst). Lets us tail-emit any TextBlock suffix the SDK didn't
    # send as text_delta — see TextBlock branch below for context.
    streamed_in_bubble = ""

    async def event_gen():
        nonlocal assistant_acc, streamed_in_bubble
        # Subscribe to the session's side-channel queue. The MCP ask_user_question
        # handler publishes here; we merge those events into the SSE stream so the
        # UI can render the question UI while the SDK tool handler is await-ing.
        side_q = register_session_queue(session_id)
        perm_q = perm.register_session_queue(session_id)
        merge_q: asyncio.Queue = asyncio.Queue()
        SENTINEL_DONE = object()

        async def pump_claude():
            """Pull from claude SDK response stream into the merge queue."""
            try:
                # Multimodal path when binary blocks (image/pdf) are present.
                # Text-only attachments were already inlined into `prompt`.
                binary_blocks = [*img_blocks, *pdf_blocks]
                if binary_blocks:
                    text_block = {"type": "text", "text": prompt}
                    content = [*binary_blocks, text_block]

                    async def gen():
                        yield {
                            "type": "user",
                            "message": {"role": "user", "content": content},
                        }
                    await client.query(gen())
                else:
                    await client.query(prompt)
                async for msg in client.receive_response():
                    await merge_q.put(("claude", msg))
                    if isinstance(msg, ResultMessage):
                        break
            except Exception as e:
                # Log the full exception type + message + traceback for diagnosis.
                # SDK transport errors / vendor 401s land here. Without this we
                # silently die and the user just sees "卡着，无法对话".
                import traceback
                import sys as _sys
                _sys.stderr.write(
                    f"[chat-stream] sid={session_id} model={model_to_use} "
                    f"exc={type(e).__name__}: {e}\n{traceback.format_exc()}\n")
                _sys.stderr.flush()
                await merge_q.put(("error", e))
            finally:
                await merge_q.put(("done", SENTINEL_DONE))

        async def pump_side_q(src_q):
            """Pull from a side channel (MCP tool / permission) into merge queue."""
            try:
                while True:
                    evt = await src_q.get()
                    await merge_q.put(("side", evt))
            except asyncio.CancelledError:
                pass

        # ====== message-type-specific handlers ======
        # Three nested async generators, one per SDK message type. They share
        # closure state (assistant_acc + streamed_in_bubble via nonlocal,
        # other locals read-only). Keeps the main loop a ~15-line dispatch
        # instead of a 200-line elif chain.

        async def _handle_stream_event(msg):
            """Token-by-token deltas → tiny text / thinking events. Fast
            feedback path; the AssistantMessage handler suppresses re-emit."""
            nonlocal assistant_acc, streamed_in_bubble
            ev = msg.event or {}
            if ev.get("type") != "content_block_delta":
                return
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                chunk = delta.get("text", "")
                if chunk:
                    assistant_acc += chunk
                    streamed_in_bubble += chunk
                    yield {"event": "text", "data": json.dumps({"text": chunk})}
            elif dt == "thinking_delta":
                chunk = delta.get("thinking", "")
                if chunk:
                    yield {"event": "thinking", "data": json.dumps({"text": chunk})}

        async def _handle_assistant_message(msg):
            """Per-turn AssistantMessage:
              1. Snapshot per-turn usage (msg.usage is raw Anthropic per-call
                 dict; populate sess_u truthfully for the context meter).
              2. Accumulate per-turn tokens into the global _stats (truth
                 for `/api/chat/usage`). ResultMessage.usage is cumulative
                 per session and would double-count, so we do it here.
              3. Iterate content blocks — tail-emit TextBlock suffix the
                 stream may have skipped; forward tool_use / tool_result.
            """
            nonlocal assistant_acc, streamed_in_bubble
            a_usage = getattr(msg, "usage", None) or {}
            if a_usage:
                in_t = int(a_usage.get("input_tokens", 0) or 0)
                cr_t = int(a_usage.get("cache_read_input_tokens", 0) or 0)
                cc_t = int(a_usage.get("cache_creation_input_tokens", 0) or 0)
                out_t = int(a_usage.get("output_tokens", 0) or 0)
                ctx_used = in_t + cr_t + cc_t
                # Per-turn accumulation into the global stats. We do this
                # here (not in ResultMessage) because ResultMessage.usage
                # is the cumulative-per-session value and would inflate
                # _stats quadratically on long sessions.
                _stats["total_input_tokens"]           += in_t
                _stats["total_output_tokens"]          += out_t
                _stats["total_cache_read_tokens"]      += cr_t
                _stats["total_cache_creation_tokens"]  += cc_t
                sess_u = _session_usage.setdefault(session_id, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "total_cost_usd": 0.0, "last_turn_at": 0.0,
                    "context_used": 0, "context_used_pct": 0.0,
                    "context_limit": 0,
                })
                # Prefer the SDK-authoritative limit set by a prior turn's
                # ResultMessage handler (real maxTokens, may be 1M). Fallback
                # to the hardcoded estimate on the very first turn before
                # any get_context_usage() has run.
                limit = sess_u.get("context_limit") or MODEL_CONTEXT_LIMITS.get(
                    model_to_use, DEFAULT_CONTEXT_LIMIT)
                sess_u["input_tokens"] = in_t
                sess_u["cache_read_tokens"] = cr_t
                sess_u["cache_creation_tokens"] = cc_t
                sess_u["output_tokens"] = out_t
                sess_u["context_used"] = ctx_used
                sess_u["context_used_pct"] = (
                    round(ctx_used / limit * 100, 1) if limit else 0.0)
                # Only write context_limit when it's still 0 (first turn).
                # Otherwise keep the SDK-authoritative value the prior turn's
                # ResultMessage handler wrote.
                if not sess_u.get("context_limit"):
                    sess_u["context_limit"] = limit
            for block in msg.content:
                if isinstance(block, TextBlock):
                    # Defensive tail-emit (see message_parser.py:279-290 — SDK
                    # forwards CLI stream events 1:1 in theory, but FE was
                    # observed truncating mid-word "CSS 变量切" 2026-05-18).
                    # Diagnostic log only fires when diff > 0 (no spam).
                    full = (getattr(block, "text", "") or "")
                    if full and full != streamed_in_bubble:
                        tail = (full[len(streamed_in_bubble):]
                                 if full.startswith(streamed_in_bubble)
                                 else full)
                        if tail:
                            import sys as _sys
                            _sys.stderr.write(
                                f"[chat-stream] sid={session_id} "
                                f"TextBlock tail-emit: streamed="
                                f"{len(streamed_in_bubble)} chars, "
                                f"block.text={len(full)} chars, "
                                f"emitting tail={len(tail)} chars "
                                f"(prefix_match="
                                f"{full.startswith(streamed_in_bubble)})\n")
                            _sys.stderr.flush()
                            assistant_acc += tail
                            streamed_in_bubble += tail
                            yield {"event": "text",
                                   "data": json.dumps({"text": tail})}
                elif isinstance(block, ThinkingBlock):
                    # Already streamed via thinking_delta events.
                    pass
                elif isinstance(block, ToolUseBlock):
                    yield {"event": "tool_use",
                           "data": json.dumps(_render_tool_use(block))}
                    # FE closeAsst()'s the bubble on tool_use; reset mirror.
                    streamed_in_bubble = ""
                elif isinstance(block, ToolResultBlock):
                    yield {"event": "tool_result",
                           "data": json.dumps(_render_tool_result(block))}

        async def _handle_result_message(msg):
            """ResultMessage = turn complete. Update cumulative cost / stats,
            write per-message sidecar annotations, bump session metadata, then
            yield the consolidated 'done' SSE event the FE awaits."""
            cost = getattr(msg, "total_cost_usd", None) or 0.0
            u = getattr(msg, "usage", {}) or {}
            # ResultMessage.usage is CUMULATIVE per session. Per-turn
            # token accumulation into _stats happens in
            # _handle_assistant_message; here we only record the
            # cumulative numbers for the SSE "done" payload (FE reads
            # them as a snapshot). Cost is per-turn (not cumulative),
            # so it's safe to += into _stats.
            in_t = int(u.get("input_tokens", 0) or 0)
            out_t = int(u.get("output_tokens", 0) or 0)
            cr_t = int(u.get("cache_read_input_tokens", 0)
                        or u.get("cache_read_tokens", 0) or 0)
            cc_t = int(u.get("cache_creation_input_tokens", 0)
                        or u.get("cache_creation_tokens", 0) or 0)
            _stats["total_cost_usd"] += cost
            _stats["total_messages"] += 1
            sess_u = _session_usage.setdefault(session_id, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "total_cost_usd": 0.0, "last_turn_at": 0.0,
                "context_used": 0, "context_used_pct": 0.0,
                "context_limit": 0,
            })
            sess_u["total_cost_usd"] += cost
            sess_u["last_turn_at"] = time.time()

            # Pull authoritative max-window from SDK so the meter reflects the
            # ACTUAL effective limit (which may be 1M for Pro/Max subscribers,
            # not the hardcoded 200K in MODEL_CONTEXT_LIMITS). One control
            # round-trip per turn — small price for accurate denominator.
            #
            # Third-party caveat: CLI's get_context_usage uses Claude's
            # tokenizer + doesn't know DeepSeek/GLM/MiniMax context windows,
            # so for those vendors we trust our hardcoded MODEL_CONTEXT_LIMITS
            # table instead. It's not perfect either but at least matches the
            # vendor's documented window. AssistantMessage.usage already
            # populated context_used / pct against that limit a few lines up.
            if endpoints.is_third_party(model_to_use):
                # Re-anchor context_limit to muselab's table in case a prior
                # turn (under a different model) left a Claude-style 1M
                # value behind.
                sess_u["context_limit"] = MODEL_CONTEXT_LIMITS.get(
                    model_to_use, DEFAULT_CONTEXT_LIMIT)
                # Recompute pct against the corrected limit.
                if sess_u["context_limit"]:
                    sess_u["context_used_pct"] = round(
                        sess_u.get("context_used", 0)
                        / sess_u["context_limit"] * 100, 1)
            else:
                try:
                    cu = await client.get_context_usage()
                    real_max = int(cu.get("maxTokens") or 0)
                    real_total = int(cu.get("totalTokens") or 0)
                    if real_max:
                        sess_u["context_limit"] = real_max
                    if real_total:
                        sess_u["context_used"] = real_total
                    if real_max and real_total:
                        sess_u["context_used_pct"] = round(
                            real_total / real_max * 100, 1)
                except Exception as _e:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[chat-stream] get_context_usage skipped for "
                        f"sid={session_id}: {type(_e).__name__}\n")

            # Sidecar annotations: pull latest transcript from SDK to find
            # new user/assistant UUIDs, then write cost / model / images /
            # docs against those rows in muselab's per-session sidecar.
            try:
                all_msgs = get_session_messages(session_id, directory=str(ROOT))
            except Exception:
                all_msgs = []
            new_asst_uuid = None
            new_user_uuid = None
            for sm in reversed(all_msgs):
                if sm.type == "assistant" and not new_asst_uuid:
                    new_asst_uuid = sm.uuid
                elif sm.type == "user" and not new_user_uuid:
                    new_user_uuid = sm.uuid
                if new_asst_uuid and new_user_uuid:
                    break
            if new_asst_uuid and assistant_acc:
                # ts (ms epoch) stamps the turn's completion time. The
                # frontend's turn-footer (.turn-footer in index.html)
                # reads it via fmtHM() → "HH:MM" under the last muse
                # block of the turn. Stored at ms granularity to match
                # JS Date.now() (the frontend writes the same ts onto
                # in-flight messages in _markDone; loading from sidecar
                # uses this one).
                sess.set_message_annotation(
                    session_id, new_asst_uuid,
                    cost=f"${cost:.4f}", model=model_to_use,
                    ts=int(time.time() * 1000))
            if new_user_uuid and (persisted_imgs or persisted_docs):
                sess.set_message_annotation(
                    session_id, new_user_uuid,
                    images=persisted_imgs or None,
                    docs=persisted_docs or None)
            # message_count = total transcript size; auto-rename from first
            # user message text if session is still auto-named.
            first_user_text = ""
            for sm in all_msgs:
                if sm.type == "user":
                    c = (sm.message or {}).get("content")
                    if isinstance(c, str):
                        first_user_text = c
                    elif isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "text":
                                first_user_text = b.get("text", "")
                                break
                    break
            # turn_count = real user prompts only. SDK's get_session_messages
            # claims to filter tool-use sidechain (parent_tool_use_id always
            # None) but actually returns *every* user-typed frame, including
            # the implicit ones that wrap tool_result blocks after an agent
            # tool call. We detect those by content shape: if every content
            # block is a tool_result, the frame is a sidechain echo, not a
            # real user message. Without this filter, a session with 45 real
            # prompts but heavy agent tool use shows up as 300+ turns.
            n_turns = sum(1 for sm in all_msgs if _is_real_user_prompt(sm))
            sess.bump_session(session_id, message_count=len(all_msgs),
                               turn_count=n_turns,
                               auto_rename_from=first_user_text or prompt)
            sess.update_model(session_id, model_to_use)
            # Web Push on turn-done. Gated by `notify_normal` setting so the
            # user can mute per-turn pushes independently from scheduler
            # pushes. Best-effort — never let push failure block the stream.
            # Body kept minimal per user preference: session name as title +
            # "Muse 已回复" as body. No reply preview — full text is one tap
            # away in the actual chat, and preview text often duplicates the
            # foreground tab the user is already looking at.
            if os.environ.get("MUSELAB_NOTIFY_NORMAL", "true").lower() != "false":
                try:
                    from . import push as _push
                    sname = ""
                    try:
                        for s in sess.list_sessions():
                            if s.get("id") == session_id:
                                sname = s.get("name", "")
                                break
                    except Exception:
                        pass
                    _push.send_to_all(
                        title=sname or "muselab",
                        body="💬 Muse 已回复",
                        url="/",
                        tag=f"turn-{session_id}",
                    )
                except Exception as e:
                    import sys as _sys
                    _sys.stderr.write(f"[chat] turn push failed: {e}\n")
            yield {"event": "done", "data": json.dumps({
                "duration_ms": getattr(msg, "duration_ms", None),
                "total_cost_usd": cost,
                "model": model_to_use,
                "stats": _stats,
                # turn_usage: cumulative (ResultMessage.usage). FE should
                # prefer session_usage.context_* for window display. Will be
                # removed once FE is fully migrated.
                "turn_usage": {
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "cache_read_tokens": cr_t,
                    "cache_creation_tokens": cc_t,
                },
                "session_usage": _session_usage[session_id],
                "budget_usd": _budget_usd(),
                "budget_used_pct": (
                    round(_stats["total_cost_usd"] / _budget_usd() * 100, 1)
                    if _budget_usd() > 0 else 0
                ),
            })}

        # event_gen is now driven by a detached background task (see
        # stream endpoint below), so the SSE generator doesn't cancel
        # these workers when the browser disconnects — they complete
        # naturally. 30-minute hard cap is applied to the outer
        # task, not here.
        claude_task = asyncio.create_task(pump_claude())
        side_task = asyncio.create_task(pump_side_q(side_q))
        perm_task = asyncio.create_task(pump_side_q(perm_q))

        try:
            while True:
                kind, payload = await merge_q.get()
                if kind == "side":
                    # Already shaped as {"event": "...", "data": "..."} — pass through.
                    yield payload
                    continue
                if kind == "error":
                    yield {"event": "error", "data": json.dumps({"error": str(payload)})}
                    break
                if kind == "done":
                    break
                # kind == "claude" — dispatch by SDK message type to the
                # per-type helper async generators defined above. Each
                # helper yields zero-or-more SSE events; we forward them.
                msg = payload
                if isinstance(msg, StreamEvent):
                    async for ev in _handle_stream_event(msg):
                        yield ev
                elif isinstance(msg, AssistantMessage):
                    async for ev in _handle_assistant_message(msg):
                        yield ev
                elif isinstance(msg, ResultMessage):
                    async for ev in _handle_result_message(msg):
                        yield ev
        except asyncio.CancelledError:
            yield {"event": "cancelled", "data": "{}"}
            raise
        finally:
            # event_gen runs as part of a detached background task now;
            # cleanup here runs after the task finishes naturally (or
            # the 30-min outer timeout fires and cancels us).
            side_task.cancel()
            perm_task.cancel()
            claude_task.cancel()
            unregister_session_queue(session_id)
            perm.unregister_session_queue(session_id)

    # Background-completion + reconnect-streaming design:
    #
    # Old: `event_gen()` was the SSE response generator directly.
    # Browser disconnect cancelled the generator, which cancelled
    # pump_claude, which cut off the SDK reply mid-stream.
    #
    # New: event_gen() runs as a DETACHED background task that publishes
    # every event it would have yielded into a per-session TurnBroadcast.
    # The HTTP response is just a subscriber that replays the buffer +
    # streams new events. A user closing their browser doesn't affect
    # the background task — it runs to completion (or 30-min timeout).
    # A second SSE request to the same session (reconnect) becomes
    # another subscriber and sees the full reply via replay + live tail.
    BG_TIMEOUT_S = 1800   # 30 minutes — see PR thread for rationale

    broadcast = TurnBroadcast(session_id=session_id, model=model_to_use)
    broadcast.user_text = prompt
    broadcast.user_images = list(persisted_imgs)
    broadcast.user_docs = list(persisted_docs)
    _active_turns[session_id] = broadcast
    # Persist an in-flight breadcrumb so a process crash / restart can
    # surface this turn to the user on next boot. Auto-dismiss any
    # stale entry for this sid — starting a new turn supersedes whatever
    # the previous process left behind.
    _write_active_turn_sidecar(broadcast)
    _interrupted_at_startup.pop(session_id, None)

    async def _pump_gen_to_broadcast():
        try:
            async with asyncio.timeout(BG_TIMEOUT_S):
                async for ev in event_gen():
                    broadcast.publish(ev)
        except asyncio.TimeoutError:
            import sys as _sys
            _sys.stderr.write(
                f"[chat] turn exceeded {BG_TIMEOUT_S}s (30min), aborting "
                f"sid={session_id}\n")
            _sys.stderr.flush()
            broadcast.publish({
                "event": "error",
                "data": json.dumps({"error": "turn exceeded 30min"}),
            })
        except Exception as e:
            import sys as _sys
            import traceback as _tb
            _sys.stderr.write(
                f"[chat] background turn crashed sid={session_id} "
                f"exc={type(e).__name__}: {e}\n{_tb.format_exc()}\n")
            _sys.stderr.flush()
            broadcast.publish({
                "event": "error",
                "data": json.dumps({"error": f"{type(e).__name__}: {e}"}),
            })
        finally:
            broadcast.finish()
            _active_turns.pop(session_id, None)
            # Turn reached a terminal state (success / error / timeout) inside
            # this process — drop the persistence breadcrumb so startup scan
            # doesn't surface it as "interrupted." Only an actual process death
            # (OOM kill / SIGKILL / power loss) leaves the sidecar on disk.
            _delete_active_turn_sidecar(session_id)

    asyncio.create_task(_pump_gen_to_broadcast())

    return EventSourceResponse(
        _subscribe_broadcast(broadcast),
        headers={
            "Content-Encoding": "identity",
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _subscribe_broadcast(broadcast: TurnBroadcast):
    """Yields buffered events first (replay), then live events as they
    publish, terminating on the broadcast's finish sentinel. A late
    subscriber (reconnecting browser) gets the complete history plus
    everything that arrives after.

    Atomicity note: `broadcast.subscribe()` adds the queue and
    `len(...)` snapshots the buffer length in one synchronous block
    (no `await` between them). asyncio is single-threaded and won't
    preempt — publishes that happen before subscribe are entirely in
    the buffer, publishes after go into BOTH the buffer and the queue.
    Slicing the buffer up to `snap_len` gives us exactly the "before"
    set, and the queue gives us exactly the "after" set, with no
    duplication and no missed events."""
    q = broadcast.subscribe()
    snap_len = len(broadcast.events)
    try:
        # Replay the buffered prefix (events published BEFORE we
        # subscribed — they're not in our queue).
        for i in range(snap_len):
            yield broadcast.events[i]
        # If the turn already finished before we subscribed, the queue
        # holds nothing but the None sentinel.
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield ev
    finally:
        broadcast.unsubscribe(q)


@router.get("/sessions/{sid}/active", dependencies=[Depends(require_token)])
def session_active_status(sid: str) -> dict:
    """Tell the frontend whether `sid` has an in-progress background
    turn. Used on session load to decide between "render JSONL history"
    and "open a reconnect SSE stream to follow the live tail."""
    b = _active_turns.get(sid)
    if not b:
        return {"active": False}
    return {
        "active": True,
        "model": b.model,
        "started_at": b.started_at,
        "events_so_far": len(b.events),
    }


# ====== interrupted turns (process-crash recovery) ======

@router.get("/interrupted-turns", dependencies=[Depends(require_token)])
def list_interrupted_turns() -> dict:
    """Returns turns that were in-flight when the previous muselab process
    died. Empty list on clean restart. Frontend reads this once per session
    boot and toasts the user — does NOT auto-resume (user decides whether
    the conversation is worth retrying)."""
    items = []
    for sid, data in _interrupted_at_startup.items():
        items.append({
            "sid": sid,
            "preview": data.get("user_text_preview") or "",
            "model": data.get("model") or "",
            "started_at": data.get("started_at") or 0,
        })
    # Most recent first — usually what the user remembers best
    items.sort(key=lambda x: x["started_at"], reverse=True)
    return {"turns": items}


@router.post("/interrupted-turns/{sid}/dismiss",
             dependencies=[Depends(require_token)])
def dismiss_interrupted_turn(sid: str) -> dict:
    """User clicked 'dismiss' (or opened the session and saw the history).
    Removes the in-memory entry AND deletes the disk sidecar so future
    restarts don't keep nagging about the same turn."""
    _interrupted_at_startup.pop(sid, None)
    _delete_active_turn_sidecar(sid)
    return {"ok": True}


# ====== ask_user_question answer endpoint ======

class AnswerReq(BaseModel):
    answers: dict[str, Any]  # question_text -> chosen label (str) or labels (list[str])


@router.post("/answer/{session_id}/{question_id}",
              dependencies=[Depends(require_token)])
async def submit_answer_api(session_id: str, question_id: str, req: AnswerReq) -> dict:
    """Frontend POSTs the user's button click here. Resolves the Future the
    ask_user_question tool handler is await-ing; the tool then returns a
    text result and the model continues."""
    if not submit_answer(session_id, question_id, req.answers):
        raise HTTPException(404, "no pending question with that id "
                                  "(may have timed out or been answered already)")
    return {"ok": True}


# ====== permission request decision endpoint ======

class PermissionDecisionReq(BaseModel):
    decision: str           # "allow" | "deny" | "always"
    message: str | None = None


@router.post("/permission/{session_id}/{request_id}",
              dependencies=[Depends(require_token)])
async def submit_permission_decision_api(
    session_id: str, request_id: str, req: PermissionDecisionReq
) -> dict:
    """Frontend POSTs Allow / Deny / Always-allow click here."""
    if not perm.submit_decision(session_id, request_id, req.decision, req.message):
        raise HTTPException(404, "no pending permission request with that id "
                                  "(may have timed out or been answered already)")
    return {"ok": True}




@router.get("/providers", dependencies=[Depends(require_token)])
def providers_list() -> dict:
    """Available model groups based on which provider API keys are configured."""
    groups = endpoints.available_groups()
    # Flatten to the {group, label, model} shape the frontend expects.
    flat = [{"group": g["group"], "label": i["label"], "model": i["model"]}
            for g in groups for i in g["items"]]
    return {"models": flat}
