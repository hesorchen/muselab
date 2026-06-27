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
import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, replace
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
    # Stable internal identity, assigned by the catalog layer (NOT user-
    # facing). Built-ins get "b:<prefix>"; user-created providers get
    # "c:<slug>". Persisted overrides / deletions / "restore default" all key
    # off this, so the endpoint URL stays freely editable without losing the
    # entry. Empty on the raw CATALOG literal; populated by catalog().
    id: str = ""


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
    # Qwen" doc names this path. Domestic (dashscope.aliyuncs.com) is the
    # default to match the CATALOG "Qwen" group; international users pick
    # the separate "Qwen (国际)" group (qwen-intl: prefix →
    # dashscope-intl.aliyuncs.com), or override DASHSCOPE_BASE_URL. This
    # value is only the last-resort fallback when no CATALOG provider
    # matches — for normal use the provider's own base_url wins.
    "DASHSCOPE_API_KEY":      "https://dashscope.aliyuncs.com/apps/anthropic",
    # Xiaomi MiMo — V2.5-Pro public beta 2026-04-22; platform.xiaomimimo
    # explicitly documents the /anthropic endpoint.
    "XIAOMI_MIMO_API_KEY":    "https://api.xiaomimimo.com/anthropic",
    "QIANFAN_API_KEY":        "https://qianfan.baidubce.com/anthropic",
    # Local sidecar gateway that translates Anthropic Messages requests to a
    # user-authenticated Codex/OpenAI backend. Loopback-only by default: muselab
    # never reads Codex OAuth files and never calls OpenAI-native APIs directly.
    "CODEX_GATEWAY_API_KEY":   "http://127.0.0.1:8317",
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
    "CODEX_GATEWAY_API_KEY": "CODEX_GATEWAY_BASE_URL",
}


# Per-OS-user path so multiple muselab installs on the same host (e.g. a
# real user + a `muselab` test user) don't collide. Earlier this was a
# single shared `/tmp/muselab-vendor-cli-config`, so whichever user
# created it first locked the others out with PermissionError on Read,
# which then surfaced as a 504 on the chat SSE stream.
_VENDOR_CONFIG_DIR = (
    Path(tempfile.gettempdir())
    / f"muselab-vendor-cli-config-{os.getuid()}"
)


def _vendor_config_dir() -> Path:
    """Returns the isolated config dir used by third-party providers. Shared
    across all three-party sessions FOR THE CURRENT OS USER — it has no
    .credentials.json, so the CLI subprocess cannot fall back to Pro OAuth
    and send the wrong token to vendor.

    chat.py also reads from here when loading session messages for vendor
    sessions."""
    _VENDOR_CONFIG_DIR.mkdir(exist_ok=True, mode=0o700)
    return _VENDOR_CONFIG_DIR


