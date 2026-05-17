"""Catalog of LLM providers that expose Anthropic-compatible Messages API
endpoints. These let Claude Agent SDK call them directly without any router —
the SDK's full agent loop (Read/Edit/Bash/Glob/Grep/Task/TodoWrite/MCP/Skills/
Subagents/CLAUDE.md auto-load) works identically across all of them.

Adding a new provider:
  1. Confirm the vendor publishes an /anthropic endpoint that speaks the
     Anthropic Messages API (most major Chinese LLM vendors do as of 2026).
  2. Add an entry below: (model prefix → base_url, env key, display name,
     known model list).
  3. Set the corresponding key in .env.
  4. Restart muselab.
"""
from __future__ import annotations
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Provider:
    prefix: str          # model name prefix (e.g. "deepseek-")
    base_url: str        # Anthropic-compatible endpoint
    env_key: str         # name of env var holding the API key
    display: str         # human-readable group name
    models: tuple[tuple[str, str], ...]   # ((model_id, short_label), ...)


# Order matters: longer prefix first wins on match.
# Label = full model id (per user preference); the prefix group name in the UI
# dropdown gives context, model id removes ambiguity.
CATALOG: tuple[Provider, ...] = (
    Provider(
        prefix="deepseek-",
        base_url="https://api.deepseek.com/anthropic",
        env_key="DEEPSEEK_API_KEY",
        display="DeepSeek",
        models=(
            ("deepseek-v4-pro",    "V4 Pro"),
            ("deepseek-v4-flash",  "V4 Flash"),
            ("deepseek-chat",      "Chat"),
            ("deepseek-reasoner",  "Reasoner (R1)"),
        ),
    ),
    Provider(
        prefix="glm-",
        base_url="https://open.bigmodel.cn/api/anthropic",
        env_key="ZHIPUAI_API_KEY",
        display="智谱 GLM",
        models=(
            ("glm-5",       "GLM 5"),
            ("glm-5-air",   "GLM 5 Air"),
            ("glm-4.7",     "GLM 4.7"),
            ("glm-4-plus",  "GLM 4 Plus"),
        ),
    ),
    Provider(
        prefix="minimax-",
        # ⚠ 务必用 minimaxi.com (中国主站)；minimax.io 是海外站，
        # 用同一把 key 测试时返回 401。
        base_url="https://api.minimaxi.com/anthropic",
        env_key="MINIMAX_API_KEY",
        display="MiniMax",
        models=(
            ("minimax-m2.7",            "M2.7"),
            ("minimax-m2.7-highspeed",  "M2.7 Highspeed"),
            ("minimax-m2.5",            "M2.5"),
        ),
    ),
    # Kimi (Moonshot) removed 2026-05-17 — vendor's anthropic endpoint
    # behavior was inconsistent in muselab testing; add back when verified.
)


# Pretty labels for Claude (Pro OAuth) models — the IDs themselves are ugly
# (e.g. "claude-haiku-4-5-20251001") so we display human-friendly names.
CLAUDE_LABELS: dict[str, str] = {
    "claude-opus-4-7":              "Opus 4.7",
    "claude-sonnet-4-6":            "Sonnet 4.6",
    "claude-haiku-4-5-20251001":    "Haiku 4.5",
}


def label_for(model: str) -> str:
    """Friendly label for any model id we know about; falls back to the id."""
    if model in CLAUDE_LABELS:
        return CLAUDE_LABELS[model]
    p = lookup(model)
    if p is not None:
        for mid, lab in p.models:
            if mid == model:
                return lab
    return model


def lookup(model: str) -> Provider | None:
    """Find the provider for a given model id (by longest matching prefix)."""
    for p in sorted(CATALOG, key=lambda x: -len(x.prefix)):
        if model.startswith(p.prefix):
            return p
    return None


def is_third_party(model: str) -> bool:
    """True if this model goes through a third-party Anthropic-compat endpoint."""
    return lookup(model) is not None


