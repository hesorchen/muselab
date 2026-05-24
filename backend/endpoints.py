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
    # Whether this vendor's Anthropic-compat endpoint handles the standard
    # Anthropic thinking config. Set False for endpoints that reject thinking
    # or where thinking budget pushes max_tokens past the vendor's output limit
    # (e.g. Qianfan 12288 cap). Defaults to True at the call site.
    supports_thinking: bool = True
    # Vendor's hard cap on max output tokens, if it's lower than what the
    # claude CLI's default would send. None = let the CLI pick its default
    # (typically 32k+ on real Anthropic). For vendors that 400 the request
    # when max_tokens exceeds their cap (e.g. Qianfan refuses anything
    # over 12288), pin this and we'll export CLAUDE_CODE_MAX_OUTPUT_TOKENS
    # in the SDK's env dict so the CLI subprocess never sends a value over
    # the cap. Symptom this prevents:
    #     API Error: 400 — max_completion_tokens range is [1, 12288]
    max_output_tokens: int | None = None


# Default base URLs per provider — used when no env override is set. Resolved
# at request time (via `_resolve_base_url`) so a Settings UI change to
# `<PROVIDER>_BASE_URL` takes effect on the next stream() without a restart.
_DEFAULT_BASE_URLS: dict[str, str] = {
    "DEEPSEEK_API_KEY":      "https://api.deepseek.com/anthropic",
    "ZHIPUAI_API_KEY":       "https://open.bigmodel.cn/api/anthropic",
    # ⚠ 务必用 minimaxi.com (中国主站)；minimax.io 是海外站，
    # 用同一把 key 测试时返回 401。
    "MINIMAX_API_KEY":        "https://api.minimaxi.com/anthropic",
    # Moonshot Kimi — re-added 2026-05-22 after the 2026-05-17 removal
    # ("inconsistent endpoint behaviour"). K2.5 / K2.6 (Jan-Apr 2026) are
    # new GA releases on a different code path than the version that was
    # flaky; community reports (liteLLM, kimrel, OpenClaw docs) confirm
    # the /anthropic endpoint stabilised. ⚠ Anthropic-compat layer maps
    # request_temperature * 0.6 to real_temperature — irrelevant for SDK
    # defaults but worth knowing if a downstream user tunes temperature.
    "MOONSHOT_API_KEY":       "https://api.moonshot.cn/anthropic",
    # Alibaba DashScope Qwen — official "Migrate Anthropic Workloads to
    # Qwen" doc names this path. International (Singapore) endpoint by
    # default because it's reachable globally (incl. mainland); domestic
    # users can override to dashscope.aliyuncs.com if latency matters.
    "DASHSCOPE_API_KEY":      "https://dashscope-intl.aliyuncs.com/apps/anthropic",
    # Xiaomi MiMo — V2.5-Pro public beta 2026-04-22; platform.xiaomimimo
    # explicitly documents the /anthropic endpoint.
    "XIAOMI_MIMO_API_KEY":    "https://api.xiaomimimo.com/anthropic",
    "QIANFAN_API_KEY":        "https://qianfan.baidubce.com/anthropic",
}
# Map api-key env name → base-url override env name. Self-hosters can point
# any provider at a proxy / regional mirror via these.
_BASE_URL_ENV_BY_KEY: dict[str, str] = {
    "DEEPSEEK_API_KEY":     "DEEPSEEK_BASE_URL",
    "ZHIPUAI_API_KEY":      "ZHIPUAI_BASE_URL",
    "MINIMAX_API_KEY":      "MINIMAX_BASE_URL",
    "MOONSHOT_API_KEY":     "MOONSHOT_BASE_URL",
    "DASHSCOPE_API_KEY":    "DASHSCOPE_BASE_URL",
    "XIAOMI_MIMO_API_KEY":  "XIAOMI_MIMO_BASE_URL",
    "QIANFAN_API_KEY":      "QIANFAN_BASE_URL",
}


