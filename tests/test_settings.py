"""Runtime settings API: GET masking, PUT writes .env + refreshes env."""
import os


def test_get_settings_shape(client, auth):
    r = client.get("/api/settings", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert "providers" in d and "defaults" in d and "params" in d
    keys = {p["env_key"] for p in d["providers"]}
    # Original 4
    assert "DEEPSEEK_API_KEY" in keys
    assert "ZHIPUAI_API_KEY" in keys
    assert "MINIMAX_API_KEY" in keys
    # Added 2026-05-22 (Kimi / Qwen / MiMo gained Anthropic-compat endpoints)
    assert "MOONSHOT_API_KEY" in keys
    assert "DASHSCOPE_API_KEY" in keys
    assert "XIAOMI_MIMO_API_KEY" in keys


def test_get_settings_masks_existing_key(client, auth, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deadbeef12345678abcd")
    r = client.get("/api/settings", headers=auth)
    ds = next(p for p in r.json()["providers"] if p["env_key"] == "DEEPSEEK_API_KEY")
    assert ds["configured"] is True
    # mask 应该露出头尾 4 位，中间是圆点
    assert ds["masked"].startswith("sk-d") and ds["masked"].endswith("abcd")
    assert "•" in ds["masked"]
    # 完整 key 不能出现
    assert "deadbeef" not in ds["masked"]


def test_get_settings_empty_when_unset(client, auth):
    r = client.get("/api/settings", headers=auth)
    glm = next(p for p in r.json()["providers"] if p["env_key"] == "ZHIPUAI_API_KEY")
    assert glm["configured"] is False
    assert glm["masked"] == ""


def test_put_settings_writes_env_and_refreshes(client, auth, monkeypatch, tmp_path):
    # 隔离 .env：让 api_settings 写到 tmp_path
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("# header\nMUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)

    r = client.put("/api/settings", headers=auth, json={
        "deepseek_api_key": "sk-newvalue",
        "default_model": "claude-haiku-4-5-20251001",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # .env 文件确实写入
    content = fake_env.read_text()
    assert "DEEPSEEK_API_KEY=sk-newvalue" in content
    assert "MUSELAB_DEFAULT_MODEL=claude-haiku-4-5-20251001" in content
    # 原有内容保留
    assert "MUSELAB_TOKEN=existing-test-token-1234567890" in content

    # os.environ 同步更新
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-newvalue"
    assert os.environ["MUSELAB_DEFAULT_MODEL"] == "claude-haiku-4-5-20251001"


def test_put_settings_skips_empty_values(client, auth, monkeypatch, tmp_path):
    """Empty / None provider key = 'don't touch'; existing value preserved."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("DEEPSEEK_API_KEY=keep-me\nMUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "keep-me")

    r = client.put("/api/settings", headers=auth, json={
        "deepseek_api_key": "",   # empty -> ignored
        "default_model": "claude-opus-4-7",
    })
    assert r.status_code == 200
    content = fake_env.read_text()
    assert "DEEPSEEK_API_KEY=keep-me" in content
    assert "MUSELAB_DEFAULT_MODEL=claude-opus-4-7" in content


def test_put_settings_updates_existing_line(client, auth, monkeypatch, tmp_path):
    """Existing key in .env gets replaced, not duplicated."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("DEEPSEEK_API_KEY=old\nMUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)

    client.put("/api/settings", headers=auth, json={"deepseek_api_key": "new"})
    content = fake_env.read_text()
    assert content.count("DEEPSEEK_API_KEY=") == 1
    assert "DEEPSEEK_API_KEY=new" in content


def test_put_settings_appends_new_key(client, auth, monkeypatch, tmp_path):
    """If key didn't exist in .env, it's appended."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("MUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)

    client.put("/api/settings", headers=auth, json={"minimax_api_key": "mx-key"})
    content = fake_env.read_text()
    assert "MINIMAX_API_KEY=mx-key" in content


def test_put_settings_preserves_comments(client, auth, monkeypatch, tmp_path):
    """Comments and blank lines in .env must survive a write."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "# muselab config\n"
        "\n"
        "MUSELAB_TOKEN=existing-test-token-1234567890\n"
        "# next is provider keys\n"
        "DEEPSEEK_API_KEY=old\n"
    )
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)

    client.put("/api/settings", headers=auth, json={"deepseek_api_key": "new"})
    content = fake_env.read_text()
    assert "# muselab config" in content
    assert "# next is provider keys" in content
    assert "DEEPSEEK_API_KEY=new" in content


def test_put_settings_requires_auth(client, monkeypatch, tmp_path):
    from backend import api_settings as api_s
    monkeypatch.setattr(api_s, "ENV_PATH", tmp_path / ".env")
    r = client.put("/api/settings", json={"deepseek_api_key": "x"})
    assert r.status_code == 401


def test_put_settings_generic_provider_keys_writes_new_vendors(
    client, auth, monkeypatch, tmp_path
):
    """The generic `provider_keys` map (added 2026-05-22) lets new vendors
    write through Settings without adding individual Pydantic fields. This
    is how Kimi / Qwen / MiMo reach .env — they don't have legacy
    `moonshot_api_key` etc. fields."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("MUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)
    r = client.put("/api/settings", headers=auth, json={
        "provider_keys": {
            "MOONSHOT_API_KEY":     "sk-kimi-test",
            "DASHSCOPE_API_KEY":    "sk-qwen-test",
            "XIAOMI_MIMO_API_KEY":  "sk-mimo-test",
        },
    })
    assert r.status_code == 200
    content = fake_env.read_text()
    assert "MOONSHOT_API_KEY=sk-kimi-test" in content
    assert "DASHSCOPE_API_KEY=sk-qwen-test" in content
    assert "XIAOMI_MIMO_API_KEY=sk-mimo-test" in content


def test_put_settings_generic_provider_keys_rejects_unknown_env(
    client, auth, monkeypatch, tmp_path
):
    """Whitelist via PROVIDER_KEYS — random env names sent through the
    generic channel are silently dropped, not written to .env. Guards
    against a credential-stealing route in the unlikely event of XSS."""
    from backend import api_settings as api_s
    fake_env = tmp_path / ".env"
    fake_env.write_text("MUSELAB_TOKEN=existing-test-token-1234567890\n")
    monkeypatch.setattr(api_s, "ENV_PATH", fake_env)
    client.put("/api/settings", headers=auth, json={
        "provider_keys": {
            "PATH":                 "/tmp/evil",
            "MUSELAB_TOKEN":        "stolen",
            "MOONSHOT_API_KEY":     "sk-legit",  # this one survives
        },
    })
    content = fake_env.read_text()
    assert "MOONSHOT_API_KEY=sk-legit" in content
    assert "PATH=/tmp/evil" not in content
    # MUSELAB_TOKEN was already there; should NOT be overwritten via this
    # endpoint (it's not in PROVIDER_KEYS — defence in depth).
    assert "MUSELAB_TOKEN=stolen" not in content
    assert "MUSELAB_TOKEN=existing-test-token-1234567890" in content


def test_get_settings_requires_auth(client):
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_settings_provider_count_matches_catalog(client, auth):
    """Settings UI should auto-sync with endpoints.CATALOG — PROVIDER_KEYS
    is now derived from it (api_settings._build_provider_keys), so adding
    a Provider to the catalog automatically surfaces a Settings input row
    without parallel code edits. This test guards that contract."""
    from backend import endpoints as _ep
    r = client.get("/api/settings", headers=auth)
    d = r.json()
    keys = {p["env_key"] for p in d["providers"]}
    # Anthropic is added explicitly (not in CATALOG — it routes via OAuth).
    # Every other catalog provider must appear.
    assert "ANTHROPIC_API_KEY" in keys
    catalog_env_keys = {p.env_key for p in _ep.CATALOG}
    assert catalog_env_keys.issubset(keys), \
        f"Settings missing catalog providers: {catalog_env_keys - keys}"
    # And we don't smuggle anything extra in.
    assert keys == {"ANTHROPIC_API_KEY"} | catalog_env_keys


def test_is_chinese_locale_zh(monkeypatch, app_module):
    """is_chinese_locale should return True for any of the standard
    zh_* values that locale env vars carry on Chinese systems."""
    from backend.settings import is_chinese_locale
    for v in ("zh_CN.UTF-8", "zh_TW.UTF-8", "zh_HK.UTF-8", "zh", "ZH_CN"):
        monkeypatch.setenv("LANG", v)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_MESSAGES", raising=False)
        assert is_chinese_locale() is True, f"failed for LANG={v}"


def test_is_chinese_locale_en_and_unset(monkeypatch, app_module):
    """is_chinese_locale should return False for non-Chinese locales
    (en_US, ja_JP, …) and when all three env vars are unset."""
    from backend.settings import is_chinese_locale
    for v in ("en_US.UTF-8", "ja_JP.UTF-8", "fr_FR.UTF-8", "C", ""):
        monkeypatch.setenv("LANG", v)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LC_MESSAGES", raising=False)
        assert is_chinese_locale() is False, f"false positive for LANG={v}"
    # all unset
    monkeypatch.delenv("LANG", raising=False)
    assert is_chinese_locale() is False


def test_is_chinese_locale_picks_up_lc_messages(monkeypatch, app_module):
    """LC_MESSAGES overrides LANG on most distros — make sure we check it."""
    from backend.settings import is_chinese_locale
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "zh_CN.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    assert is_chinese_locale() is True


# ===== Prompt cache TTL opt-in (added 2026-05-22) =====
# Defends muselab's "make long sessions cheap to resume" decision: 1h cache
# is the recommended default since Anthropic's 2026-03-06 silent regression
# from 1h → 5min global. These tests exercise configure_prompt_cache() with
# an isolated dict so they don't depend on module reload / sys.modules
# caching (which conflicts with pytest's monkeypatch teardown). Actual cache
# behaviour lives in the claude CLI subprocess — out of unit-test scope; we
# only validate the env-var contract muselab promises to the CLI.

def test_prompt_cache_default_is_1h():
    """No MUSELAB_PROMPT_CACHE_TTL → 1h opt-in by default."""
    from backend.settings import configure_prompt_cache
    env: dict[str, str] = {}
    configure_prompt_cache(env)
    assert env.get("ENABLE_PROMPT_CACHING_1H") == "1"
    assert env.get("FORCE_PROMPT_CACHING_5M") is None


def test_prompt_cache_explicit_1h():
    from backend.settings import configure_prompt_cache
    env = {"MUSELAB_PROMPT_CACHE_TTL": "1h"}
    configure_prompt_cache(env)
    assert env.get("ENABLE_PROMPT_CACHING_1H") == "1"
    assert env.get("FORCE_PROMPT_CACHING_5M") is None


def test_prompt_cache_5m_opts_out():
    """User can opt into Anthropic's regressed default (5min) explicitly.
    Must also unset any pre-existing ENABLE_PROMPT_CACHING_1H so the 5m
    choice actually takes effect (otherwise the CLI sees both flags and
    falls back to 1h)."""
    from backend.settings import configure_prompt_cache
    env = {
        "MUSELAB_PROMPT_CACHE_TTL": "5m",
        "ENABLE_PROMPT_CACHING_1H": "1",   # pre-set; must be cleared
    }
    configure_prompt_cache(env)
    assert env.get("ENABLE_PROMPT_CACHING_1H") is None
    assert env.get("FORCE_PROMPT_CACHING_5M") == "1"


def test_prompt_cache_empty_leaves_cli_default():
    """Empty string means 'don't touch' — useful if user wants whatever
    upstream Anthropic decides without muselab overriding."""
    from backend.settings import configure_prompt_cache
    env = {"MUSELAB_PROMPT_CACHE_TTL": ""}
    configure_prompt_cache(env)
    assert env.get("ENABLE_PROMPT_CACHING_1H") is None
    assert env.get("FORCE_PROMPT_CACHING_5M") is None


def test_prompt_cache_case_insensitive_and_trim():
    """`1H`, ` 1h `, ` 5M `, etc. all normalise correctly — user typos
    in .env shouldn't silently break the opt-in."""
    from backend.settings import configure_prompt_cache
    for val in ("1H", " 1h", "1h ", " 1H "):
        env: dict[str, str] = {"MUSELAB_PROMPT_CACHE_TTL": val}
        configure_prompt_cache(env)
        assert env.get("ENABLE_PROMPT_CACHING_1H") == "1", f"failed for {val!r}"
    for val in ("5M", " 5m", "5m ", " 5M "):
        env = {"MUSELAB_PROMPT_CACHE_TTL": val}
        configure_prompt_cache(env)
        assert env.get("FORCE_PROMPT_CACHING_5M") == "1", f"failed for {val!r}"


def test_prompt_cache_unknown_value_is_noop():
    """Garbage like `2h` or `forever` shouldn't crash — just be a no-op."""
    from backend.settings import configure_prompt_cache
    env: dict[str, str] = {"MUSELAB_PROMPT_CACHE_TTL": "2h"}
    configure_prompt_cache(env)
    assert env.get("ENABLE_PROMPT_CACHING_1H") is None
    assert env.get("FORCE_PROMPT_CACHING_5M") is None
