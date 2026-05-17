import os
import base64
import json
import asyncio
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
_lock = asyncio.Lock()

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

# Approximate context window per model (input + output cap). Used to render
# the meter as a percentage. Conservative defaults — adjust as vendors update.
MODEL_CONTEXT_LIMITS = {
    "claude-opus-4-7":            200_000,
    "claude-sonnet-4-6":          200_000,
    "claude-haiku-4-5-20251001":  200_000,
    "deepseek-v4-pro":            128_000,
    "deepseek-v4-flash":          128_000,
    "deepseek-chat":              128_000,
    "deepseek-reasoner":          128_000,
    "glm-5":                      128_000,
    "minimax-m2.7":               245_000,
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
            opts_kwargs = dict(
                cwd=str(ROOT),
                model=model,
                permission_mode=permission,
                system_prompt=sp,
                resume=session_id,
                max_buffer_size=max_buf,
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
                    cfg = json.loads(MCP_CONFIG_PATH.read_text())
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
            except Exception:
                # resume failed (probably no prior on-disk session). Retry with
                # session_id to create a fresh one tied to our id.
                opts_kwargs.pop("resume", None)
                opts_kwargs["session_id"] = session_id
                client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts_kwargs))
                await client.connect()
            _clients[key] = client
        return _clients[key]


async def disconnect_client(session_id: str) -> None:
    """Disconnect every cached client for this session (across all models)."""
    async with _lock:
        keys = [k for k in _clients if k[0] == session_id]
        for k in keys:
            c = _clients.pop(k, None)
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


@router.get("/sessions/{sid}", dependencies=[Depends(require_token)])
def get_session_api(sid: str) -> dict:
    s = sess.get_session(sid)
    if s is None:
        raise HTTPException(404, "session not found")
    return s


@router.delete("/sessions/{sid}", dependencies=[Depends(require_token)])
async def delete_session_api(sid: str) -> dict:
    await disconnect_client(sid)
    if not sess.delete_session(sid):
        raise HTTPException(404, "session not found")
    return {"ok": True}


@router.delete("/sessions", dependencies=[Depends(require_token)])
async def cleanup_empty_sessions() -> dict:
    """Bulk-remove all sessions with zero messages (placeholder / abandoned).
    Exposed in Settings as "清理空会话"."""
    # Drop in-memory clients for any of them too
    removed = sess.delete_empty_sessions()
    for sid in removed:
        await disconnect_client(sid)
    return {"ok": True, "removed": removed, "count": len(removed)}