_VENDOR_CONFIG_DIR = Path(tempfile.gettempdir()) / "muselab-vendor-cli-config"


def _vendor_config_dir() -> Path:
    """Returns the isolated config dir used by third-party providers. Shared
    across all three-party sessions — it has no .credentials.json, so the CLI
    subprocess cannot fall back to Pro OAuth and send the wrong token to vendor.

    chat.py also reads from here when loading session messages for vendor
    sessions."""
    _VENDOR_CONFIG_DIR.mkdir(exist_ok=True)
    return _VENDOR_CONFIG_DIR


def _resolve_base_url(env_key: str, provider: Provider | None = None) -> str:
    """Look up the current base URL for a provider: env override > provider's
    own base_url (from CATALOG) > per-key default. The provider fallback is
    critical for families that share one API key but need different endpoints
    (e.g. Qwen domestic vs international — both use DASHSCOPE_API_KEY but
    route to different hosts)."""
    override_env = _BASE_URL_ENV_BY_KEY.get(env_key, "")
    if override_env:
        v = os.environ.get(override_env, "").strip()
        if v:
            return v.rstrip("/")
    if provider is not None:
        return provider.base_url.rstrip("/")
    return _DEFAULT_BASE_URLS.get(env_key, "").rstrip("/")


# Order matters: longer prefix first wins on match.
# `base_url` here is the default at module-load time; runtime calls resolve
# via `_resolve_base_url(env_key)` so users can swap endpoints without
# restart (handy for proxied / on-prem deployments).
# Label = full model id (per user preference); the prefix group name in the UI
# dropdown gives context, model id removes ambiguity.
CATALOG: tuple[Provider, ...] = (
    Provider(
        prefix="deepseek-",
        base_url=_DEFAULT_BASE_URLS["DEEPSEEK_API_KEY"],
        env_key="DEEPSEEK_API_KEY",
        display="DeepSeek",
        models=(
            ("deepseek-v4-pro",    "V4 Pro"),
            ("deepseek-v4-flash",  "V4 Flash"),
        ),
    ),
    Provider(
        prefix="glm-",
        base_url=_DEFAULT_BASE_URLS["ZHIPUAI_API_KEY"],
        env_key="ZHIPUAI_API_KEY",
        display="智谱 GLM",
        models=(
            ("glm-5.1",      "GLM 5.1"),
            ("glm-5",        "GLM 5"),
            ("glm-5-air",    "GLM 5 Air"),
            ("glm-4.7",      "GLM 4.7"),
            ("glm-4-plus",   "GLM 4 Plus"),
        ),
    ),
    Provider(
        prefix="minimax-",
        base_url=_DEFAULT_BASE_URLS["MINIMAX_API_KEY"],
        env_key="MINIMAX_API_KEY",
        display="MiniMax",
        models=(
            ("minimax-m2.7",            "M2.7"),
            ("minimax-m2.7-highspeed",  "M2.7 Highspeed"),
            ("minimax-m2.5",            "M2.5"),
            ("minimax-m2.5-highspeed",  "M2.5 Highspeed"),
            ("minimax-m2.1",            "M2.1"),
            ("minimax-m2.1-highspeed",  "M2.1 Highspeed"),
        ),
    ),
    # MiniMax 国际站 — 海外用户延迟更低。⚠ 注意：国际站需要单独的 API key，
    # 中国站的 key 在国际站会返回 401。
    Provider(
        prefix="minimax-intl:",
        base_url="https://api.minimax.io/anthropic",
        env_key="MINIMAX_INTL_API_KEY",
        display="MiniMax (国际)",
        models=(
            ("minimax-intl:minimax-m2.7",            "M2.7"),
            ("minimax-intl:minimax-m2.7-highspeed",  "M2.7 Highspeed"),
            ("minimax-intl:minimax-m2.5",            "M2.5"),
            ("minimax-intl:minimax-m2.5-highspeed",  "M2.5 Highspeed"),
            ("minimax-intl:minimax-m2.1",            "M2.1"),
            ("minimax-intl:minimax-m2.1-highspeed",  "M2.1 Highspeed"),
        ),
    ),
    # Moonshot Kimi — re-added 2026-05-22. Removed once on 2026-05-17 for
    # "inconsistent endpoint behaviour"; the K2.5 / K2.6 releases land on
    # an updated stack with stable Anthropic-compat per vendor docs +
    # third-party adapters (liteLLM, kimrel, OpenClaw). Verify tool-use
    # works for your account before relying on production usage.
    Provider(
        prefix="kimi-",
        base_url="https://api.moonshot.cn/anthropic",
        env_key="MOONSHOT_API_KEY",
        display="Kimi",
        models=(
            ("kimi-k2.6",          "K2.6"),          # 2026-04 GA
            ("kimi-k2.5",          "K2.5"),          # 2026-01
            ("kimi-k2-thinking",   "K2 Thinking"),
            ("kimi-k2",            "K2"),
        ),
    ),
    # Alibaba DashScope Qwen — 国内站（默认）。Anthropic-compat path is
    # /apps/anthropic (not /anthropic). Prefix is the bare string "qwen"
    # (no dash) because model ids alternate "qwen-plus" and "qwen3-max".
    # 同一把 API key 可用于国内站和国际站，国际用户可选国际站降低延迟。
    Provider(
        prefix="qwen",
        base_url="https://dashscope.aliyuncs.com/apps/anthropic",
        env_key="DASHSCOPE_API_KEY",
        display="Qwen",
        models=(
            ("qwen3.6-plus",          "Qwen3.6 Plus"),
            ("qwen3-max",             "Qwen3 Max"),
            ("qwen3.5-plus",          "Qwen3.5 Plus"),
            ("qwen3.5-flash",         "Qwen3.5 Flash"),
            ("qwen3.5-coder-plus",    "Qwen3.5 Coder Plus"),
            ("qwen-plus",             "Qwen Plus"),
        ),
    ),
    # Qwen 国际站 — 新加坡节点，国际用户延迟更低。与国内站共用同一把 API key。
    Provider(
        prefix="qwen-intl:",
        base_url="https://dashscope-intl.aliyuncs.com/apps/anthropic",
        env_key="DASHSCOPE_API_KEY",
        display="Qwen (国际)",
        models=(
            ("qwen-intl:qwen3.6-plus",          "Qwen3.6 Plus"),
            ("qwen-intl:qwen3-max",             "Qwen3 Max"),
            ("qwen-intl:qwen3.5-plus",          "Qwen3.5 Plus"),
            ("qwen-intl:qwen3.5-flash",         "Qwen3.5 Flash"),
            ("qwen-intl:qwen3.5-coder-plus",    "Qwen3.5 Coder Plus"),
            ("qwen-intl:qwen-plus",             "Qwen Plus"),
        ),
    ),
    # Xiaomi MiMo — added 2026-05-22. V2.5-Pro public beta 2026-04-22.
    # MIT-licensed weights + Anthropic-compatible API; endpoint format
    # follows the DeepSeek convention exactly.
    Provider(
        prefix="mimo-",
        base_url=_DEFAULT_BASE_URLS["XIAOMI_MIMO_API_KEY"],
        env_key="XIAOMI_MIMO_API_KEY",
        display="Xiaomi MiMo",
        models=(
            ("mimo-v2.5-pro",   "V2.5 Pro"),
            ("mimo-v2.5",       "V2.5"),
            ("mimo-v2-flash",   "V2 Flash"),
        ),
    ),
    # Baidu Qianfan — Anthropic-compat endpoint confirmed 2026-05-23.
    # ⚠ Auth uses IAM access token (bce-v3/ALTAK-xxx/xxx), not a plain
    # sk-xxx key. Qianfan is a model aggregator: in addition to ERNIE
    # models, it also hosts third-party models (DeepSeek / Kimi / GLM /
    # MiniMax / Qwen) behind the same endpoint. Model availability may
    # vary by account — check console.bce.baidu.com/qianfan for your
    # region's current model list.
    Provider(
        prefix="ernie-",
        base_url=_DEFAULT_BASE_URLS["QIANFAN_API_KEY"],
        env_key="QIANFAN_API_KEY",
        display="百度千帆",
        supports_thinking=False,
        # Qianfan rejects max_completion_tokens > 12288 with HTTP 400.
        # The CLI's default sits around 32-64k, so we have to pin this.
        max_output_tokens=12288,
        # Model list audited 2026-05-24 by direct probe against
        # qianfan.baidubce.com/anthropic. ernie-5.0 added (flagship,
        # ships with thinking output). ernie-x1.1-preview added (new
        # reasoning preview). deepseek-v3.1 / deepseek-r1 removed —
        # Qianfan no longer serves them on the Anthropic-compat path
        # (returns invalid_model).
        models=(
            ("ernie-5.0",                 "ERNIE 5.0"),
            ("ernie-4.5-turbo-20260402",  "ERNIE 4.5 Turbo"),
            ("ernie-4.5-turbo-128k",      "ERNIE 4.5 Turbo 128K"),
            ("ernie-4.0-turbo-128k",      "ERNIE 4.0 Turbo"),
            ("ernie-4.0-8k",              "ERNIE 4.0"),
            ("ernie-x1.1-preview",        "ERNIE X1.1 推理 (preview)"),
            ("ernie-x1-turbo-32k",        "ERNIE X1 推理"),
            ("deepseek-v3.2",             "DeepSeek V3.2 (千帆)"),
        ),
    ),
    # Doubao (字节 Volcengine) deliberately NOT added — only
    # `Doubao-Seed-Code` is documented as Claude-Code-native
    # Anthropic-compat; the general Doubao endpoint
    # (ark.cn-beijing.volces.com/api/v3) doesn't expose the standard
    # /anthropic path. Revisit once Volcengine publishes a stable
    # /anthropic gateway across model families.
)


