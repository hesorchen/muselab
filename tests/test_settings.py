"""Runtime settings API: GET masking, PUT writes .env + refreshes env."""
import os
from pathlib import Path


def test_get_settings_shape(client, auth):
    r = client.get("/api/settings", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert "providers" in d and "defaults" in d and "params" in d
    keys = {p["env_key"] for p in d["providers"]}
    assert "DEEPSEEK_API_KEY" in keys
    assert "ZHIPUAI_API_KEY" in keys
    # MiMo / Qwen 暂不支持 (no Anthropic-compat endpoint)
    assert "MIMO_API_KEY" not in keys
    assert "DASHSCOPE_API_KEY" not in keys


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
        "thinking_budget": 8000,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # .env 文件确实写入
    content = fake_env.read_text()
    assert "DEEPSEEK_API_KEY=sk-newvalue" in content
    assert "MUSELAB_DEFAULT_MODEL=claude-haiku-4-5-20251001" in content
    assert "MUSELAB_THINKING_BUDGET=8000" in content
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


def test_get_settings_requires_auth(client):
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_settings_provider_count_matches_catalog(client, auth):
    """Settings UI should expose exactly the providers that have Anthropic-compat
    endpoints (4 today). New ones must be added to both PROVIDER_KEYS and CATALOG."""
    r = client.get("/api/settings", headers=auth)
    d = r.json()
    assert len(d["providers"]) == 4
    keys = {p["env_key"] for p in d["providers"]}
    assert keys == {"DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY",
                    "MINIMAX_API_KEY", "MOONSHOT_API_KEY"}
