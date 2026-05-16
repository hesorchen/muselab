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
from dataclasses import dataclass


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
        models=tuple((m, m) for m in (
            "deepseek-v4-pro", "deepseek-v4-flash",
            "deepseek-chat", "deepseek-reasoner",
        )),
    ),
    Provider(
        prefix="glm-",
        base_url="https://open.bigmodel.cn/api/anthropic",
        env_key="ZHIPUAI_API_KEY",
        display="智谱 GLM",
        models=tuple((m, m) for m in (
            "glm-5", "glm-5-air", "glm-4.7", "glm-4-plus",
        )),
    ),
    Provider(
        prefix="minimax-",
        base_url="https://api.minimax.io/anthropic",
        env_key="MINIMAX_API_KEY",
        display="MiniMax",
        models=tuple((m, m) for m in (
            "minimax-m2.7", "minimax-m2.7-highspeed", "minimax-m2.5",
        )),
    ),
    Provider(
        prefix="kimi-",
        base_url="https://api.moonshot.cn/anthropic",
        env_key="MOONSHOT_API_KEY",
        display="Kimi (Moonshot)",
        models=tuple((m, m) for m in (
            "kimi-k2.6", "kimi-k2", "kimi-latest",
        )),
    ),
)


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
    routes to the vendor's Anthropic endpoint. Returns None if no key is set.

    IMPORTANT: SDK passes this dict to the CLI subprocess as a full env
    REPLACEMENT (not merge). So we must include the inherited env that claude
    CLI needs (PATH, HOME, etc.) — otherwise the subprocess can't even find its
    own config and exits with code 1."""
    p = lookup(model)
    if p is None:
        return None
    key = os.environ.get(p.env_key, "")
    if not key:
        return None
    merged = dict(os.environ)
    merged.update({
        "ANTHROPIC_BASE_URL": p.base_url,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",   # defensive: strip inherited
    })
    return merged


def available_groups() -> list[dict]:
    """Catalog filtered to providers whose API key is configured.
    Always returns the Claude group as the first entry."""
    groups: list[dict] = [{
        "group": "Claude (Pro OAuth)",
        "items": [
            {"label": m, "model": m} for m in (
                "claude-sonnet-4-6",
                "claude-haiku-4-5-20251001",
                "claude-opus-4-7",
            )
        ],
    }]
    for p in CATALOG:
        if not os.environ.get(p.env_key):
            continue
        groups.append({
            "group": p.display,
            "items": [{"label": label, "model": mid} for mid, label in p.models],
        })
    return groups
