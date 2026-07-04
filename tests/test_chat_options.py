import asyncio


def _capture_build_options(chat_mod, monkeypatch):
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            captured["connected"] = True

    monkeypatch.setattr(chat_mod, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(chat_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(chat_mod, "_find_session_jsonl", lambda sid: None)
    return captured


def test_third_party_provider_enables_sdk_skills(app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("MUSELAB_DISABLE_SKILLS", raising=False)
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    client = asyncio.run(chat_mod._build_and_connect_client(
        "sid-third-party-skills", "deepseek-v4-pro", "bypassPermissions", ""))

    assert captured["connected"] is True
    assert client is not None
    assert captured["skills"] == "all"
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-test"


def test_codex_gateway_effort_reaches_sdk_options(app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-secret")
    monkeypatch.setenv("CODEX_GATEWAY_BASE_URL", "http://127.0.0.1:9876")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    client = asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-effort", "codex:gpt-5.5", "bypassPermissions", "high"))

    assert captured["connected"] is True
    assert client is not None
    assert captured["model"] == "gpt-5.5"
    assert captured["effort"] == "high"
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9876"
    assert captured["env"]["ANTHROPIC_API_KEY"] == "local-secret"


def test_disable_skills_env_still_opts_out(app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("MUSELAB_DISABLE_SKILLS", "1")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-third-party-no-skills", "deepseek-v4-pro", "bypassPermissions", ""))

    assert "skills" not in captured
