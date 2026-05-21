"""Third-party provider catalog: prefix→endpoint+key dispatch."""
import sys

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
    assert not ep.is_third_party("claude-sonnet-4-6")


def test_available_groups_only_lists_configured(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {
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
    ("deepseek-v4-pro", "api.deepseek.com"),
    ("glm-5", "bigmodel.cn"),
    ("minimax-m2.7", "minimaxi.com"),
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
    """Every catalog entry should be self-consistent and complete."""
    ep = _reload_endpoints(monkeypatch, {})
    for p in ep.CATALOG:
        assert p.prefix.endswith("-"), f"prefix should end with '-': {p.prefix}"
        assert p.base_url.startswith("https://"), f"base_url should be https: {p.base_url}"
        assert "anthropic" in p.base_url, "base_url should hit /anthropic endpoint"
        assert p.env_key.endswith("_API_KEY"), f"env_key convention: {p.env_key}"
        assert len(p.models) > 0, f"provider {p.prefix} has no models listed"
        for mid, label in p.models:
            assert mid.startswith(p.prefix), f"model {mid} doesn't match prefix {p.prefix}"
            assert label, "label must be non-empty"


def test_available_groups_claude_always_first(monkeypatch):
    ep = _reload_endpoints(monkeypatch, {"DEEPSEEK_API_KEY": "x"})
    groups = ep.available_groups()
    assert groups[0]["group"] == "Claude"