class SessionPatchReq(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    model: str | None = None


@router.patch("/sessions/{sid}", dependencies=[Depends(require_token)])
async def patch_session_api(sid: str, req: SessionPatchReq) -> dict:
    ok = False
    if req.name is not None:
        ok = sess.rename_session(sid, req.name) or ok
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
        # Free the old (sid, oldmodel) client so the next send uses the new model
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
    """Per-session context meter — what fraction of the model's window we're at."""
    u = _session_usage.get(session_id, {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "total_cost_usd": 0.0, "last_turn_at": 0.0,
    })
    m = model or MODEL
    limit = MODEL_CONTEXT_LIMITS.get(m, DEFAULT_CONTEXT_LIMIT)
    # Real context window usage = input + cache_read + cache_creation.
    # With prompt caching, replayed history shows up under cache_read, not
    # input_tokens — using input alone makes the meter "shrink" between turns
    # when really the window is still mostly full.
    ctx_used = u["input_tokens"] + u.get("cache_read_tokens", 0) + u.get("cache_creation_tokens", 0)
    return {
        **u,
        "model": m,
        "context_limit": limit,
        "context_used": ctx_used,
        "context_used_pct": round(ctx_used / limit * 100, 1) if limit else 0,
    }


class SeedReq(BaseModel):
    summary: str


@router.post("/sessions/{sid}/seed", dependencies=[Depends(require_token)])
async def seed_session_api(sid: str, req: SeedReq) -> dict:
    """Persist a summary as the first user message of a (typically just-created
    compact) session. Used by the /compact flow: model summarizes the old
    session, that summary becomes context for the fresh one."""
    s = sess.get_session(sid)
    if s is None:
        raise HTTPException(404, "session not found")
    if s.get("messages"):
        raise HTTPException(409, "session already has messages; seed only fits empty")
    sess.append_messages(sid, [{
        "role": "user",
        "text": f"以下是上一个会话的全部要点摘要，请把它作为我们继续对话的起点：\n\n{req.summary}",
    }])
    return {"ok": True}


@router.post("/sessions/{sid}/compact", dependencies=[Depends(require_token)])
async def compact_session_api(sid: str) -> dict:
    """Compact a session: ask the model to produce a structured summary of the
    full history, then create a NEW session whose first message is that summary
    (preserving model + system_prompt). Original session is untouched so the
    user can compare. Returns the new session metadata.

    Note: this is a stateless server-side compaction trigger — the actual
    summary is generated by the next streamed turn the frontend kicks off with
    a special prompt. The backend just creates the empty fork."""
    src = sess.get_session(sid)
    if src is None:
        raise HTTPException(404, "session not found")
    name = f"{src.get('name', 'session')} (compact)"
    new_meta = sess.create_session(name=name, model=src.get("model", ""),
                                    system_prompt=src.get("system_prompt", ""))
    return new_meta


class BudgetReq(BaseModel):
    budget_usd: float       # 0 = disabled


@router.get("/context-info", dependencies=[Depends(require_token)])
def context_info() -> dict:
    """Information about what Muse can see — used by the UI's onboarding
    hints (does the user have a CLAUDE.md? archive empty? skills loaded?
    has any working auth?). All paths relative to ROOT for safety."""
    claude_md = ROOT / "CLAUDE.md"
    # Detect "do we have ANY working auth?" — needed so the chat-empty card
    # can warn "you have no provider set up; configure one before chatting".
    # Claude OAuth lives in ~/.claude/.credentials.json (Pro/Max).
    claude_oauth = (Path.home() / ".claude" / ".credentials.json").exists()
    third_party_configured = [
        name for env_key, name in (
            ("DEEPSEEK_API_KEY", "DeepSeek"),
            ("ZHIPUAI_API_KEY",  "GLM"),
            ("MINIMAX_API_KEY",  "MiniMax"),
        ) if os.environ.get(env_key)
    ]
    info: dict = {
        "archive_root": str(ROOT),
        "claude_md_exists": claude_md.exists(),
        "claude_md_lines": 0,
        "claude_md_mtime": 0.0,
        "archive_empty": True,
        "subdir_present": {},
        "has_claude_oauth": claude_oauth,
        "third_party_configured": third_party_configured,
        "has_any_provider": claude_oauth or len(third_party_configured) > 0,
    }
    if claude_md.exists():
        try:
            info["claude_md_lines"] = sum(1 for _ in claude_md.open(encoding="utf-8", errors="replace"))
            info["claude_md_mtime"] = claude_md.stat().st_mtime
        except OSError:
            pass
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
        cfg = json.loads(MCP_CONFIG_PATH.read_text())
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

    out: dict = {"name": name, "summary": summary, "id": block.id}
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

    client = await get_client(session_id, model_to_use, permission, show_thinking)

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

    persisted_user = {"role": "user", "text": prompt}
    if persisted_imgs:
        persisted_user["images"] = persisted_imgs
    if persisted_docs:
        persisted_user["docs"] = persisted_docs
    persisted: list[dict] = [persisted_user]
    assistant_acc = ""

    async def event_gen():
        nonlocal assistant_acc
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
                # kind == "claude"
                msg = payload
                if isinstance(msg, StreamEvent):
                    # Token-by-token deltas — emit each as a tiny text/thinking event.
                    # This is the fast-feedback path; the consolidated AssistantMessage
                    # below carries the final text but we suppress re-emit there.
                    ev = msg.event or {}
                    et = ev.get("type")
                    if et == "content_block_delta":
                        delta = ev.get("delta") or {}
                        dt = delta.get("type")
                        if dt == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                assistant_acc += chunk
                                yield {"event": "text", "data": json.dumps({"text": chunk})}
                        elif dt == "thinking_delta":
                            chunk = delta.get("thinking", "")
                            if chunk:
                                yield {"event": "thinking", "data": json.dumps({"text": chunk})}
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            # Already streamed via StreamEvent deltas above —
                            # don't re-emit (would double the assistant bubble).
                            pass
                        elif isinstance(block, ThinkingBlock):
                            # Same: streamed via thinking_delta events.
                            pass
                        elif isinstance(block, ToolUseBlock):
                            d = _render_tool_use(block)
                            persisted.append({"role": "tool_use", **d})
                            yield {"event": "tool_use", "data": json.dumps(d)}
                        elif isinstance(block, ToolResultBlock):
                            d = _render_tool_result(block)
                            persisted.append({"role": "tool_result", **d})
                            yield {"event": "tool_result", "data": json.dumps(d)}
                elif isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    u = getattr(msg, "usage", {}) or {}
                    in_t   = int(u.get("input_tokens", 0) or 0)
                    out_t  = int(u.get("output_tokens", 0) or 0)
                    cr_t   = int(u.get("cache_read_input_tokens", 0)
                                  or u.get("cache_read_tokens", 0) or 0)
                    cc_t   = int(u.get("cache_creation_input_tokens", 0)
                                  or u.get("cache_creation_tokens", 0) or 0)
                    _stats["total_cost_usd"] += cost
                    _stats["total_messages"] += 1
                    _stats["total_input_tokens"]  += in_t
                    _stats["total_output_tokens"] += out_t
                    _stats["total_cache_read_tokens"]     += cr_t
                    _stats["total_cache_creation_tokens"] += cc_t

                    # Snapshot per-session usage. input_tokens of the most-recent
                    # turn ≈ current context window occupancy, which is what the
                    # meter shows.
                    sess_u = _session_usage.setdefault(session_id, {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0,
                        "total_cost_usd": 0.0, "last_turn_at": 0.0,
                    })
                    sess_u["input_tokens"] = in_t        # current context, not accumulated
                    sess_u["output_tokens"] += out_t
                    sess_u["cache_read_tokens"] += cr_t
                    sess_u["cache_creation_tokens"] += cc_t
                    sess_u["total_cost_usd"] += cost
                    sess_u["last_turn_at"] = time.time()

                    if assistant_acc:
                        persisted.append({
                            "role": "assistant",
                            "text": assistant_acc,
                            "cost": f"${cost:.4f}",
                            # Persist which model produced this reply, so the bubble
                            # badge stays accurate after session reload / model switch.
                            # Without this, switching models silently loses provenance.
                            "model": model_to_use,
                        })
                    sess.append_messages(session_id, persisted)
                    sess.update_model(session_id, model_to_use)
                    limit = MODEL_CONTEXT_LIMITS.get(model_to_use, DEFAULT_CONTEXT_LIMIT)
                    yield {"event": "done", "data": json.dumps({
                        "duration_ms": getattr(msg, "duration_ms", None),
                        "total_cost_usd": cost,
                        "model": model_to_use,
                        "stats": _stats,
                        "turn_usage": {
                            "input_tokens": in_t,
                            "output_tokens": out_t,
                            "cache_read_tokens": cr_t,
                            "cache_creation_tokens": cc_t,
                        },
                        "session_usage": {
                            **_session_usage[session_id],
                            "context_limit": limit,
                            "context_used": in_t + cr_t + cc_t,
                            "context_used_pct": round((in_t + cr_t + cc_t) / limit * 100, 1) if limit else 0,
                        },
                        "budget_usd": _budget_usd(),
                        "budget_used_pct": (
                            round(_stats["total_cost_usd"] / _budget_usd() * 100, 1)
                            if _budget_usd() > 0 else 0
                        ),
                    })}
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
