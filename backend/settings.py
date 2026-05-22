import os
import warnings
from pathlib import Path
from dotenv import load_dotenv


def atomic_write_text(path: Path, data: str, encoding: str = "utf-8") -> None:
    """Write text atomically: tmpfile in same dir + os.replace().

    Survives crash / OOM-kill mid-write — the destination either holds the
    old content or the new content, never a truncated half-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(data, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 不再主动 pop ANTHROPIC_API_KEY —— claude CLI 的优先级已经正确：
# 若 ~/.claude/.credentials.json 存在则用 OAuth（Pro 配额，免费），
# 否则 fallback 到 ANTHROPIC_API_KEY（按量计费）。
# 之前 pop 是过度防御，会把"只有 API key 没 Pro"的用户彻底堵死。
# AUTH_TOKEN 仍清理，避免某些场景下被误当成 OAuth bearer 发出去。
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


def _env(new_name: str, old_name: str = "", default: str = "") -> str:
    """Read MUSELAB_X with fallback to legacy PORTAL_X (deprecation warning).
    Pass old_name="" to skip the fallback."""
    v = os.environ.get(new_name)
    if v:
        return v
    if old_name:
        v = os.environ.get(old_name)
        if v:
            warnings.warn(
                f"{old_name} is deprecated; rename to {new_name} in your .env",
                DeprecationWarning, stacklevel=2,
            )
            return v
    return default


_root_str = _env("MUSELAB_ROOT", "PORTAL_ROOT")
ROOT = Path(_root_str).resolve() if _root_str else None
TOKEN = _env("MUSELAB_TOKEN", "PORTAL_TOKEN")
PORT = int(_env("MUSELAB_PORT", "PORTAL_PORT", "8765"))
# Default to localhost-only. The one-shot installer scripts target single-user
# desktops, so binding to LAN by default would be a footgun. Override to "0.0.0.0"
# in .env for LAN/VPS/Docker scenarios.
HOST = _env("MUSELAB_HOST", "PORTAL_HOST", "127.0.0.1")
MODEL = _env("MUSELAB_MODEL", "PORTAL_MODEL", "claude-sonnet-4-6")

# MCP server config. Editable via the Settings UI (api_settings.py).
# Stored as {"mcpServers": {name: {command, args, env, disabled}}}.
# Always set the path so the UI can create it on first write; chat.py guards
# the read with a try/except, so it's safe if the file doesn't exist yet.
MCP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp.json"

# Optional non-Claude providers. Base URLs default to each vendor's
# Anthropic-compatible endpoint (NOT the OpenAI-compatible one — Claude
# Agent SDK speaks Anthropic Messages protocol). Self-hosters can override
# any of these via env (proxy, regional mirror, on-prem deployment).
# Read at startup; `endpoints.py` re-reads at request time so a Settings
# UI tweak takes effect on the next stream without a process restart.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
ZHIPUAI_BASE_URL = os.environ.get("ZHIPUAI_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")

if not TOKEN:
    raise RuntimeError("MUSELAB_TOKEN must be set in .env")
if len(TOKEN) < 16:
    raise RuntimeError(
        "MUSELAB_TOKEN too short (need >=16 chars). Generate with: "
        "python -c 'import secrets;print(secrets.token_hex(24))'"
    )
if ROOT is None:
    raise RuntimeError("MUSELAB_ROOT must be set in .env (do NOT default to $HOME)")
if not ROOT.exists():
    raise RuntimeError(f"MUSELAB_ROOT does not exist: {ROOT}")

# Reject roots that point at system / cross-user paths — those are almost
# always misconfiguration (single-user muselab has no business browsing /etc
# or another user's $HOME). $HOME is allowed: the agent runs with
# bypassPermissions and already has full FS write access regardless of ROOT,
# so restricting ROOT to a subdir was security theatre — it only crippled
# the UI without changing the actual blast radius.
_FORBIDDEN_ROOTS = {Path("/"), Path("/etc"), Path("/root"), Path("/home"),
                    Path("/var"), Path("/usr"), Path("/boot")}
if ROOT in _FORBIDDEN_ROOTS:
    raise RuntimeError(
        f"MUSELAB_ROOT={ROOT} is a system / cross-user path. Point it at "
        f"your $HOME or a sub-directory you own."
    )


def is_chinese_locale() -> bool:
    """Best-effort host-locale check — True when LANG / LC_ALL / LC_MESSAGES
    indicates a Chinese system.

    Used to choose between bilingual template assets at runtime
    (`default-CLAUDE.md` vs `default-CLAUDE.en.md`, archive-skeleton READMEs,
    session labels). Mirrors the env-var probe in
    `scripts/install-*.{sh,ps1}` and `scripts/intake.{sh,ps1}` so install-time
    + runtime decisions agree.

    Conservative: only returns True when one of the locale env vars contains
    "zh" (zh / zh_CN / zh_TW / zh_HK). Everything else falls through to
    English."""
    blob = (
        os.environ.get("LANG", "")
        + os.environ.get("LC_ALL", "")
        + os.environ.get("LC_MESSAGES", "")
    )
    return "zh" in blob.lower()
