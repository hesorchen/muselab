import os
import base64
import json
import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
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
from .settings import ROOT, MODEL, MCP_CONFIG_PATH
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
# LRU bookkeeping. Each CLI subprocess holds ~30-50 MB RSS; without a cap
# muselab leaks memory as users open more sessions. New clients append to
# the tail; on cache miss with len > cap, oldest gets disconnected.
_client_lru: list[tuple[str, str]] = []
_CLIENT_POOL_CAP = int(os.environ.get("MUSELAB_CLIENT_POOL_CAP", "3"))
_lock = asyncio.Lock()


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
def _budget_usd() -> float:
    try:
        return float(os.environ.get("MUSELAB_BUDGET_USD", "0") or 0)
    except ValueError:
        return 0.0


SYSTEM_PROMPT = (
    "You are a personal assistant for browsing and editing the user's "
    f"archive directory at {ROOT}. Be concise. Reply in Chinese unless asked.\n\n"
    "When you need to disambiguate the user's intent or have them pick from a "
    "small set of mutually exclusive options (2-4 choices), call the "
    "`mcp__muselab__ask_user_question` tool with structured questions. The UI "
    "will render clickable buttons. Prefer this over writing out the options as "
    "plain text and waiting for the user to retype an answer — it's faster for "
    "them. Do NOT use it for open-ended questions; use plain text reply for those."
)


