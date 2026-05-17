"""Runtime-editable settings: provider API keys, defaults, model params.
GET returns current values with keys masked. PUT atomically rewrites .env and
refreshes os.environ so the changes take effect without restarting the server.
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from . import endpoints

MCP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp.json"
MCP_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "mcp.json.example"

router = APIRouter(prefix="/api/settings", tags=["settings"])

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Providers exposed in the settings UI. Only list厂商 with verified Anthropic-
# compatible Messages API endpoints (so Claude SDK can call them directly).
# 小米 MiMo / Qwen / Doubao 暂只支持 OpenAI 协议，等他们出 /anthropic 端点再加。
PROVIDER_KEYS = [
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("ZHIPUAI_API_KEY", "智谱 GLM"),
    ("MINIMAX_API_KEY", "MiniMax"),
]

DEFAULT_KEYS = [
    "MUSELAB_DEFAULT_MODEL",
    "MUSELAB_DEFAULT_PERMISSION",
    "MUSELAB_DEFAULT_SHOW_THINKING",
    "MUSELAB_THINKING_BUDGET",
    "MUSELAB_MAX_TURNS",
]


def _mask(v: str) -> str:
    """Mask an API key for display. Show first 4 + last 4 chars only."""
    if not v:
        return ""
    if len(v) <= 10:
        return "•" * len(v)
    return v[:4] + "•" * (len(v) - 8) + v[-4:]


class SettingsIn(BaseModel):
    # Provider keys — empty string = "don't change", explicit null = remove.
    deepseek_api_key: str | None = None
    zhipuai_api_key: str | None = None
    minimax_api_key: str | None = None
    # Defaults
    default_model: str | None = None
    default_permission: str | None = None
    default_show_thinking: bool | None = None
    # Params
    thinking_budget: int | None = None
    max_turns: int | None = None


def _write_env(updates: dict[str, str]) -> None:
    """Atomically merge updates into .env. Keys with empty-string value get
    written as `KEY=` (allowed); to actually remove a key, pass None and we
    drop the line."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    written: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_v = updates[key]
            if new_v is None:
                # drop line entirely
                continue
            out.append(f"{key}={new_v}")
            written.add(key)
        else:
            out.append(line)

    # Append new keys not seen above.
    for k, v in updates.items():
        if v is None or k in written:
            continue
        out.append(f"{k}={v}")

    # Atomic write via temp + rename.
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(ENV_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out).rstrip("\n") + "\n")
        os.replace(tmp, ENV_PATH)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

    # Refresh in-process env so the change takes effect immediately.
    for k, v in updates.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@router.get("", dependencies=[Depends(require_token)])
def get_settings() -> dict:
    """Return current settings with API keys masked."""
    providers: list[dict] = []
    for key, display in PROVIDER_KEYS:
        v = os.environ.get(key, "")
        providers.append({
            "env_key": key,
            "display": display,
            "configured": bool(v),
            "masked": _mask(v),
        })
    return {
        "providers": providers,
        "defaults": {
            "model": os.environ.get("MUSELAB_DEFAULT_MODEL", os.environ.get("MUSELAB_MODEL", "claude-sonnet-4-6")),
            "permission": os.environ.get("MUSELAB_DEFAULT_PERMISSION", "bypassPermissions"),
            "show_thinking": os.environ.get("MUSELAB_DEFAULT_SHOW_THINKING", "false").lower() == "true",
        },
        "params": {
            "thinking_budget": int(os.environ.get("MUSELAB_THINKING_BUDGET", "4000")),
            "max_turns": int(os.environ.get("MUSELAB_MAX_TURNS", "0")),
        },
    }


@router.put("", dependencies=[Depends(require_token)])
def put_settings(req: SettingsIn) -> dict:
    """Write any provided fields to .env and refresh os.environ in-process."""
    updates: dict[str, str] = {}

    # Provider keys: empty string means "keep current"; we ignore them.
    # Explicit non-empty string writes; "_delete_" sentinel removes.
    key_map = {
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "zhipuai_api_key": "ZHIPUAI_API_KEY",
        "minimax_api_key": "MINIMAX_API_KEY",
    }
    for field, env_name in key_map.items():
        v = getattr(req, field)
        if v is None or v == "":   # skip unchanged
            continue
        if v == "_delete_":
            updates[env_name] = None  # type: ignore[assignment]  # signals removal
        else:
            updates[env_name] = v

    if req.default_model is not None:
        updates["MUSELAB_DEFAULT_MODEL"] = req.default_model
    if req.default_permission is not None:
        updates["MUSELAB_DEFAULT_PERMISSION"] = req.default_permission
    if req.default_show_thinking is not None:
        updates["MUSELAB_DEFAULT_SHOW_THINKING"] = "true" if req.default_show_thinking else "false"
    if req.thinking_budget is not None:
        updates["MUSELAB_THINKING_BUDGET"] = str(req.thinking_budget)
    if req.max_turns is not None:
        updates["MUSELAB_MAX_TURNS"] = str(req.max_turns)

    _write_env(updates)
    return {"ok": True, "updated": list(updates.keys())}


# ====== MCP server management ======
#
# Storage shape on disk (mcp.json):
#   {"mcpServers": {"name": {"command": "...", "args": [...], "env": {...},
#                              "disabled": false}, ...}}
# `disabled` is a muselab-local field — when true, the server is omitted from
# the dict we hand to ClaudeAgentOptions so the SDK doesn't connect to it.


class MCPServerSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    command: str = Field(..., min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False


def _load_mcp() -> dict:
    if not MCP_CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        d = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"mcpServers": {}}
        d.setdefault("mcpServers", {})
        return d
    except (json.JSONDecodeError, OSError):
        return {"mcpServers": {}}


def _save_mcp(cfg: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix="mcp.", suffix=".json",
                                dir=str(MCP_CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, MCP_CONFIG_PATH)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def _load_examples() -> list[dict]:
    """Return preset/example servers from mcp.json.example, if present."""
    if not MCP_EXAMPLE_PATH.exists():
        return []
    try:
        d = json.loads(MCP_EXAMPLE_PATH.read_text(encoding="utf-8"))
        items = []
        for name, spec in (d.get("mcpServers") or {}).items():
            items.append({
                "name": name,
                "command": spec.get("command", ""),
                "args": spec.get("args", []),
                "env": spec.get("env", {}),
                "description": spec.get("description", ""),
            })
        return items
    except (json.JSONDecodeError, OSError):
        return []


@router.get("/mcp", dependencies=[Depends(require_token)])
def get_mcp_servers() -> dict:
    cfg = _load_mcp()
    servers = []
    for name, spec in (cfg.get("mcpServers") or {}).items():
        servers.append({
            "name": name,
            "command": spec.get("command", ""),
            "args": spec.get("args", []),
            "env": {k: _mask(v) for k, v in (spec.get("env") or {}).items()},
            "disabled": bool(spec.get("disabled", False)),
        })
    return {"servers": servers, "examples": _load_examples()}


@router.put("/mcp/{name}", dependencies=[Depends(require_token)])
def upsert_mcp_server(name: str, spec: MCPServerSpec) -> dict:
    """Create or replace one MCP server entry. Path `name` wins over body `name`."""
    cfg = _load_mcp()
    cfg["mcpServers"][name] = {
        "command": spec.command,
        "args": spec.args,
        "env": spec.env,
        "disabled": spec.disabled,
    }
    _save_mcp(cfg)
    return {"ok": True, "name": name}


@router.delete("/mcp/{name}", dependencies=[Depends(require_token)])
def delete_mcp_server(name: str) -> dict:
    cfg = _load_mcp()
    if name not in (cfg.get("mcpServers") or {}):
        raise HTTPException(404, f"MCP server not found: {name}")
    del cfg["mcpServers"][name]
    _save_mcp(cfg)
    return {"ok": True, "name": name}


class MCPToggleReq(BaseModel):
    disabled: bool


@router.patch("/mcp/{name}/toggle", dependencies=[Depends(require_token)])
def toggle_mcp_server(name: str, req: MCPToggleReq) -> dict:
    cfg = _load_mcp()
    if name not in (cfg.get("mcpServers") or {}):
        raise HTTPException(404, f"MCP server not found: {name}")
    cfg["mcpServers"][name]["disabled"] = req.disabled
    _save_mcp(cfg)
    return {"ok": True, "name": name, "disabled": req.disabled}


# ====== Skill discovery ======
#
# We don't enable/disable skills individually here — the SDK takes
# `skills="all"` or a list. We expose what's discoverable so users can browse.

SKILL_USER_DIR = Path.home() / ".claude" / "skills"
SKILL_PROJECT_DIR = Path(__file__).resolve().parent.parent / "skills"


def _parse_skill_md(p: Path) -> dict | None:
    """Parse frontmatter of a SKILL.md (or skill.md). Cheap, no YAML dep."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return {"name": p.parent.name, "description": ""}
    end = text.find("\n---", 3)
    if end == -1:
        return {"name": p.parent.name, "description": ""}
    fm = text[3:end].strip()
    out: dict = {"name": p.parent.name, "description": ""}
    cur_key = None
    cur_val: list[str] = []
    for line in fm.splitlines():
        if not line.strip():
            continue
        if line.startswith(" ") and cur_key:  # continuation
            cur_val.append(line.strip())
            continue
        if cur_key:
            out[cur_key] = " ".join(cur_val).strip().strip('"\'')
        if ":" in line:
            k, _, v = line.partition(":")
            cur_key = k.strip()
            cur_val = [v.strip()]
        else:
            cur_key = None
            cur_val = []
    if cur_key:
        out[cur_key] = " ".join(cur_val).strip().strip('"\'')
    return out


def _list_skills_in(dir_: Path, scope: str) -> list[dict]:
    if not dir_.exists() or not dir_.is_dir():
        return []
    out = []
    for child in sorted(dir_.iterdir()):
        if not child.is_dir():
            continue
        for fname in ("SKILL.md", "skill.md"):
            p = child / fname
            if p.exists():
                meta = _parse_skill_md(p) or {}
                out.append({
                    "name": meta.get("name") or child.name,
                    "description": meta.get("description", ""),
                    "scope": scope,
                    "path": str(child),
                })
                break
    return out


@router.get("/skills", dependencies=[Depends(require_token)])
def list_skills() -> dict:
    """List all skills discoverable by the SDK (user + project scopes)."""
    skills = (_list_skills_in(SKILL_PROJECT_DIR, "project") +
              _list_skills_in(SKILL_USER_DIR, "user"))
    return {"skills": skills}