# Pretty labels for Claude (Pro OAuth) models — the IDs themselves are ugly
# (e.g. "claude-haiku-4-5-20251001") so we display human-friendly names.
CLAUDE_LABELS: dict[str, str] = {
    "claude-opus-4-7":              "Opus 4.7",
    "claude-sonnet-4-6":            "Sonnet 4.6",
    "claude-haiku-4-5-20251001":    "Haiku 4.5",
}


_CLAUDE_LABEL_RE = __import__("re").compile(
    r"^claude-(opus|sonnet|haiku)-(\d+)[-.](\d+)", __import__("re").IGNORECASE)


def label_for(model: str) -> str:
    """Friendly label for any model id we know about; falls back to a
    derived label for unknown Claude variants, then to the raw id.

    Why the derive step: cost-dashboard rows come from the JSONL transcript,
    which may contain older / preview / region-specific Claude ids that
    aren't in `CLAUDE_LABELS` (e.g. `claude-opus-4-6`, `claude-sonnet-3-7`).
    Showing raw ids made the by_model table look broken next to nicely-
    named neighbors. The regex extracts "Opus 4.6" / "Sonnet 3.7" /
    "Haiku 4.5" from any `claude-{tier}-{X}-{Y}...` shape.
    """
    if not model:
        return model
    if model in CLAUDE_LABELS:
        return CLAUDE_LABELS[model]
    # Derive friendly Claude label from id pattern when not in the explicit
    # map. Catches historical / future-proof Anthropic ids without code
    # changes per release.
    if model.lower().startswith("claude-"):
        m = _CLAUDE_LABEL_RE.match(model)
        if m:
            kind = m.group(1).capitalize()
            return f"{kind} {m.group(2)}.{m.group(3)}"
    p = lookup(model)
    if p is not None:
        low = model.lower()
        for mid, lab in p.models:
            if mid.lower() == low:
                return lab
    return model