def _resolve_base_url(env_key: str, provider: Provider | None = None) -> str:
    """Look up the current base URL for a provider: env override > provider's
    own base_url (from CATALOG) > per-key default. The provider fallback is
    critical for families that share one API key but need different endpoints
    (e.g. Qwen domestic vs international — both use DASHSCOPE_API_KEY but
    route to different hosts).

    OVERRIDE PRECEDENCE — two providers that share one API key must NOT
    collapse to the same endpoint when the user sets `<KEY>_BASE_URL`.
    Qwen domestic (prefix "qwen") and intl (prefix "qwen-intl:") both use
    DASHSCOPE_API_KEY, so a bare `DASHSCOPE_BASE_URL` override used to apply
    to BOTH — silently routing intl traffic to the domestic host (or vice
    versa). To preserve the distinction, a provider whose prefix is a
    colon-tagged mirror (e.g. "qwen-intl:") gets its OWN, more specific
    override env derived from the tag: `<KEY base>_<TAG>_BASE_URL`
    (DASHSCOPE_API_KEY + tag "intl" → DASHSCOPE_INTL_BASE_URL). That tagged
    var wins for the mirror group; the generic `DASHSCOPE_BASE_URL` only
    affects the untagged (domestic) group. If the tagged var is unset we
    fall through to the provider's catalog base_url, so the domestic/intl
    host split is kept by default."""
    # Colon-tagged mirror groups (prefix like "qwen-intl:") get a dedicated
    # override env so they don't share the generic <KEY>_BASE_URL with the
    # primary group that holds the same api key. Only apply this split when the
    # key is actually shared by multiple built-ins; single-provider colon tags
    # (e.g. a local Codex gateway) should honor the generic override normally.
    if provider is not None and provider.prefix.endswith(":"):
        generic_env = _BASE_URL_ENV_BY_KEY.get(env_key, "")
        shared_key = sum(1 for p in CATALOG if p.env_key == env_key) > 1
        if shared_key and generic_env.endswith("_BASE_URL"):
            tag = provider.prefix.rstrip(":").rsplit("-", 1)[-1].upper()
            tagged_env = generic_env[:-len("_BASE_URL")] + f"_{tag}_BASE_URL"
            v = os.environ.get(tagged_env, "").strip()
            if v:
                return v.rstrip("/")
            # No tagged override → keep the catalog host (don't fall through
            # to the generic override, which belongs to the primary group).
            return provider.base_url.rstrip("/")
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
    # Codex Gateway — local sidecar that speaks Anthropic Messages on one side
    # and uses the user's own authenticated Codex/OpenAI backend on the other.
    # This is NOT native OpenAI protocol support inside muselab: the gateway is
    # responsible for translation, auth, and model availability. Loopback HTTP is
    # intentional here; remote gateways should be put behind HTTPS + a strong key.
    Provider(
        prefix="codex:",
        base_url=_DEFAULT_BASE_URLS["CODEX_GATEWAY_API_KEY"],
        env_key="CODEX_GATEWAY_API_KEY",
        display="Codex Gateway",
        supports_thinking=False,
        models=(
            ("codex:gpt-5.5",               "GPT-5.5"),
            ("codex:gpt-5.4",               "GPT-5.4"),
            ("codex:gpt-5.4-mini",          "GPT-5.4 Mini"),
            ("codex:gpt-5.3-codex-spark",   "GPT-5.3 Codex Spark"),
        ),
    ),
    # Doubao (字节 Volcengine) deliberately NOT added — only
    # `Doubao-Seed-Code` is documented as Claude-Code-native
    # Anthropic-compat; the general Doubao endpoint
    # (ark.cn-beijing.volces.com/api/v3) doesn't expose the standard
    # /anthropic path. Revisit once Volcengine publishes a stable
    # /anthropic gateway across model families.
)


# ===========================================================================
# Effective catalog = built-in defaults (CATALOG above) + user overrides.
#
# Users can edit any built-in provider's endpoint / api-key env / prefix /
# model list, create brand-new providers, and "restore default" per provider.
# Overrides persist in `provider_overrides.json` (next to mcp.json). Built-in
# definitions in CATALOG are the immutable factory defaults; we never mutate
# them, we layer overrides on top at read time so a future muselab release can
# ship new built-in defaults without clobbering user edits.
#
# Identity: each provider has a STABLE internal id (not the endpoint URL,
# which is user-editable). Built-ins → "b:<prefix>"; user-created → "c:<slug>".
# Overrides / deletions / restore all key off this id.
# ===========================================================================
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "provider_overrides.json"

# Fields a stored override / custom provider may carry. supports_thinking and
# max_output_tokens are intentionally NOT user-editable in the UI (vendor
# quirks that break the request if wrong); built-ins keep their baked values,
# user-created providers take the safe defaults below.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-]{0,99}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-]{0,39}$")

# Custom-provider env keys must live in this reserved namespace. Without the
# allowlist, the user-supplied env_key flows straight into .env writes
# (api_settings._write_env), so a typo'd or malicious value could overwrite
# MUSELAB_TOKEN, PATH, or another provider's credential.
_CUSTOM_ENV_KEY_RE = re.compile(r"^MUSELAB_PROVIDER_[A-Z0-9_]{1,64}_API_KEY$")


def _allowed_env_keys() -> set[str]:
    """env keys that are legitimate targets for a provider's api-key write:
    every built-in catalog key plus anything in the custom namespace."""
    return {p.env_key for p in CATALOG}


def validate_env_key(env_key: str) -> None:
    """Raise ValueError unless env_key is a built-in provider key or matches
    the MUSELAB_PROVIDER_*_API_KEY custom namespace."""
    k = (env_key or "").strip()
    if k in _allowed_env_keys():
        return
    if _CUSTOM_ENV_KEY_RE.match(k):
        return
    raise ValueError(
        "env_key must be a built-in provider key or match "
        "MUSELAB_PROVIDER_<NAME>_API_KEY")


