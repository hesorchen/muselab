"""Third-party provider catalog: prefix→endpoint+key dispatch."""
import sys
import tempfile
from pathlib import Path

import pytest


def _reload_endpoints(monkeypatch, env: dict):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    if "backend.endpoints" in sys.modules:
        del sys.modules["backend.endpoints"]
    from backend import endpoints as ep   # type: ignore[import]
    # Keep catalog tests hermetic: a developer's local provider_overrides.json
    # (gitignored runtime state) must not change built-in routing assertions.
    monkeypatch.setattr(ep, "OVERRIDES_PATH", Path(tempfile.mkdtemp()) / "provider_overrides.json")
    ep._OVERRIDES_CACHE = None
    ep._CATALOG_CACHE = None
    ep._SORTED_CATALOG_CACHE = None
    return ep


def test_lookup_deepseek(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "test"})
    p = ep.lookup("deepseek-v4-pro")
    assert p is not None
    assert p.base_url.endswith("/anthropic")
    assert p.env_key == "DEEPSEEK_API_KEY"


def test_lookup_unknown_model(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {})
    assert ep.lookup("gpt-5") is None
    assert ep.lookup("claude-sonnet-4-6") is None  # claude not in catalog


def test_env_override_missing_key(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": None})
    assert ep.env_override("deepseek-v4-pro") is None


def test_env_override_present(monkeypatch):
    """Both ANTHROPIC_API_KEY (x-api-key) and ANTHROPIC_AUTH_TOKEN (Bearer) are
    set to the vendor key — different vendors prefer different headers; setting
    both means the request authenticates regardless. CLI OAuth fallback envs
    are zeroed so a 401 from the vendor can't silently re-route to Anthropic."""
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "sk-test"})
    env = ep.env_override("deepseek-v4-pro")
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"].startswith("https://api.deepseek.com")
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["CLAUDE_OAUTH_TOKEN"] == ""


def test_is_third_party(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {})
    assert ep.is_third_party("deepseek-v4-pro")
    assert ep.is_third_party("glm-5")
    assert ep.is_third_party("minimax-m2.7")
    assert ep.is_third_party("kimi-k2.6")
    assert ep.is_third_party("qwen3-max")
    assert ep.is_third_party("qwen-plus")
    assert ep.is_third_party("mimo-v2.5-pro")
    assert ep.is_third_party("codex:gpt-5.5")
    assert not ep.is_third_party("claude-sonnet-4-6")


def test_available_groups_only_lists_configured(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {
        # ANTHROPIC_API_KEY makes has_anthropic_auth() True without
        # needing a ~/.claude/.credentials.json on disk — required so
        # this test passes on CI runners (developer machines have the
        # credentials file from `claude login` and historically hid
        # the gap).
        "ANTHROPIC_API_KEY": "x",
        "DEEPSEEK_API_KEY": "x",
        "ZHIPUAI_API_KEY": None,
        "MINIMAX_API_KEY": None,
    })
    groups = ep.available_groups()
    names = {g["group"] for g in groups}
    assert "Claude" in names
    assert "DeepSeek" in names
    assert "智谱 GLM" not in names
    assert "MiniMax" not in names


@pytest.mark.parametrize("model,expected_host", [
    ("deepseek-v4-pro",         "api.deepseek.com"),
    ("glm-5",                   "bigmodel.cn"),
    ("minimax-m2.7",            "minimaxi.com"),
    # Re-added / new providers (2026-05-22) — keep the regression matrix
    # honest: a future endpoint URL typo would route a vendor's traffic to
    # the wrong host and we'd only catch it via "all my Kimi turns fail".
    # NOTE: Kimi moved to api.moonshot.cn (was .ai) per Moonshot's 2026-05
    # endpoint consolidation. Qwen domestic dashscope.aliyuncs.com is the
    # default; international users explicitly opt in via "qwen-intl:" prefix.
    ("kimi-k2.6",               "api.moonshot.cn"),
    ("kimi-k2-thinking",        "api.moonshot.cn"),
    ("qwen3-max",               "dashscope.aliyuncs.com"),
    ("qwen-plus",               "dashscope.aliyuncs.com"),
    ("qwen3.5-flash",           "dashscope.aliyuncs.com"),
    ("qwen-intl:qwen3-max",     "dashscope-intl.aliyuncs.com"),
    ("mimo-v2.5-pro",           "api.xiaomimimo.com"),
    # Baidu Qianfan — aggregator hosting ERNIE + cross-vendor models
    ("ernie-4.5-turbo-128k",    "qianfan.baidubce.com"),
    # Codex Gateway is a local Anthropic-compatible sidecar by default.
    ("codex:gpt-5.5",           "127.0.0.1:8317"),
    # NOTE: "deepseek-v3.2" via Qianfan is documented in Qianfan's catalog
    # but lookup() matches by prefix → DeepSeek's own provider wins (the
    # "deepseek-" prefix entry registers first). Users wanting Qianfan-
    # hosted DeepSeek must call Qianfan directly. Not in the routing matrix.
])
def test_all_providers_route_to_correct_host(monkeypatch, model, expected_host):
    """Each catalog entry's base_url contains the expected vendor domain."""
    ep = _reload_endpoints(monkeypatch, {})
    p = ep.lookup(model)
    assert p is not None
    assert expected_host in p.base_url


