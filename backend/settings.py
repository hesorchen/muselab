import os
import shutil
import sys
import warnings
from pathlib import Path
from dotenv import load_dotenv


def env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    """Read ``name`` from env as an int, falling back to ``default`` on
    missing / empty / non-numeric input. Optional ``min_value`` clamps
    the result (e.g. negatives → 0 for "days to keep" semantics).

    Naive ``int(os.environ.get(...))`` patterns crash the whole backend
    when the env var holds a typo (``MAX_TURNS=80 turns``,
    ``CLIENT_POOL_CAP=3.5``). Three of the four module-level uses of
    this pattern (MAX_UPLOAD_BYTES, CLIENT_POOL_CAP, TRASH_TTL_DAYS)
    would refuse to even import the backend on a bad value — turning a
    config typo into "the server won't start, and the log just says
    ``ValueError: invalid literal for int()``" with no hint at the
    culprit env var. This helper makes the fallback explicit + logs
    the offending value to stderr so the operator can fix it.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        v = default
    else:
        try:
            v = int(raw)
        except ValueError:
            print(f"[muselab] {name}={raw!r} is not an integer; "
                  f"falling back to {default}", file=sys.stderr, flush=True)
            v = default
    if min_value is not None and v < min_value:
        v = min_value
    return v


def env_float(name: str, default: float) -> float:
    """Sibling to env_int for float-valued knobs (e.g. MUSELAB_BUDGET_USD).
    Same fallback behaviour."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[muselab] {name}={raw!r} is not a number; "
              f"falling back to {default}", file=sys.stderr, flush=True)
        return default


def locate_executable(name: str) -> str | None:
    """Find a CLI binary that may live outside the running process's PATH.

    Why: when muselab runs as a systemd user service, the inherited PATH is
    minimal (usually /usr/local/bin:/usr/bin) and excludes ~/.local/bin
    (where `uv` installs by default), ~/.npm-global/bin (where the Claude
    CLI lands after `npm install -g @anthropic-ai/claude-code`), and per-user node
    version-manager directories. shutil.which() only consults the current
    PATH so calls like `shutil.which("claude")` return None and the
    upgrade endpoint reports "CLI not installed" even when it is.

    Returns absolute path if found, else None.
    """
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    extra_dirs: list[Path] = [
        home / ".local" / "bin",          # uv default install, pipx
        home / ".cargo" / "bin",          # rustup-installed uv
        home / ".npm-global" / "bin",     # npm-global prefix (claude code)
        home / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),        # Apple Silicon Homebrew
        Path("/usr/bin"),
    ]
    # Node version managers shadow the system npm/node; check the active
    # version they expose.
    if name in {"npm", "node", "claude"}:
        nvm_node = home / ".nvm" / "versions" / "node"
        if nvm_node.exists():
            for v in sorted([p for p in nvm_node.iterdir() if p.is_dir()],
                             reverse=True):
                extra_dirs.insert(0, v / "bin")
        extra_dirs.insert(0, home / ".volta" / "bin")
    for d in extra_dirs:
        cand = d / name
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def atomic_write_text(path: Path, data: str, encoding: str = "utf-8") -> None:
    """Write text atomically: tmpfile in same dir + os.replace().

    Survives crash / OOM-kill mid-write — the destination either holds the
    old content or the new content, never a truncated half-write.
    """
    import secrets
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tmp name must be unique per concurrent caller. PID alone collides
    # when two asyncio.create_task'd writers (e.g. chat stream's sidecar
    # writer + sessions.bump_session firing at the same moment) target
    # the same path inside one process — the second write would overwrite
    # the first's half-written tmp, then both os.replace race. Add a
    # random suffix so each call's tmp is distinct.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
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

def configure_prompt_cache(env: dict | None = None) -> str:
    """Set the claude CLI's prompt-cache-TTL env flags based on
    `MUSELAB_PROMPT_CACHE_TTL`. Returns the resolved TTL string for logging.

    Default 1h, because Anthropic silently dropped the global default from
    1h → 5min on 2026-03-06 (claude-code issue #46829), making "first turn
    after any >5min idle" re-create the entire context cache at 1.25× input
    price. For long muselab sessions (100K-500K tokens) that's tens of
    dollars per day of casual use. `ENABLE_PROMPT_CACHING_1H=1` opts the
    spawned claude CLI back into the 1-hour TTL.

    Trade-off: cache_creation tokens cost 2× base price under 1h (vs 1.25×
    for 5min default), so a session touched only once per day is slightly
    more expensive to seed. Any session touched ≥2 times in an hour comes
    out ahead.

    Values:
      "1h" (default, recommended) — set ENABLE_PROMPT_CACHING_1H=1
      "5m"                        — set FORCE_PROMPT_CACHING_5M=1
      ""                          — leave CLI defaults alone
      anything else                — same as ""

    Exposed as a function so tests can exercise it without relying on
    module reload semantics (which interact unpredictably with pytest's
    monkeypatch and sys.modules caching).
    """
    env = env if env is not None else os.environ
    ttl = (env.get("MUSELAB_PROMPT_CACHE_TTL", "1h") or "").strip().lower()
    if ttl == "1h":
        env["ENABLE_PROMPT_CACHING_1H"] = "1"
        env.pop("FORCE_PROMPT_CACHING_5M", None)
    elif ttl == "5m":
        env["FORCE_PROMPT_CACHING_5M"] = "1"
        env.pop("ENABLE_PROMPT_CACHING_1H", None)
    # Anything else (empty / unrecognised) → leave CLI defaults alone.
    return ttl


configure_prompt_cache()


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
_port_raw = _env("MUSELAB_PORT", "PORTAL_PORT", "8765")
try:
    PORT = int(_port_raw)
except ValueError:
    # Non-numeric MUSELAB_PORT would crash backend import. Better to
    # fall back to the standard 8765 with a clear stderr warning than
    # to refuse startup with a cryptic stack trace.
    print(f"[muselab] MUSELAB_PORT={_port_raw!r} is not an integer; "
          f"falling back to 8765", file=sys.stderr, flush=True)
    PORT = 8765
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
