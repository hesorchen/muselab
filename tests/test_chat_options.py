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
    # Third-party models take the compatibility subclass, NOT ClaudeSDKClient
    # (see _build_and_connect_client). Patching only the latter left the real
    # SDK client in the third-party tests, and its connect() reads concrete
    # ClaudeAgentOptions attributes (session_store, can_use_tool, …) that a
    # kwargs-capturing stub doesn't have → AttributeError. Stub both so these
    # tests keep asserting on the OPTIONS we build, not on SDK internals.
    monkeypatch.setattr(chat_mod, "UnsignedThinkingCompatibleClient", FakeClient)
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
    assert "can_use_tool" not in captured
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        assert captured["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "deepseek-v4-pro"


def test_non_bypass_runtime_installs_permission_resolver(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-default-permission", "deepseek-v4-pro", "default", ""))

    assert callable(captured["can_use_tool"])


def test_plan_runtime_can_return_to_bypass_and_installs_exit_hooks(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-plan-bypass",
        "deepseek-v4-pro",
        "plan",
        "",
        plan_return_permission="bypassPermissions",
    ))

    assert captured["permission_mode"] == "plan"
    assert captured["extra_args"] == {
        "allow-dangerously-skip-permissions": None,
    }
    assert callable(captured["can_use_tool"])
    for hook_name in ("PostToolUse", "PostToolUseFailure"):
        matchers = captured["hooks"][hook_name]
        assert len(matchers) == 1
        assert matchers[0].matcher == "ExitPlanMode"
        assert len(matchers[0].hooks) == 1
        assert callable(matchers[0].hooks[0])


def test_plan_runtime_with_default_return_does_not_gain_bypass_capability(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-plan-default",
        "deepseek-v4-pro",
        "plan",
        "",
        plan_return_permission="default",
    ))

    assert captured["permission_mode"] == "plan"
    assert "extra_args" not in captured


def test_codex_gateway_effort_reaches_sdk_options(app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-secret")
    monkeypatch.setenv("CODEX_GATEWAY_BASE_URL", "http://127.0.0.1:9876")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")

    async def catalog_capability(_model):
        return {
            "context_limit": 258_400,
            "context_raw_limit": 272_000,
            "context_max_limit": 272_000,
            "context_effective_percent": 95,
            "catalog_auto_compact_threshold": 0,
            "context_limit_source": "gateway_catalog",
            "context_limit_is_estimate": False,
        }

    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability", catalog_capability)
    captured = _capture_build_options(chat_mod, monkeypatch)

    client = asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-effort", "codex:gpt-5.5", "bypassPermissions", "high"))

    assert captured["connected"] is True
    assert client is not None
    assert captured["model"] == "gpt-5.5"
    assert captured["effort"] == "high"
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9876"
    assert captured["env"]["ANTHROPIC_API_KEY"] == "local-secret"
    # Claude CLI must use the same effective window as the meter and native
    # compact preflight instead of its unrelated built-in 200K default.
    assert captured["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "258400"
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        assert captured["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "gpt-5.5"


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


def test_mem0_recall_uses_user_prompt_hook(app_module, monkeypatch, tmp_path):
    """Memory context belongs in additionalContext, never the user message."""
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-mem0-hook", "deepseek-v4-pro", "bypassPermissions", ""))

    matchers = captured["hooks"]["UserPromptSubmit"]
    assert len(matchers) == 1
    assert len(matchers[0].hooks) == 1
    assert callable(matchers[0].hooks[0])
    assert matchers[0].timeout == chat_mod.mem0.RECALL_HOOK_TIMEOUT