def _builtin_id(p: Provider) -> str:
    """Stable id for a built-in provider, derived from its (code-fixed)
    prefix. Built-in prefixes never change across releases, so this is a
    durable key for overrides even after the user edits the displayed URL."""
    return "b:" + p.prefix


def _slug(text: str) -> str:
    """Lowercase alnum slug from arbitrary text (used to mint ids / env keys
    for user-created providers)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "provider"


# Process-wide caches keyed on the override file's (mtime_ns, size). catalog()
# /lookup() consult the store many times per request (the cost dashboard runs
# lookup() once per assistant JSONL row → thousands of calls), so re-reading +
# re-parsing the JSON, then rebuilding+sorting the Provider list, each call is
# pure waste. Caching by stat() signature keeps the "edit Settings → effective
# next request, no restart" contract: _save_overrides() rewrites the file via
# atomic rename, which bumps mtime, which misses the cache on the next load.
# A cheap stat() syscall stays on every call; the read + json.loads + normalize
# (_load_overrides) and the build + sort (catalog) are skipped on a hit.
_OVERRIDES_CACHE: tuple[tuple[int, int], dict] | None = None
_CATALOG_CACHE: tuple[tuple[int, int] | None, tuple[Provider, ...]] | None = None
_OVERRIDES_CACHE_LOCK = threading.Lock()


def _overrides_stat_key() -> tuple[int, int] | None:
    """(mtime_ns, size) signature of the override file, or None if it's
    absent/unreadable. None is a stable key meaning "no file" — both caches
    treat it as a hittable state so a factory-default install (file never
    created) still benefits."""
    try:
        st = OVERRIDES_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except (FileNotFoundError, OSError):
        return None


def _parse_overrides(text: str) -> dict:
    """Normalize the raw JSON text into the canonical store shape. Tolerates
    malformed content by returning the empty shape."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"providers": {}, "deleted": [], "anthropic_models": None}
    if not isinstance(data, dict):
        return {"providers": {}, "deleted": [], "anthropic_models": None}
    prov = data.get("providers")
    deleted = data.get("deleted")
    # Anthropic/Claude is special-cased (OAuth-or-key auth, no editable
    # endpoint/prefix), but its MODEL LIST is user-editable like any other
    # provider's. None = "use the built-in default list"; a list = override.
    am = data.get("anthropic_models")
    return {
        "providers": prov if isinstance(prov, dict) else {},
        "deleted": [d for d in deleted if isinstance(d, str)] if isinstance(deleted, list) else [],
        "anthropic_models": [str(m) for m in am] if isinstance(am, list) else None,
    }


def _load_overrides() -> dict:
    """Read the override store, cached by the file's (mtime_ns, size). Tolerates
    a missing / malformed file by returning the empty shape.

    Returns a deep copy so write-path callers (which mutate `store` in place
    before _save_overrides) can't corrupt the shared cache."""
    global _OVERRIDES_CACHE
    key = _overrides_stat_key()
    if key is None:
        return {"providers": {}, "deleted": [], "anthropic_models": None}
    with _OVERRIDES_CACHE_LOCK:
        cached = _OVERRIDES_CACHE
        if cached is not None and cached[0] == key:
            return copy.deepcopy(cached[1])
    try:
        text = OVERRIDES_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {"providers": {}, "deleted": [], "anthropic_models": None}
    store = _parse_overrides(text)
    with _OVERRIDES_CACHE_LOCK:
        _OVERRIDES_CACHE = (key, store)
    return copy.deepcopy(store)


def _save_overrides(store: dict) -> None:
    from .settings import atomic_write_text
    out = {"providers": store.get("providers", {}),
           "deleted": store.get("deleted", [])}
    # Only persist the anthropic model override when it's actually set, so a
    # factory-default install keeps a clean two-key file.
    am = store.get("anthropic_models")
    if isinstance(am, list):
        out["anthropic_models"] = am
    atomic_write_text(
        OVERRIDES_PATH,
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
    )