def env_override(model: str) -> dict[str, str] | None:
    """Build the env dict to pass to ClaudeAgentOptions(env=...) so the SDK
    routes to the vendor's Anthropic-compatible endpoint. Returns None if no
    key is set for this provider.

    IMPORTANT auth gotcha:
      - `ANTHROPIC_API_KEY`    → sent as `x-api-key` header (standard).
      - `ANTHROPIC_AUTH_TOKEN` → sent as `Authorization: Bearer` (OAuth/enterprise).
      Third-party Anthropic-compatible vendors (DeepSeek / GLM / MiniMax)
      expect **x-api-key**. If we only set AUTH_TOKEN, vendor returns 401 and
      the CLI then silently falls back to OAuth credentials stored in ~/.claude/,
      which means the request actually hits api.anthropic.com — billing as
      Claude (often Opus). Symptom: user picked "deepseek-v4-flash" in the UI
      but sees $0.30 / msg cost. So set BOTH; the CLI ignores AUTH_TOKEN when
      API_KEY is present.

    Also: SDK passes this dict to the CLI subprocess as a full env
    REPLACEMENT (not merge). We must inherit PATH, HOME, etc. so the CLI can
    even find its config dir."""
    p = lookup(model)
    if p is None:
        return None
    key = os.environ.get(p.env_key, "")
    if not key:
        return None
    # Critical: claude CLI subprocess prefers `~/.claude/.credentials.json`
    # (Pro OAuth) over ANTHROPIC_API_KEY env. When we point it at a vendor
    # endpoint (DeepSeek/GLM/MiniMax) it would happily send the Claude OAuth
    # token to that vendor → vendor 401 "invalid api key". So for third-party
    # providers we redirect the CLI to a throwaway CLAUDE_CONFIG_DIR with no
    # credentials.json — forcing it to fall back to env-based auth.
    isolated_cfg = Path(tempfile.gettempdir()) / "muselab-vendor-cli-config"
    isolated_cfg.mkdir(exist_ok=True)
    # Make sure NO credentials file leaks in.
    cred = isolated_cfg / ".credentials.json"
    if cred.exists():
        cred.unlink()

    merged = dict(os.environ)
    merged.update({
        "ANTHROPIC_BASE_URL": p.base_url,
        "ANTHROPIC_API_KEY": key,            # primary — x-api-key header
        "ANTHROPIC_AUTH_TOKEN": key,         # belt-and-suspenders for vendors that accept Bearer
        # Defensive: kill OAuth fallback paths.
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "CLAUDE_OAUTH_TOKEN": "",
        # Point CLI at an empty config dir so it can't load saved Pro OAuth.
        "CLAUDE_CONFIG_DIR": str(isolated_cfg),
    })
    return merged


def has_anthropic_auth() -> bool:
    """True if the claude CLI has a saved Pro/Max OAuth credential. settings.py
    unsets ANTHROPIC_API_KEY at startup to force Pro-quota usage, so OAuth is
    the only way Claude works in muselab — without it the Claude group must
    not appear in the model picker."""
    return (Path.home() / ".claude" / ".credentials.json").exists()


def available_groups() -> list[dict]:
    """Catalog filtered to providers whose API key (or OAuth, for Claude) is
    configured. Each item has `label` (short pretty name) + `model` (full id
    used as dropdown value). Returns [] if nothing is configured — UI should
    treat that as 'no model, open Settings'."""
    groups: list[dict] = []
    if has_anthropic_auth():
        groups.append({
            "group": "Claude",
            "items": [
                {"label": CLAUDE_LABELS.get(m, m), "model": m} for m in (
                    "claude-sonnet-4-6",
                    "claude-haiku-4-5-20251001",
                    "claude-opus-4-7",
                )
            ],
        })
    for p in CATALOG:
        if not os.environ.get(p.env_key):
            continue
        groups.append({
            "group": p.display,
            "items": [{"label": label, "model": mid} for mid, label in p.models],
        })
    return groups
