"""Runtime-editable settings: provider API keys, defaults, model params.
GET returns current values with keys masked. PUT atomically rewrites .env and
refreshes os.environ so the changes take effect without restarting the server.
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token

MCP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp.json"
MCP_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "mcp.json.example"

router = APIRouter(prefix="/api/settings", tags=["settings"])

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Providers exposed in the settings UI. Only list厂商 with verified Anthropic-
# compatible Messages API endpoints (so Claude SDK can call them directly).
# 小米 MiMo / Qwen / Doubao 暂只支持 OpenAI 协议，等他们出 /anthropic 端点再加。
PROVIDER_KEYS = [
    # Anthropic 排第一 — 是 muselab 的默认/推荐 provider。
    # 用户也可以走 `claude login`（Pro/Max OAuth），那种情况下这一栏可以留空。
    ("ANTHROPIC_API_KEY", "Anthropic (Claude API)"),
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("ZHIPUAI_API_KEY", "智谱 GLM"),
    ("MINIMAX_API_KEY", "MiniMax"),
]

DEFAULT_KEYS = [
    "MUSELAB_DEFAULT_MODEL",
    "MUSELAB_DEFAULT_PERMISSION",
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
    anthropic_api_key: str | None = None
    deepseek_api_key: str | None = None
    zhipuai_api_key: str | None = None
    minimax_api_key: str | None = None
    # Defaults
    default_model: str | None = None
    default_permission: str | None = None
    # Params
    thinking_budget: int | None = None
    max_turns: int | None = None
    # Push notification toggles — server-side decision on whether to
    # send_to_all for each event class. notify_scheduled covers the
    # scheduler.py daemon's task-done push; notify_normal covers
    # chat.py's per-turn done push.
    notify_scheduled: bool | None = None
    notify_normal: bool | None = None


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
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
        },
        "params": {
            "thinking_budget": int(os.environ.get("MUSELAB_THINKING_BUDGET", "4000")),
            "max_turns": int(os.environ.get("MUSELAB_MAX_TURNS", "0")),
            "notify_scheduled": (os.environ.get("MUSELAB_NOTIFY_SCHEDULED", "true").lower() != "false"),
            "notify_normal":    (os.environ.get("MUSELAB_NOTIFY_NORMAL",    "true").lower() != "false"),
        },
    }


@router.put("", dependencies=[Depends(require_token)])
def put_settings(req: SettingsIn) -> dict:
    """Write any provided fields to .env and refresh os.environ in-process."""
    updates: dict[str, str] = {}

    # Provider keys: empty string means "keep current"; we ignore them.
    # Explicit non-empty string writes; "_delete_" sentinel removes.
    key_map = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
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
    if req.thinking_budget is not None:
        updates["MUSELAB_THINKING_BUDGET"] = str(req.thinking_budget)
    if req.max_turns is not None:
        updates["MUSELAB_MAX_TURNS"] = str(req.max_turns)
    if req.notify_scheduled is not None:
        updates["MUSELAB_NOTIFY_SCHEDULED"] = "true" if req.notify_scheduled else "false"
    if req.notify_normal is not None:
        updates["MUSELAB_NOTIFY_NORMAL"] = "true" if req.notify_normal else "false"

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
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
async def toggle_mcp_server(name: str, req: MCPToggleReq) -> dict:
    cfg = _load_mcp()
    if name not in (cfg.get("mcpServers") or {}):
        raise HTTPException(404, f"MCP server not found: {name}")
    cfg["mcpServers"][name]["disabled"] = req.disabled
    _save_mcp(cfg)
    # Apply to every live SDK client too — otherwise the toggle would only
    # take effect on the next client rebuild, leaving the user staring at a
    # toggled-off server that still answers tool calls. SDK exposes
    # client.toggle_mcp_server() for exactly this case.
    from . import chat as _chat
    enabled = not req.disabled
    propagated: list[str] = []
    errors: list[str] = []
    async with _chat._lock:
        live = list(_chat._clients.items())
    for key, client in live:
        try:
            await client.toggle_mcp_server(name, enabled)
            propagated.append(f"{key[0]}@{key[1]}")
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {e}")
    return {
        "ok": True, "name": name, "disabled": req.disabled,
        "propagated": propagated,
        "errors": errors,
    }


@router.post("/mcp/{name}/reconnect", dependencies=[Depends(require_token)])
async def reconnect_mcp_server(name: str) -> dict:
    """Force every live SDK client to re-establish the MCP connection for this
    server. Useful when an MCP process died / network blip — SDK's
    client.reconnect_mcp_server() restarts the transport without rebuilding
    the whole client."""
    from . import chat as _chat
    reconnected: list[str] = []
    errors: list[str] = []
    async with _chat._lock:
        live = list(_chat._clients.items())
    if not live:
        return {"ok": True, "reconnected": [], "errors": [], "note": "no live client"}
    for key, client in live:
        try:
            await client.reconnect_mcp_server(name)
            reconnected.append(f"{key[0]}@{key[1]}")
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {e}")
    return {"ok": True, "reconnected": reconnected, "errors": errors}


@router.get("/mcp/status", dependencies=[Depends(require_token)])
async def mcp_status() -> dict:
    """Aggregate MCP server status from every live SDK client (each may have
    its own connection state). Returns per-client breakdown so the UI can
    show which session's MCP is borked when reconnect is needed."""
    from . import chat as _chat
    async with _chat._lock:
        live = list(_chat._clients.items())
    out: list[dict] = []
    for key, client in live:
        sid, model = key
        try:
            status = await client.get_mcp_status()
            out.append({"session_id": sid, "model": model, "status": status})
        except Exception as e:
            out.append({"session_id": sid, "model": model,
                         "error": f"{type(e).__name__}: {e}"})
    return {"clients": out}