def _provider_from_def(pid: str, d: dict, base: Provider | None) -> Provider:
    """Build a Provider from a stored override dict, falling back to `base`
    (the built-in) for any field the override omits. For user-created
    providers base is None and the dict must be self-complete."""
    def pick(key: str, default):
        v = d.get(key)
        return v if v is not None else default
    base_url = pick("base_url", base.base_url if base else "")
    prefix = pick("prefix", base.prefix if base else "")
    env_key = pick("env_key", base.env_key if base else "")
    display = pick("display", base.display if base else prefix)
    # models stored as a flat list of ids; label == id for user-touched
    # providers (built-in pretty labels are only kept when NOT overridden).
    raw_models = d.get("models")
    if isinstance(raw_models, list) and raw_models:
        models = tuple((str(m), str(m)) for m in raw_models)
    elif base is not None:
        models = base.models
    else:
        models = ()
    return Provider(
        prefix=prefix,
        base_url=base_url,
        env_key=env_key,
        display=display,
        models=models,
        supports_thinking=base.supports_thinking if base else True,
        max_output_tokens=base.max_output_tokens if base else None,
        id=pid,
    )


def catalog() -> tuple[Provider, ...]:
    """The EFFECTIVE provider list: built-ins (minus user-deleted) with any
    overrides applied, followed by user-created providers. This is what every
    routing / UI path should consult — `CATALOG` is only the factory default.

    Memoized by the override file's stat signature so Settings edits still take
    effect on the next request without a restart (the rename bumps mtime →
    cache miss), but the hot read path — lookup() runs this once per usage-model
    resolution — skips the rebuild + sort entirely on a hit. The returned tuple
    holds immutable Provider dataclasses that no reader mutates, so it's safe to
    share without copying."""
    global _CATALOG_CACHE
    key = _overrides_stat_key()
    with _OVERRIDES_CACHE_LOCK:
        cached = _CATALOG_CACHE
        if cached is not None and cached[0] == key:
            return cached[1]
    store = _load_overrides()
    overrides = store["providers"]
    deleted = set(store["deleted"])
    out: list[Provider] = []
    seen: set[str] = set()
    for bp in CATALOG:
        bid = _builtin_id(bp)
        seen.add(bid)
        if bid in deleted:
            continue
        if bid in overrides and isinstance(overrides[bid], dict):
            out.append(_provider_from_def(bid, overrides[bid], bp))
        else:
            out.append(replace(bp, id=bid))
    # User-created providers: any override id that isn't a built-in.
    for pid, d in overrides.items():
        if pid in seen or pid in deleted or not isinstance(d, dict):
            continue
        out.append(_provider_from_def(pid, d, None))
    result = tuple(out)
    with _OVERRIDES_CACHE_LOCK:
        _CATALOG_CACHE = (key, result)
    return result


def get_provider(pid: str) -> Provider | None:
    """Effective provider by stable id."""
    for p in catalog():
        if p.id == pid:
            return p
    return None


def _builtin_by_id(pid: str) -> Provider | None:
    for bp in CATALOG:
        if _builtin_id(bp) == pid:
            return bp
    return None