async def get_client(session_id: str, model: str, permission: str = "bypassPermissions",
                     show_thinking: bool = False) -> ClaudeSDKClient:
    """Create or fetch a ClaudeSDKClient for a (session, model) pair. Switching
    model in the UI yields a fresh client; resume=session_id loads the same
    on-disk conversation history into the new model."""
    key = (session_id, model)
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
            if show_thinking:
                from claude_agent_sdk import ThinkingConfigEnabled
                budget = int(os.environ.get("MUSELAB_THINKING_BUDGET", "4000") or 4000)
                opts_kwargs["thinking"] = ThinkingConfigEnabled(budget_tokens=budget)
            # When permission_mode is not bypassPermissions, wire the SDK's
            # can_use_tool callback to muselab's permission_request UI bridge.
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
                client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts_kwargs))
                await client.connect()
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
                out.append({"role": "thinking", "text": block.get("thinking", ""),
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
    transcript so the UI gets one flat list of bubbles."""
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
    return {**meta, "messages": messages}


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
    if req.model is not None:
        # Model switch is allowed any time — including mid-session. The next
        # turn will use the new model (frontend captures `streamingModel`
        # per-request so old bubbles keep their original model badge).
        # Caveats (frontend warns about cross-vendor):
        #   - cross-vendor switches can hit thinking-signature errors on the
        #     next reply if the prior turn had thinking blocks
        #   - prompt cache resets when model changes (first turn slower)
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
                    new_key = (sid, req.model)
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
    key = (session_id, m)
    if key not in _clients:
        # No live client → can't ask CLI for breakdown. Surface this rather
        # than returning fake data; frontend can fall back to /usage.
        raise HTTPException(409, "no live client for this session — send a message first")
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
    client = await get_client(sid, model, "bypassPermissions", show_thinking=False)
    try:
        await client.query("/compact")
        async for _ in client.receive_response():
            pass
    except Exception as e:
        raise HTTPException(500, f"native /compact failed: {e}")
    # Refresh message_count so the sidebar reflects the compacted size.
    try:
        new_msgs = get_session_messages(sid, directory=str(ROOT))
        sess.bump_session(sid, message_count=len(new_msgs))
    except Exception:
        pass
    return {"ok": True}


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
_TEXT_MAX_BYTES = 200 * 1024            # inline at most 200 KB as text


def _gc_images() -> None:
    """Drop entries older than TTL."""
    cutoff = time.time() - _IMAGE_TTL_S
    for k in list(_image_store.keys()):
        if _image_store[k]["ts"] < cutoff:
            del _image_store[k]


def _classify_attachment(mime: str, name: str) -> str:
    """Return one of: 'image' / 'pdf' / 'text' / '' (unsupported)."""
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
    return ""


@router.post("/upload-image", dependencies=[Depends(require_token)])
async def upload_image(file: UploadFile = File(...)) -> dict:
    """Legacy endpoint name; now handles images + PDF + text-ish docs."""
    _gc_images()
    mime = (file.content_type or "").lower()
    name = file.filename or "upload"
    kind = _classify_attachment(mime, name)
    if not kind:
        raise HTTPException(
            400,
            f"unsupported file type: {mime or 'unknown'} ({name}). "
            f"Accepted: images (png/jpg/gif/webp), PDF, or text-based docs "
            f"(md/txt/csv/json/yaml/source code).",
        )
    body = await file.read()
    if kind == "text" and len(body) > _TEXT_MAX_BYTES:
        raise HTTPException(
            413,
            f"text file too large: {len(body)} bytes. Max {_TEXT_MAX_BYTES} "
            f"(~200 KB). Trim it or convert to PDF.",
        )
    if len(body) > _IMAGE_MAX_BYTES:
        raise HTTPException(413, f"file too large: {len(body)} bytes. "
                                  f"Max {_IMAGE_MAX_BYTES} bytes (~10MB)")
    aid = uuid.uuid4().hex
    entry: dict = {"kind": kind, "mime": mime, "name": name, "ts": time.time()}
    if kind == "text":
        try:
            entry["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "text file is not valid UTF-8 — "
                                      "convert to UTF-8 or send as PDF") from None
    else:
        entry["b64"] = base64.b64encode(body).decode("ascii")
    _image_store[aid] = entry
    return {"id": aid, "mime": mime, "bytes": len(body),
             "kind": kind, "name": name}


@router.get("/stream", dependencies=[Depends(require_token_query)])
async def stream(
    prompt: str = Query(...),
    token: str = Query(...),
    session_id: str = Query(...),
    model: str = Query(default=""),
    permission: str = Query(default="bypassPermissions"),
    show_thinking: bool = Query(default=False),
    image_ids: str = Query(default=""),
):
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

    # Wrap get_client so SDK / auth pre-check errors surface as SSE error
    # events instead of bubbling up as a 500 (which the frontend can only
    # render as the generic "stream connection failed" toast).
    try:
        client = await get_client(session_id, model_to_use, permission, show_thinking)
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
                import traceback, sys as _sys
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
              2. Iterate content blocks — tail-emit TextBlock suffix the
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
            in_t = int(u.get("input_tokens", 0) or 0)
            out_t = int(u.get("output_tokens", 0) or 0)
            cr_t = int(u.get("cache_read_input_tokens", 0)
                        or u.get("cache_read_tokens", 0) or 0)
            cc_t = int(u.get("cache_creation_input_tokens", 0)
                        or u.get("cache_creation_tokens", 0) or 0)
            _stats["total_cost_usd"] += cost
            _stats["total_messages"] += 1
            _stats["total_input_tokens"] += in_t
            _stats["total_output_tokens"] += out_t
            _stats["total_cache_read_tokens"] += cr_t
            _stats["total_cache_creation_tokens"] += cc_t

            # Per-turn input/cache_* + context_used populated from
            # AssistantMessage.usage in _handle_assistant_message above.
            # ResultMessage.usage is CUMULATIVE per session — if we += it
            # into _stats we'd double-count (known bug, out-of-scope here).
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
                sess.set_message_annotation(
                    session_id, new_asst_uuid,
                    cost=f"${cost:.4f}", model=model_to_use)
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
            sess.bump_session(session_id, message_count=len(all_msgs),
                               auto_rename_from=first_user_text or prompt)
            sess.update_model(session_id, model_to_use)
            limit = MODEL_CONTEXT_LIMITS.get(model_to_use, DEFAULT_CONTEXT_LIMIT)
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
            side_task.cancel()
            perm_task.cancel()
            claude_task.cancel()
            unregister_session_queue(session_id)
            perm.unregister_session_queue(session_id)

    return EventSourceResponse(event_gen())


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