# ====== Skill discovery ======
#
# We don't enable/disable skills individually here — the SDK takes
# `skills="all"` or a list. We expose what's discoverable so users can browse.

SKILL_USER_DIR = Path.home() / ".claude" / "skills"
SKILL_PROJECT_DIR = Path(__file__).resolve().parent.parent / "skills"
# Plugin skills live two levels deeper under marketplaces — each marketplace
# holds N plugins, each plugin can ship its own skills/ subdir. Discovered
# at request time (no caching) so newly installed plugins surface without
# a muselab restart.
SKILL_PLUGIN_ROOT = Path.home() / ".claude" / "plugins" / "marketplaces"


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


def _list_skills_in(dir_: Path, scope: str, source: str = "") -> list[dict]:
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
                entry = {
                    "name": meta.get("name") or child.name,
                    "description": meta.get("description", ""),
                    "scope": scope,
                    "path": str(child),
                }
                if source:
                    entry["source"] = source  # e.g. plugin name for scope=plugin
                out.append(entry)
                break
    return out


def _list_plugin_skills() -> list[dict]:
    """Walk ~/.claude/plugins/marketplaces/<mp>/plugins/<plugin>/skills/ and
    collect skills as scope=plugin entries. Each entry carries `source` =
    "<marketplace>/<plugin>" so the UI can show which plugin owns it.

    Why this matters: Claude Code users who install plugins (via the
    plugin marketplace) get skills bundled with each plugin. The SDK
    surfaces those at runtime, but muselab's discovery loop only looked
    at the top-level user/project scopes — so users saw "muselab knows
    18 skills" when their actual Claude Code setup had 40+.
    """
    out: list[dict] = []
    if not SKILL_PLUGIN_ROOT.exists() or not SKILL_PLUGIN_ROOT.is_dir():
        return out
    try:
        marketplaces = sorted(p for p in SKILL_PLUGIN_ROOT.iterdir() if p.is_dir())
    except OSError:
        return out
    for mp in marketplaces:
        plugins_dir = mp / "plugins"
        if not plugins_dir.is_dir():
            continue
        try:
            plugins = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for plugin in plugins:
            skills_dir = plugin / "skills"
            if not skills_dir.is_dir():
                continue
            source = f"{mp.name}/{plugin.name}"
            out.extend(_list_skills_in(skills_dir, "plugin", source=source))
    return out