def validate_provider_fields(base_url: str, prefix: str, models: list[str],
                             *, this_id: str | None = None) -> None:
    """Raise ValueError if a provider's user-supplied fields are malformed or
    would break routing. `this_id` excludes the provider being edited from the
    prefix-uniqueness check."""
    url = (base_url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("endpoint must be an http(s) URL")
    pre = (prefix or "").strip()
    if not _PREFIX_RE.match(pre):
        raise ValueError("invalid prefix (letters, digits, . _ : + -; ≤40 chars)")
    # Prefix must be unique across the effective catalog so longest-prefix
    # lookup() routes deterministically.
    for p in catalog():
        if p.id != this_id and p.prefix == pre:
            raise ValueError(f"prefix '{pre}' already used by another provider")
    seen: set[str] = set()
    for m in models:
        mid = (m or "").strip()
        if not mid:
            continue
        if not _MODEL_ID_RE.match(mid):
            raise ValueError(f"invalid model id: {mid!r}")
        if not mid.startswith(pre):
            raise ValueError(f"model '{mid}' must start with the prefix '{pre}'")
        low = mid.lower()
        if low in seen:
            raise ValueError(f"duplicate model id: {mid}")
        seen.add(low)


def upsert_provider(*, pid: str | None, base_url: str, prefix: str,
                    display: str, env_key: str, models: list[str]) -> Provider:
    """Create or update a provider override. Returns the saved effective
    Provider. api-key is handled separately (stays in .env). Raises ValueError
    on invalid input."""
    models = [str(m).strip() for m in models if str(m).strip()]
    validate_provider_fields(base_url, prefix, models, this_id=pid)
    store = _load_overrides()
    # New provider: mint a stable custom id + an env-key slot if none given.
    if not pid:
        pid = "c:" + _slug(base_url)
        # Avoid collision with an existing id.
        n = 1
        base_pid = pid
        existing = set(store["providers"].keys()) | {_builtin_id(b) for b in CATALOG}
        while pid in existing:
            n += 1
            pid = f"{base_pid}-{n}"
    if not env_key:
        env_key = "MUSELAB_PROVIDER_" + _slug(base_url).upper().replace("-", "_") + "_API_KEY"
    # User-supplied env_key flows into .env writes (api_settings._write_env) —
    # without this gate a crafted value could overwrite MUSELAB_TOKEN, PATH,
    # or another provider's credential.
    validate_env_key(env_key)
    entry = {
        "base_url": base_url.strip(),
        "prefix": prefix.strip(),
        "display": (display or prefix).strip(),
        "env_key": env_key.strip(),
        "models": models,
    }
    store["providers"][pid] = entry
    # If this id was previously deleted (e.g. re-adding a built-in), un-delete.
    store["deleted"] = [d for d in store["deleted"] if d != pid]
    _save_overrides(store)
    return _provider_from_def(pid, entry, _builtin_by_id(pid))


def delete_provider(pid: str) -> bool:
    """Remove a provider. Built-ins are tombstoned in `deleted` so they don't
    reappear; user-created providers are dropped outright. Returns True if
    anything changed."""
    store = _load_overrides()
    changed = False
    if pid in store["providers"]:
        del store["providers"][pid]
        changed = True
    if _builtin_by_id(pid) is not None and pid not in store["deleted"]:
        store["deleted"].append(pid)
        changed = True
    if changed:
        _save_overrides(store)
    return changed


def restore_provider(pid: str) -> bool:
    """Restore a built-in provider to its factory default by dropping its
    override + tombstone. No-op (returns False) for user-created providers
    (nothing to restore to). Caller should DELETE those instead."""
    # Anthropic isn't in CATALOG (special auth), but its model list IS
    # overridable — route restore to the dedicated Claude-models reset.
    if pid == "anthropic":
        return restore_anthropic_models()
    if _builtin_by_id(pid) is None:
        return False
    store = _load_overrides()
    changed = False
    if pid in store["providers"]:
        del store["providers"][pid]
        changed = True
    if pid in store["deleted"]:
        store["deleted"] = [d for d in store["deleted"] if d != pid]
        changed = True
    if changed:
        _save_overrides(store)
    return changed


def provider_meta() -> list[dict]:
    """UI-facing metadata for every effective provider (built-in + custom).
    Excludes secrets / env-state (configured / masked / disabled) — the api
    layer layers those on, since they depend on .env which endpoints.py
    doesn't own. `is_overridden` lets the UI light up the per-provider
    'restore default' button only when there's something to restore."""
    store = _load_overrides()
    overridden = set(store["providers"].keys())
    out: list[dict] = []
    for p in catalog():
        out.append({
            "id": p.id,
            "display": p.display,
            "base_url": p.base_url,
            "prefix": p.prefix,
            "env_key": p.env_key,
            "models": [mid for mid, _ in p.models],
            "supports_thinking": p.supports_thinking,
            "is_builtin": _builtin_by_id(p.id) is not None,
            "is_overridden": p.id in overridden,
            "probe_model": p.models[0][0] if p.models else "",
        })
    return out


# Pretty labels for Claude (Pro OAuth) models — the IDs themselves are ugly
# (e.g. "claude-haiku-4-5-20251001") so we display human-friendly names.
CLAUDE_LABELS: dict[str, str] = {
    "claude-opus-4-8":              "Opus 4.8",
    "claude-opus-4-7":              "Opus 4.7",
    "claude-sonnet-4-6":            "Sonnet 4.6",
    "claude-haiku-4-5-20251001":    "Haiku 4.5",
}

# Factory-default Claude model list (the picker order). User-editable via
# Settings → overridden in provider_overrides.json["anthropic_models"]; this
# tuple is what `restore` reverts to and what a clean install shows.
ANTHROPIC_DEFAULT_MODELS: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-8",
    "claude-opus-4-7",
)


