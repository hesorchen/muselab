import os
import json
import asyncio
from fastapi import APIRouter, Depends, Query, HTTPException
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ThinkingBlock, ResultMessage,
    ToolUseBlock, ToolResultBlock,
)
from .auth import require_token_query, require_token
from .settings import ROOT, MODEL, MCP_CONFIG_PATH
from . import sessions as sess
from . import endpoints

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Clients keyed by (session_id, model) — model is part of the key so that
# switching model mid-session creates a fresh client for that model (which
# uses resume=session_id to inherit the conversation history from disk).
_clients: dict[tuple[str, str], ClaudeSDKClient] = {}
_lock = asyncio.Lock()

# aggregate
_stats = {"total_cost_usd": 0.0, "total_messages": 0,
          "total_input_tokens": 0, "total_output_tokens": 0}


SYSTEM_PROMPT = (
    "You are a personal assistant for browsing and editing the user's "
    f"archive directory at {ROOT}. Be concise. Reply in Chinese unless asked."
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
            opts_kwargs = dict(
                cwd=str(ROOT),
                model=model,
                permission_mode=permission,
                system_prompt=sp,
                resume=session_id,
            )
            # Optional model params from env (UI-editable via /api/settings).
            mt = int(os.environ.get("MUSELAB_MAX_TURNS", "0") or 0)
            if mt > 0:
                opts_kwargs["max_turns"] = mt
            # For non-Claude models, point the SDK at the vendor's own
            # Anthropic-compatible endpoint (DeepSeek / GLM / MiniMax / Kimi).
            # This way the SDK's full agent loop (tools, MCP, skills, CLAUDE.md)
            # works uniformly across providers — no router process needed.
            # Claude models still go direct so Pro OAuth keeps working.
            env_ovr = endpoints.env_override(model)
            if env_ovr is not None:
                opts_kwargs["env"] = env_ovr
            if MCP_CONFIG_PATH is not None:
                opts_kwargs["mcp_servers"] = str(MCP_CONFIG_PATH)
            if show_thinking:
                from claude_agent_sdk import ThinkingConfigEnabled
                budget = int(os.environ.get("MUSELAB_THINKING_BUDGET", "4000") or 4000)
                opts_kwargs["thinking"] = ThinkingConfigEnabled(budget_tokens=budget)
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


class SessionPatchReq(BaseModel):
    name: str | None = None
    system_prompt: str | None = None


@router.patch("/sessions/{sid}", dependencies=[Depends(require_token)])
async def patch_session_api(sid: str, req: SessionPatchReq) -> dict:
    ok = False
    if req.name is not None:
        ok = sess.rename_session(sid, req.name) or ok
    if req.system_prompt is not None:
        ok = sess.update_system_prompt(sid, req.system_prompt) or ok
        # System prompt change invalidates cached SDK clients for this session.
        await disconnect_client(sid)
    if not ok:
        raise HTTPException(404, "session not found or no changes")
    return {"ok": True}


# ====== usage / reset ======

@router.get("/usage", dependencies=[Depends(require_token)])
def usage() -> dict:
    return {**_stats, "model_default": MODEL,
            "active_sessions": list(_clients.keys())}


@router.get("/mcp", dependencies=[Depends(require_token)])
def mcp_status() -> dict:
    """Return configured MCP servers (parsed from mcp.json) for UI display."""
    if MCP_CONFIG_PATH is None:
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
    else:
        summary = json.dumps(inp, ensure_ascii=False)[:200]
    return {"name": name, "summary": summary, "id": block.id}


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


@router.get("/stream", dependencies=[Depends(require_token_query)])
async def stream(
    prompt: str = Query(...),
    token: str = Query(...),
    session_id: str = Query(...),
    model: str = Query(default=""),
    permission: str = Query(default="bypassPermissions"),
    show_thinking: bool = Query(default=False),
):
    model_to_use = model or MODEL

    client = await get_client(session_id, model_to_use, permission, show_thinking)

    # buffer of frontend-format messages to persist
    persisted: list[dict] = [{"role": "user", "text": prompt}]
    assistant_acc = ""

    async def event_gen():
        nonlocal assistant_acc
        try:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            assistant_acc += block.text
                            yield {"event": "text", "data": json.dumps({"text": block.text})}
                        elif isinstance(block, ThinkingBlock):
                            yield {"event": "thinking", "data": json.dumps({"text": block.thinking})}
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
                    _stats["total_cost_usd"] += cost
                    _stats["total_messages"] += 1
                    _stats["total_input_tokens"] += int(u.get("input_tokens", 0) or 0)
                    _stats["total_output_tokens"] += int(u.get("output_tokens", 0) or 0)
                    # persist assistant message
                    if assistant_acc:
                        persisted.append({
                            "role": "assistant",
                            "text": assistant_acc,
                            "cost": f"${cost:.4f}",
                        })
                    sess.append_messages(session_id, persisted)
                    sess.update_model(session_id, model_to_use)
                    yield {"event": "done", "data": json.dumps({
                        "duration_ms": getattr(msg, "duration_ms", None),
                        "total_cost_usd": cost,
                        "model": model_to_use,
                        "stats": _stats,
                    })}
                    break
        except asyncio.CancelledError:
            yield {"event": "cancelled", "data": "{}"}
            raise
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(event_gen())




@router.get("/providers", dependencies=[Depends(require_token)])
def providers_list() -> dict:
    """Available model groups based on which provider API keys are configured."""
    groups = endpoints.available_groups()
    # Flatten to the {group, label, model} shape the frontend expects.
    flat = [{"group": g["group"], "label": i["label"], "model": i["model"]}
            for g in groups for i in g["items"]]
    return {"models": flat}