def test_env_override_replaces_inherited_anthropic_key(monkeypatch):
    """Even if the parent process has ANTHROPIC_API_KEY set to a real Anthropic
    key, env_override must overwrite it with the vendor's key. Otherwise the
    request would go to the vendor's base_url but authenticate as Anthropic →
    vendor 401 → CLI OAuth fallback → bills Claude (the original Opus-billing
    bug we're guarding against)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "sk-ds"})
    env = ep.env_override("deepseek-v4-pro")
    assert env["ANTHROPIC_API_KEY"] == "sk-ds"      # vendor key wins
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ds"


def test_longest_prefix_wins(monkeypatch):
    """If two prefixes both match, the longer one should win (defensive)."""
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "x"})
    p = ep.lookup("deepseek-anything-here")
    assert p is not None and p.prefix == "deepseek-"


def test_codex_gateway_strips_internal_prefix_and_honors_base_url(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {
        "CODEX_GATEWAY_API_KEY": "local-secret",
        "CODEX_GATEWAY_BASE_URL": "http://127.0.0.1:9876",
    })
    assert ep.normalize_model_id("codex:gpt-5.5") == "gpt-5.5"
    p = ep.lookup("codex:gpt-5.5")
    assert p is not None
    assert p.supports_effort is True
    assert p.supports_thinking is False
    env = ep.env_override("codex:gpt-5.5")
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9876"
    assert env["ANTHROPIC_API_KEY"] == "local-secret"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
    groups = ep.available_groups()
    codex = next(g for g in groups if g["group"] == "Codex Gateway")
    assert codex["supports_effort"] is True
    assert codex["supports_thinking"] is False


def test_legacy_custom_codex_provider_gets_effort_capability(monkeypatch):
    """Older installs may have a custom raw `gpt-*` Codex sidecar provider.

    Custom providers do not persist capability flags, so the known local Codex
    sidecar shape must infer Effort support for the frontend controls.
    """
    ep = _reload_endpoints(monkeypatch, {"MUSELAB_PROVIDER_CODEX_API_KEY": "local-secret"})
    ep.OVERRIDES_PATH.write_text(
        '{"providers":{"c:http-127-0-0-1-8317":{'
        '"display":"Codex (ChatGPT subscription)",'
        '"prefix":"gpt-",'
        '"base_url":"http://127.0.0.1:8317",'
        '"env_key":"MUSELAB_PROVIDER_CODEX_API_KEY",'
        '"models":["gpt-5.5","gpt-5.4"]'
        '}}}',
        encoding="utf-8",
    )
    ep._OVERRIDES_CACHE = None
    ep._CATALOG_CACHE = None
    ep._SORTED_CATALOG_CACHE = None

    p = ep.lookup("gpt-5.5")
    assert p is not None
    assert p.supports_effort is True
    assert p.supports_thinking is False
    assert p.max_output_tokens == 128000
    groups = ep.available_groups()
    codex = next(g for g in groups if g["group"] == "Codex (ChatGPT subscription)")
    assert codex["supports_effort"] is True
    assert codex["supports_thinking"] is False


def test_env_override_merges_with_os_environ(monkeypatch):
    """SDK passes env as full subprocess replacement; we must include PATH/HOME
    or claude CLI crashes with exit 1."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "k"})
    env = ep.env_override("deepseek-v4-pro")
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"
    assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")


def test_all_catalog_providers_have_valid_fields(monkeypatch):
    """Every catalog entry should be self-consistent and complete.

    Prefix-ending convention was relaxed 2026-05-22 when Qwen joined: model
    ids alternate `qwen-plus` and `qwen3-max`, so the shared prefix is the
    bare string `qwen` (no trailing dash). The harder invariant — every
    model id starts with its provider's prefix — is checked below regardless.
    """
    ep = _reload_endpoints(monkeypatch, {})
    # Aggregator providers host cross-vendor model IDs (e.g. Qianfan exposes
    # `deepseek-v3.2` alongside its native `ernie-*` line). For them the
    # prefix names the primary brand only, not the model-ID convention.
    AGGREGATOR_ENV_KEYS = {"QIANFAN_API_KEY"}
    for p in ep.CATALOG:
        assert p.prefix, "prefix must be non-empty"
        assert p.prefix == p.prefix.lower(), f"prefix should be lowercase: {p.prefix}"
        assert (p.base_url.startswith("https://")
                or p.base_url.startswith("http://127.0.0.1:")), \
            f"base_url should be https or loopback http: {p.base_url}"
        if p.env_key != "CODEX_GATEWAY_API_KEY":
            assert "anthropic" in p.base_url, "base_url should hit /anthropic endpoint"
        assert p.env_key.endswith("_API_KEY"), f"env_key convention: {p.env_key}"
        assert len(p.models) > 0, f"provider {p.prefix} has no models listed"
        if p.env_key in AGGREGATOR_ENV_KEYS:
            # Aggregator: just check labels are non-empty; model IDs can be any
            # vendor's native form.
            for _mid, label in p.models:
                assert label, "label must be non-empty"
            continue
        for mid, label in p.models:
            assert mid.startswith(p.prefix), f"model {mid} doesn't match prefix {p.prefix}"
            assert label, "label must be non-empty"


def test_available_groups_claude_always_first(monkeypatch):
    # ANTHROPIC_API_KEY so Claude shows up on CI (see comment in
    # test_available_groups_only_lists_configured).
    ep = _reload_endpoints(monkeypatch, {
        "ANTHROPIC_API_KEY": "x",
        "DEEPSEEK_API_KEY": "x",
    })
    groups = ep.available_groups()
    assert groups[0]["group"] == "Claude"