def _overrides_get(key: str, default=None):
    """Read a SINGLE key from the override store without deep-copying the whole
    store. _load_overrides() returns a `copy.deepcopy` of the entire providers
    map (to protect write-path callers); read-only callers that only inspect
    one field paid that full deepcopy for nothing. Returns the cached value
    directly — callers MUST treat it as read-only and not mutate in place."""
    skey = _overrides_stat_key()
    if skey is None:
        return default
    with _OVERRIDES_CACHE_LOCK:
        cached = _OVERRIDES_CACHE
        if cached is not None and cached[0] == skey:
            return cached[1].get(key, default)
    # Cache miss: _load_overrides() populates the cache (and returns a fresh
    # deep copy we can safely read from without aliasing the cache).
    return _load_overrides().get(key, default)


def anthropic_models() -> list[str]:
    """Effective Claude model id list: the user override if set, else the
    factory default. Read from disk each call so Settings edits apply on the
    next request without a restart (matches the rest of the catalog)."""
    am = _overrides_get("anthropic_models")
    return list(am) if am else list(ANTHROPIC_DEFAULT_MODELS)


def anthropic_models_overridden() -> bool:
    """True when the Claude model list has been customized (so the UI can
    offer a 'restore default' affordance)."""
    return _overrides_get("anthropic_models") is not None


def set_anthropic_models(models: list[str]) -> None:
    """Persist a custom Claude model list. Validates non-empty + that every id
    looks like a Claude model (the auth/endpoint are fixed to Anthropic, so a
    non-`claude-` id here would 404). Raises ValueError on bad input."""
    cleaned = [str(m).strip() for m in (models or []) if str(m).strip()]
    if not cleaned:
        raise ValueError("model list cannot be empty")
    for m in cleaned:
        if not m.lower().startswith("claude-"):
            raise ValueError(f"not a Claude model id: {m!r} (must start with 'claude-')")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("duplicate model ids")
    store = _load_overrides()
    store["anthropic_models"] = cleaned
    _save_overrides(store)


def restore_anthropic_models() -> bool:
    """Drop the Claude model override (revert to factory default). Returns
    True if there was an override to remove, False if already default."""
    store = _load_overrides()
    if store.get("anthropic_models") is None:
        return False
    store["anthropic_models"] = None
    _save_overrides(store)
    return True


# Tier is left open (`[a-z]+`) rather than a fixed opus|sonnet|haiku allowlist:
# Anthropic ships new tiers (e.g. "fable") between our releases, and a
# user-added `claude-fable-5` was rendering as a raw id next to nicely-named
# built-ins (2026-06-10 user report). The minor-version group is optional and
# capped at 1-2 digits + a `(?=$|-)` boundary so a date suffix
# (`claude-fable-5-20260101`) is NOT mistaken for a minor version.
_CLAUDE_LABEL_RE = __import__("re").compile(
    r"^claude-([a-z]+)-(\d+)(?:[-.](\d{1,2})(?=$|-))?", __import__("re").IGNORECASE)


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
            # Minor is optional now (single-version tiers like `claude-fable-5`
            # have no minor) — render "Fable 5", not "Fable 5.None".
            ver = m.group(2) if m.group(3) is None else f"{m.group(2)}.{m.group(3)}"
            return f"{kind} {ver}"
    p = lookup(model)
    if p is not None:
        low = model.lower()
        for mid, lab in p.models:
            if mid.lower() == low:
                return lab
    return model


# Memoized longest-prefix-first ordering of the catalog. catalog() returns the
# SAME cached tuple object on a hit, so identity comparison lets us reuse the
# sorted list instead of re-sorting on every lookup() — the cost dashboard
# calls lookup() once per assistant JSONL row (thousands per request), and each
# call was paying a full sort of the provider list.
_SORTED_CATALOG_CACHE: tuple[object, list[Provider]] | None = None