@router.get("/skills", dependencies=[Depends(require_token)])
def list_skills() -> dict:
    """List all skills discoverable from project, user, and plugin scopes.

    project = muselab's own preset skills (this repo's skills/)
    user    = ~/.claude/skills/ (shared with Claude Code CLI)
    plugin  = ~/.claude/plugins/marketplaces/*/plugins/*/skills/

    Same-named skills across scopes are returned as separate entries —
    the UI shows scope badges so users can see when a skill is shadowed
    (e.g. they have a user-scope mermaid-helper AND a project preset)."""
    skills = (_list_skills_in(SKILL_PROJECT_DIR, "project") +
              _list_skills_in(SKILL_USER_DIR, "user") +
              _list_plugin_skills())
    return {"skills": skills}


# ====== Upgrade / version check ======
# (asyncio / shutil / subprocess / sys imports hoisted to module top per E402)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UPGRADE_LOCK = asyncio.Lock()        # serialize upgrades — never run two at once
_LAST_UPGRADE: dict[str, Any] = {}     # cache last upgrade output for UI replay


def _current_versions() -> dict:
    """Currently-installed muselab + SDK + CLI versions.

    Two distinct CLIs to track:
      - bundled_cli: ships inside claude-agent-sdk's _bundled/ directory,
        always present, version follows SDK releases. This is what muselab
        actually spawns to talk to Anthropic.
      - system_cli:  optional, installed via `npm install -g
        @anthropic-ai/claude-code`. Only needed once for `claude login`
        to create ~/.claude/.credentials.json (Pro/Max OAuth). After
        login the bundled CLI can read those credentials too.

    The UI should make this distinction clear so users don't think they
    need to install the system CLI when DeepSeek / API-key paths suffice."""
    import re
    sdk_version = None
    try:
        from claude_agent_sdk import __version__ as _v
        sdk_version = _v
    except Exception:
        pass

    def _probe(bin_path: str | None) -> str | None:
        if not bin_path:
            return None
        try:
            out = subprocess.run([bin_path, "--version"],
                                  capture_output=True, text=True, timeout=3)
            line = (out.stdout.strip().splitlines() or [""])[0]
            m = re.search(r"\d+\.\d+\.\d+", line)
            return m.group(0) if m else (line or None)
        except Exception:
            return None

    bundled_path = None
    try:
        import claude_agent_sdk as _sdk
        bp = Path(_sdk.__file__).parent / "_bundled" / "claude.exe"
        if not bp.exists():
            bp = Path(_sdk.__file__).parent / "_bundled" / "claude"
        if bp.exists():
            bundled_path = str(bp)
    except Exception:
        pass

    bundled_cli = _probe(bundled_path)

    # System claude lookup — check multiple common install locations and
    # pick the one with the highest version. Reason: `npm install -g
    # @anthropic-ai/claude-code` typically writes to the user's npm prefix
    # (e.g. ~/.npm-global/bin/claude), but the user's $PATH may still
    # resolve `claude` to an older system-wide install at /usr/bin/claude.
    # If we only probed shutil.which("claude") we'd report the shadowed
    # OLD version even right after the user successfully upgraded — UI
    # would keep showing the upgrade-available badge. The fix is to also
    # peek into npm's prefix and the conventional ~/.npm-global path, then
    # report whichever binary actually has the highest semver.
    system_candidates: list[str] = []
    which_claude = shutil.which("claude")
    if which_claude:
        system_candidates.append(which_claude)
    home_npm = str(Path.home() / ".npm-global" / "bin" / "claude")
    if Path(home_npm).exists() and home_npm not in system_candidates:
        system_candidates.append(home_npm)
    if shutil.which("npm"):
        try:
            r = subprocess.run(
                ["npm", "config", "get", "prefix"],
                capture_output=True, text=True, timeout=2,
            )
            prefix = (r.stdout or "").strip()
            if prefix and prefix not in ("undefined",):
                cand = str(Path(prefix) / "bin" / "claude")
                if Path(cand).exists() and cand not in system_candidates:
                    system_candidates.append(cand)
        except Exception:
            pass

    system_cli: str | None = None
    system_cli_path: str | None = None
    for cand in system_candidates:
        v = _probe(cand)
        if v and (system_cli is None or _semver_gt(v, system_cli)):
            system_cli = v
            system_cli_path = cand

    return {
        "sdk": sdk_version,
        "bundled_cli": bundled_cli,
        "system_cli": system_cli,
        "system_cli_present": system_cli is not None,
        # New: the actual path muselab is reporting from. Helps users
        # diagnose "I just upgraded but the badge is still there" — they
        # can see whether muselab is looking at the right binary.
        "system_cli_path": system_cli_path,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        # Legacy field kept for backward-compat with any consumer expecting it.
        "cli": bundled_cli or system_cli,
        "cli_present": bool(bundled_cli or system_cli),
    }