def lookup(model: str) -> Provider | None:
    """Find the provider for a given model id (by longest matching prefix).
    Case-insensitive: third-party vendors sometimes return mixed-case in
    `usage.model` (e.g. MiniMax returns `MiniMax-M2.7`), and we want those
    to route to the same provider as the lowercase catalog entry."""
    low = (model or "").lower()
    for p in sorted(CATALOG, key=lambda x: -len(x.prefix)):
        if low.startswith(p.prefix):
            return p
    return None


def is_third_party(model: str) -> bool:
    """True if this model goes through a third-party Anthropic-compat endpoint."""
    return lookup(model) is not None


def normalize_model_id(model: str) -> str:
    """Strip internal prefixes from model id before sending to the API.
    For example, 'qwen-intl:qwen3-max' becomes 'qwen3-max'."""
    if model.startswith("qwen-intl:"):
        return model[len("qwen-intl:"):]
    if model.startswith("minimax-intl:"):
        return model[len("minimax-intl:"):]
    return model


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
    isolated_cfg = _vendor_config_dir()
    # Make sure NO credentials file leaks in.
    cred = isolated_cfg / ".credentials.json"
    if cred.exists():
        cred.unlink()

    # Resolve base URL at call time so a Settings-UI override (or .env tweak
    # via `<VENDOR>_BASE_URL`) takes effect on the very next stream() — no
    # process restart needed. Falls back to the catalog default.
    base_url = _resolve_base_url(p.env_key, p)
    merged = dict(os.environ)
    merged.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": key,            # primary — x-api-key header
        "ANTHROPIC_AUTH_TOKEN": key,         # belt-and-suspenders for vendors that accept Bearer
        # Defensive: kill OAuth fallback paths.
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "CLAUDE_OAUTH_TOKEN": "",
        # Point CLI at an empty config dir so it can't load saved Pro OAuth.
        "CLAUDE_CONFIG_DIR": str(isolated_cfg),
    })
    # Cap output tokens for vendors whose ceiling is below the CLI's
    # default. Without this, Qianfan returns 400 "max_completion_tokens
    # range is [1, 12288]" on every call. CLAUDE_CODE_MAX_OUTPUT_TOKENS
    # is the documented env knob the CLI honours for its outgoing
    # max_tokens parameter.
    if p.max_output_tokens is not None:
        merged["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(p.max_output_tokens)
    return merged


def has_anthropic_auth() -> bool:
    """True if Claude is reachable, via either:
      - ~/.claude/.credentials.json  (claude CLI Pro/Max OAuth, free quota), OR
      - ANTHROPIC_API_KEY env var    (pay-per-use console.anthropic.com).
    If neither, the Claude group hides from the model picker so the UI doesn't
    offer a model that's guaranteed to 401 on first send."""
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    return False


def available_groups() -> list[dict]:
    """Catalog filtered to providers whose API key (or OAuth, for Claude) is
    configured AND not disabled in Settings. Each item has `label` (short
    pretty name) + `model` (full id used as dropdown value). Returns [] if
    nothing is configured — UI should treat that as 'no model, open Settings'."""
    raw_disabled = os.environ.get("MUSELAB_DISABLED_PROVIDERS", "").strip()
    disabled_models = set(raw_disabled.split(",")) if raw_disabled else set()
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
        # Skip provider if its first model is in the disabled list. We use the
        # first model ID as the provider's stable identifier — it uniquely
        # identifies each provider row (including Qwen domestic vs international
        # which share DASHSCOPE_API_KEY but have different first models).
        if p.models[0][0] in disabled_models:
            continue
        groups.append({
            "group": p.display,
            "items": [{"label": label, "model": mid} for mid, label in p.models],
        })
    return groups