def lookup(model: str) -> Provider | None:
    """Find the provider for a given model id (by longest matching prefix).
    Case-insensitive: third-party vendors sometimes return mixed-case in
    `usage.model` (e.g. MiniMax returns `MiniMax-M2.7`), and we want those
    to route to the same provider as the lowercase catalog entry."""
    global _SORTED_CATALOG_CACHE
    low = (model or "").lower()
    cat = catalog()
    cached = _SORTED_CATALOG_CACHE
    if cached is None or cached[0] is not cat:
        ordered = sorted(cat, key=lambda x: -len(x.prefix))
        _SORTED_CATALOG_CACHE = (cat, ordered)
    else:
        ordered = cached[1]
    for p in ordered:
        if p.prefix and low.startswith(p.prefix.lower()):
            return p
    return None


def is_third_party(model: str) -> bool:
    """True if this model goes through a third-party Anthropic-compat endpoint."""
    return lookup(model) is not None


def normalize_model_id(model: str) -> str:
    """Strip the provider's INTERNAL routing prefix before sending the id to
    the vendor. Convention: a prefix ending in ':' is a muselab-internal tag
    (used to disambiguate two endpoints that serve the same real model id,
    e.g. domestic vs international mirrors) and is stripped; an ordinary prefix
    like 'deepseek-' is part of the vendor's real model id and kept as-is.

    Generalised from the old hard-coded 'qwen-intl:' / 'minimax-intl:' so that
    user-created providers using a colon-tag prefix work the same way. The
    legacy literals are kept as a fallback in case lookup() can't resolve the
    provider (e.g. it was deleted)."""
    p = lookup(model)
    if p and p.prefix.endswith(":") and model.startswith(p.prefix):
        return model[len(p.prefix):]
    for pre in ("qwen-intl:", "minimax-intl:"):
        if model.startswith(pre):
            return model[len(pre):]
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
    REPLACEMENT (not merge). We forward only a minimal allowlist of process /
    proxy / TLS vars (see below) so the agent subprocess never inherits
    MUSELAB_TOKEN or other providers' API keys."""
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
    # Build a MINIMAL allowlisted env rather than copying all of os.environ.
    # The SDK hands this dict to the CLI subprocess as a full env REPLACEMENT,
    # and that subprocess runs an internet-capable, prompt-injectable agent
    # (often under bypassPermissions). Inheriting the whole environment would
    # leak MUSELAB_TOKEN and every *_API_KEY (the agent could `echo
    # $MUSELAB_TOKEN` or exfiltrate other vendors' keys). The CLI only needs:
    #   - process basics (PATH/HOME/shell/locale/tmp) to spawn + find its config
    #   - proxy / TLS-CA vars so on-prem & proxied deployments still reach the
    #     vendor endpoint
    # The vendor's own key is injected below as ANTHROPIC_API_KEY, so we do NOT
    # forward the raw <VENDOR>_API_KEY either.
    _ENV_ALLOWLIST = (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    )
    merged = {k: v for k in _ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}
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
        # Model list is user-editable (anthropic_models override); falls back
        # to ANTHROPIC_DEFAULT_MODELS. Honour the Settings disable toggle and
        # use friendly labels where we know them, else a derived label.
        claude_items = [
            {"label": label_for(m), "model": m}
            for m in anthropic_models()
            if m not in disabled_models
        ]
        if claude_items:
            # Anthropic's own endpoint always handles the standard thinking
            # config (it IS the standard) → supports_thinking True.
            groups.append({"group": "Claude", "items": claude_items,
                           "supports_thinking": True})
    for p in catalog():
        if not p.models:
            continue
        if not os.environ.get(p.env_key):
            continue
        # Skip provider if disabled in Settings. Disabled set carries the
        # provider's stable id; for backward-compat we also honour the legacy
        # first-model-id form that earlier builds wrote.
        if p.id in disabled_models or p.models[0][0] in disabled_models:
            continue
        groups.append({
            "group": p.display,
            "items": [{"label": label, "model": mid} for mid, label in p.models],
            # Provider-level flag — some vendors (e.g. Qianfan) reject the
            # standard thinking config; the FE uses this to hide the thinking
            # toggle for models that can't honor it anyway.
            "supports_thinking": p.supports_thinking,
        })
    return groups