async def _latest_versions() -> dict:
    """Query PyPI + npm for the latest released versions of SDK / CLI.
    Returns {sdk: str|None, cli: str|None, errors: [str]}."""
    import httpx
    errors: list[str] = []
    sdk_latest = None
    cli_latest = None
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get("https://pypi.org/pypi/claude-agent-sdk/json")
            if r.status_code == 200:
                sdk_latest = r.json().get("info", {}).get("version")
            else:
                errors.append(f"pypi HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"pypi: {type(e).__name__}: {e}")
        try:
            r = await client.get("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")
            if r.status_code == 200:
                cli_latest = r.json().get("version")
            else:
                errors.append(f"npm HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"npm: {type(e).__name__}: {e}")
    return {"sdk": sdk_latest, "cli": cli_latest, "errors": errors}


def _semver_gt(a: str | None, b: str | None) -> bool:
    """True if a > b (both 'X.Y.Z' style). Missing → False (don't suggest upgrade)."""
    if not a or not b:
        return False
    try:
        ta = tuple(int(p) for p in a.split(".")[:3])
        tb = tuple(int(p) for p in b.split(".")[:3])
        return ta > tb
    except (ValueError, AttributeError):
        return False


@router.get("/versions", dependencies=[Depends(require_token)])
async def get_versions() -> dict:
    """Current + latest versions for SDK and CLI; flags whether an upgrade
    is available. Used by the Settings panel's "版本与升级" section."""
    current = _current_versions()
    latest = await _latest_versions()
    return {
        "current": current,
        "latest": latest,
        "sdk_upgrade_available": _semver_gt(latest.get("sdk"), current.get("sdk")),
        # Only relevant if user has system CLI installed (for `claude login`).
        # Bundled CLI is auto-upgraded when SDK is upgraded.
        "system_cli_upgrade_available": (
            current.get("system_cli_present") and
            _semver_gt(latest.get("cli"), current.get("system_cli"))
        ),
        "expected_cli_version": _expected_cli_version(),
        "last_upgrade": _LAST_UPGRADE.copy() if _LAST_UPGRADE else None,
    }


def _expected_cli_version() -> str | None:
    """SDK bundles a string indicating the CLI version it was built against.
    If user's installed CLI is older, --session-id and other recent flags fail."""
    try:
        from claude_agent_sdk._cli_version import __cli_version__
        return __cli_version__
    except Exception:
        return None


class UpgradeReq(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["sdk", "cli"])
    """Which packages to upgrade. Default: both."""


@router.post("/upgrade", dependencies=[Depends(require_token)])
async def trigger_upgrade(req: UpgradeReq) -> dict:
    """Run the upgrade flow in-process. Returns step-by-step output for the
    UI to render. Does NOT restart muselab — the running Python process keeps
    serving the old SDK in memory until you restart it (Scheduled Task /
    systemd / launchctl). UI shows the restart command after upgrade ends."""
    if _UPGRADE_LOCK.locked():
        raise HTTPException(409, "upgrade already in progress")
    async with _UPGRADE_LOCK:
        steps: list[dict] = []
        before = _current_versions()
        steps.append({"step": "before", "versions": before})

        if "sdk" in req.targets:
            # uv lock --upgrade-package claude-agent-sdk, then uv sync
            try:
                p1 = await asyncio.create_subprocess_exec(
                    "uv", "lock", "--upgrade-package", "claude-agent-sdk",
                    cwd=str(_REPO_ROOT),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out1, _ = await p1.communicate()
                steps.append({"step": "uv lock", "rc": p1.returncode,
                              "output": (out1 or b"").decode(errors="replace")[-2000:]})
                if p1.returncode != 0:
                    raise RuntimeError("uv lock failed")

                p2 = await asyncio.create_subprocess_exec(
                    "uv", "sync", "--frozen",
                    cwd=str(_REPO_ROOT),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out2, _ = await p2.communicate()
                steps.append({"step": "uv sync", "rc": p2.returncode,
                              "output": (out2 or b"").decode(errors="replace")[-2000:]})
                if p2.returncode != 0:
                    raise RuntimeError("uv sync failed")
            except FileNotFoundError:
                steps.append({"step": "uv lock", "rc": -1, "output": "uv binary not found in PATH"})
            except Exception as e:
                steps.append({"step": "sdk upgrade aborted", "rc": -1, "output": f"{type(e).__name__}: {e}"})

        if "cli" in req.targets and shutil.which("npm"):
            try:
                p3 = await asyncio.create_subprocess_exec(
                    "npm", "install", "-g", "@anthropic-ai/claude-code@latest",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out3, _ = await p3.communicate()
                steps.append({"step": "npm install -g claude-code", "rc": p3.returncode,
                              "output": (out3 or b"").decode(errors="replace")[-2000:]})
            except Exception as e:
                steps.append({"step": "cli upgrade aborted", "rc": -1, "output": f"{type(e).__name__}: {e}"})
        elif "cli" in req.targets:
            steps.append({"step": "npm not found", "rc": -1, "output": "skipped — install Node.js + npm first"})

        # Re-read post-upgrade versions
        after = _current_versions()
        steps.append({"step": "after", "versions": after})

        # Detect changes
        sdk_changed = before.get("sdk") != after.get("sdk")
        cli_changed = before.get("cli") != after.get("cli")
        all_ok = all(s.get("rc", 0) == 0 for s in steps if "rc" in s)
        result = {
            "ok": all_ok,
            "steps": steps,
            "sdk_changed": sdk_changed,
            "cli_changed": cli_changed,
            "needs_restart": sdk_changed,   # SDK loaded in-process → restart to pick up
            "restart_hint": _restart_hint(),
        }
        _LAST_UPGRADE.clear()
        _LAST_UPGRADE.update(result)
        return result


def _restart_hint() -> str:
    """Platform-specific command to restart muselab so the new SDK is loaded."""
    import platform
    sysname = platform.system()
    if sysname == "Windows":
        return ("Stop-ScheduledTask -TaskName Muselab; "
                "Get-Process python,uv -EA SilentlyContinue | Stop-Process -Force; "
                "Start-ScheduledTask -TaskName Muselab")
    if sysname == "Darwin":
        return "launchctl kickstart -k gui/$UID/com.muselab"
    return "systemctl --user restart muselab"
