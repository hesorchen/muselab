import asyncio
from datetime import datetime

import pytest


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


@pytest.mark.asyncio
async def test_cancelled_connect_disconnects_unpooled_client(
    app_module,
    monkeypatch,
    tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    connect_entered = asyncio.Event()
    disconnected = asyncio.Event()

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class CancelledConnectClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            connect_entered.set()
            await asyncio.Event().wait()

        async def disconnect(self):
            disconnected.set()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(
        endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    monkeypatch.setattr(chat_mod, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(
        chat_mod, "UnsignedThinkingCompatibleClient", CancelledConnectClient)
    monkeypatch.setattr(chat_mod, "_find_session_jsonl", lambda _sid: None)

    building = asyncio.create_task(chat_mod._build_and_connect_client(
        "sid-cancel-connect", "deepseek-v4-pro", "bypassPermissions", ""))
    await asyncio.wait_for(connect_entered.wait(), timeout=1)
    building.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(building, timeout=1)
    assert disconnected.is_set()


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
    assert [m.matcher for m in captured["hooks"]["PreToolUse"]] == [
        "AskUserQuestion"
    ]
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        assert captured["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "deepseek-v4-pro"


def test_runtime_successor_marks_live_resume_source_without_losing_provider_env(
    app_module, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import endpoints, sessions as sess

    ambient = "ambient-value-must-not-leak"
    monkeypatch.setenv("CLAUDE_CODE_RESUME_SOURCE_ALIVE", ambient)
    monkeypatch.setattr(
        endpoints,
        "env_override",
        lambda _model: {
            "ANTHROPIC_API_KEY": "provider-secret",
            "ANTHROPIC_BASE_URL": "https://provider.invalid",
            "PROVIDER_SENTINEL": "preserved",
        },
    )
    source = sess.create_session("runtime source", model="deepseek-v4-pro")
    fork_boundary = "2026-08-14T09:21:38.123Z"
    successor = sess.register_session(
        "11111111-2222-4333-8444-555555555555",
        name="runtime successor",
        model="deepseek-v4-pro",
        runtime_predecessor=source["id"],
        runtime_fork_boundary_at=fork_boundary,
    )
    assert sess.link_runtime_successor(source["id"], successor["id"])
    captured = _capture_build_options(chat_mod, monkeypatch)
    # Exercise the resume path: this marker exists specifically to tell the
    # resumed CLI that the predecessor process still owns inherited tasks.
    monkeypatch.setattr(
        chat_mod, "_find_session_jsonl", lambda _sid: "/fake/session.jsonl")

    asyncio.run(chat_mod._build_and_connect_client(
        successor["id"], "deepseek-v4-pro", "bypassPermissions", ""))

    assert captured["resume"] == successor["id"]
    provider_env = captured["env"]
    assert provider_env["ANTHROPIC_API_KEY"] == "provider-secret"
    assert provider_env["ANTHROPIC_BASE_URL"] == "https://provider.invalid"
    assert provider_env["PROVIDER_SENTINEL"] == "preserved"
    marker = provider_env["CLAUDE_CODE_RESUME_SOURCE_ALIVE"]
    assert marker
    assert marker != ambient
    parsed = datetime.fromisoformat(marker.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed == datetime.fromisoformat(
        fork_boundary.replace("Z", "+00:00"))


def test_ordinary_resume_neutralizes_ambient_live_source_marker(
    app_module, monkeypatch,
):
    from backend import chat as chat_mod
    from backend import endpoints, sessions as sess

    monkeypatch.setenv(
        "CLAUDE_CODE_RESUME_SOURCE_ALIVE", "ambient-value-must-not-leak")
    monkeypatch.setattr(
        endpoints,
        "env_override",
        lambda _model: {
            "ANTHROPIC_API_KEY": "provider-secret",
            "ANTHROPIC_BASE_URL": "https://provider.invalid",
            "PROVIDER_SENTINEL": "preserved",
        },
    )
    ordinary = sess.create_session("ordinary", model="deepseek-v4-pro")
    captured = _capture_build_options(chat_mod, monkeypatch)
    monkeypatch.setattr(
        chat_mod, "_find_session_jsonl", lambda _sid: "/fake/session.jsonl")

    asyncio.run(chat_mod._build_and_connect_client(
        ordinary["id"], "deepseek-v4-pro", "bypassPermissions", ""))

    assert captured["resume"] == ordinary["id"]
    assert captured["env"]["PROVIDER_SENTINEL"] == "preserved"
    assert captured["env"]["CLAUDE_CODE_RESUME_SOURCE_ALIVE"] == ""


def test_runtime_task_context_uses_authoritative_state_without_private_output(
    app_module,
):
    from backend import chat as chat_mod
    from backend import sessions as sess

    source = sess.create_session("runtime source")
    child = sess.create_session("runtime successor")
    assert sess.link_runtime_successor(source["id"], child["id"])
    assert sess.set_runtime_task_overlay(
        source["id"],
        "task-safe-id",
        owner_session_id=source["id"],
        state="completed",
        updated_at=123456,
        summary="private task result must stay out",
        output_file="/private/workspace/result.txt",
        description="private command description",
    )

    hook = chat_mod._build_runtime_task_context_hook(child["id"])
    result = asyncio.run(hook({}, None, None))
    context = result["hookSpecificOutput"]["additionalContext"]

    assert "task-safe-id" in context
    assert "state=completed" in context
    assert "updated_at_ms=123456" in context
    assert "private task result" not in context
    assert "/private/workspace" not in context
    assert "private command description" not in context
    assert source["id"] not in context
    assert child["id"] not in context


def test_ducc_model_uses_real_cli_runtime_without_native_auth(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    wrapper = tmp_path / "muselab-ducc"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-cloud-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://synthetic-private-db")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/synthetic-private-agent.sock")
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "synthetic-private-value")
    monkeypatch.setenv("DUCC_AUTH_SOURCE", "managed-login")
    monkeypatch.setenv("HTTPS_PROXY", "https://user:password@proxy.invalid")
    monkeypatch.setattr(
        chat_mod, "locate_ducc_executable", lambda: "/opt/ducc/bin/ducc")
    monkeypatch.setattr(chat_mod, "ducc_cli_wrapper", lambda: str(wrapper))
    monkeypatch.setattr(
        endpoints, "env_override",
        lambda model: (_ for _ in ()).throw(
            AssertionError("DUCC must not use endpoint env overrides")),
    )
    captured = _capture_build_options(chat_mod, monkeypatch)

    client = asyncio.run(chat_mod._build_and_connect_client(
        "sid-ducc-runtime", "ducc:claude-opus-4-8",
        "bypassPermissions", "high"))

    assert captured["connected"] is True
    assert client is not None
    assert captured["cli_path"] == str(wrapper)
    assert captured["model"] == "Opus 4.8"
    ducc_env = captured["env"]
    assert ducc_env["MUSELAB_DUCC_CLI"] == "/opt/ducc/bin/ducc"
    assert ducc_env["HOME"]
    assert ducc_env["DUCC_AUTH_SOURCE"] == "managed-login"
    assert "HTTPS_PROXY" not in ducc_env
    for secret_name in (
        "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL",
        "SSH_AUTH_SOCK", "UNRELATED_PRIVATE_VALUE",
    ):
        assert secret_name not in ducc_env
    assert captured["effort"] == "high"
    assert captured["thinking"] == {
        "type": "enabled", "budget_tokens": 10000,
    }


def test_non_claude_ducc_model_uses_catalog_name_without_claude_controls(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    wrapper = tmp_path / "muselab-ducc"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(
        chat_mod, "locate_ducc_executable", lambda: "/opt/ducc/bin/ducc")
    monkeypatch.setattr(chat_mod, "ducc_cli_wrapper", lambda: str(wrapper))
    monkeypatch.setattr(
        endpoints, "env_override",
        lambda model: (_ for _ in ()).throw(
            AssertionError("DUCC must not use endpoint env overrides")),
    )
    captured = _capture_build_options(chat_mod, monkeypatch)

    client = asyncio.run(chat_mod._build_and_connect_client(
        "sid-ducc-gpt", "ducc:gpt-5-6-sol", "bypassPermissions", "high"))

    assert captured["connected"] is True
    assert client is not None
    assert captured["cli_path"] == str(wrapper)
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["env"]["MUSELAB_DUCC_CLI"] == "/opt/ducc/bin/ducc"
    assert "effort" not in captured
    assert captured["thinking"] == {"type": "disabled"}


def test_ducc_stderr_is_categorized_without_persisting_raw_detail(app_module):
    from backend import chat as chat_mod

    private_line = (
        "authentication failed token=synthetic-secret "
        "prompt=synthetic-private-prompt"
    )
    notice = chat_mod._ducc_stderr_notice(private_line)

    assert notice == "authentication detail suppressed for privacy"
    assert "synthetic-secret" not in notice
    assert "synthetic-private-prompt" not in notice


def test_regular_cli_stderr_is_private_and_deduplicated_per_client(
    app_module, monkeypatch, tmp_path, capsys,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "12345678-private-session", "deepseek-v4-pro",
        "bypassPermissions", ""))
    stderr_sink = captured["stderr"]
    capsys.readouterr()
    private_auth = (
        "authentication failed token=synthetic-secret "
        "prompt=synthetic-private-prompt /private/workspace/file.py"
    )
    stderr_sink(private_auth)
    stderr_sink(private_auth)
    stderr_sink("network timeout contacting https://private.invalid/api")
    stderr_sink("network timeout contacting https://private.invalid/api")
    logged = capsys.readouterr().err

    assert logged.count("category=authentication") == 1
    assert logged.count("category=network") == 1
    assert "sid=12345678" in logged
    assert "synthetic-secret" not in logged
    assert "synthetic-private-prompt" not in logged
    assert "/private/workspace" not in logged
    assert "private.invalid" not in logged


def test_turn_perf_summary_is_emitted_once_without_content(
    app_module, monkeypatch,
):
    from backend import chat as chat_mod

    events = []
    monkeypatch.setattr(
        chat_mod.obs,
        "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    broadcast = chat_mod.TurnBroadcast(
        "12345678-private-session", model="codex:gpt-5.6-sol")
    broadcast.user_text = "synthetic-private-prompt"
    broadcast.perf_client = "warm"
    broadcast.perf_client_ms = 4
    broadcast.perf_preflight_ms = 7
    broadcast.perf_query_started = chat_mod.obs.monotonic()
    broadcast.publish({
        "event": "text",
        "data": '{"text":"synthetic-private-response"}',
    })
    broadcast.perf_result_ms = 11
    broadcast.perf_post_started = chat_mod.obs.monotonic()
    broadcast.perf_status = "completed"
    broadcast.perf_background_count = 2

    broadcast.finish()
    broadcast.finish()

    assert len(events) == 1
    event, fields = events[0]
    assert event == "chat.turn"
    assert fields["sid8"] == "12345678"
    assert len(fields["turn8"]) == 8
    assert fields["source"] == "direct"
    assert fields["client"] == "warm"
    assert fields["status"] == "completed"
    assert fields["background_count"] == 2
    rendered = repr(fields)
    assert "synthetic-private-prompt" not in rendered
    assert "synthetic-private-response" not in rendered
    assert "private-session" not in rendered


def test_context_probe_failure_log_is_deduplicated_and_private(
    app_module, capsys,
):
    from backend import chat as chat_mod

    chat_mod._CONTEXT_PROBE_LOG_STATE.clear()
    capsys.readouterr()
    error = TimeoutError(
        "synthetic-secret https://private.invalid /private/workspace")
    chat_mod._log_context_probe_failure("codex:gpt-5.6-sol", error)
    chat_mod._log_context_probe_failure("codex:gpt-5.6-sol", error)
    failed = capsys.readouterr().err

    assert failed.count("gateway context probe unavailable") == 1
    assert "exc=TimeoutError" in failed
    assert "synthetic-secret" not in failed
    assert "private.invalid" not in failed
    assert "/private/workspace" not in failed

    chat_mod._log_context_probe_recovery("codex:gpt-5.6-sol")
    recovered = capsys.readouterr().err
    assert recovered.count("gateway context probe recovered") == 1
    assert "suppressed=1" in recovered
    chat_mod._CONTEXT_PROBE_LOG_STATE.clear()


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


def test_side_question_runtime_exposes_only_public_web_tools(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints, sessions as sess

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("MUSELAB_DISABLE_SKILLS", raising=False)
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    side = sess.create_session(
        "side question",
        model="deepseek-v4-pro",
        permission="default",
        activity_hidden=True,
        runtime_profile="side_question",
    )
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        side["id"], "deepseek-v4-pro", "default", ""))

    assert captured["tools"] == ["WebSearch", "WebFetch"]
    assert captured["allowed_tools"] == ["WebSearch", "WebFetch"]
    assert captured["setting_sources"] == []
    assert captured["plugins"] == []
    assert captured["mcp_servers"] == {}
    assert captured["skills"] == []
    assert "can_use_tool" not in captured
    assert "UserPromptSubmit" not in captured["hooks"]


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
    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9876"
    assert captured["env"]["ANTHROPIC_API_KEY"] == "local-secret"
    assert captured["env"]["ANTHROPIC_CUSTOM_HEADERS"] == (
        "X-MuseLab-Effort: high"
    )
    assert "system_prompt" not in captured
    assert "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH" not in captured["env"]
    assert "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS" not in captured["env"]
    # Claude CLI must use the same effective window as the meter and native
    # compact preflight instead of its unrelated built-in 200K default.
    assert captured["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "258400"
    assert captured["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "258400"
    pre_tool_hooks = captured["hooks"]["PreToolUse"]
    assert [m.matcher for m in pre_tool_hooks] == [
        "Skill", "AskUserQuestion"
    ]
    skill_guard = pre_tool_hooks[0]

    async def invoke_skill_guard(name):
        return await skill_guard.hooks[0](
            {"tool_input": {"skill": name}}, None, None)

    denied = asyncio.run(invoke_skill_guard("claude-api"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "too large" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert asyncio.run(invoke_skill_guard("deep-research")) == {}
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        assert captured["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "gpt-5.5"

    monkeypatch.setenv("MUSELAB_ALLOW_LARGE_CODEX_CLAUDE_API_SKILL", "1")
    opted_out = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-skill-optout", "codex:gpt-5.5",
        "bypassPermissions", "high"))
    assert [m.matcher for m in opted_out["hooks"]["PreToolUse"]] == [
        "AskUserQuestion"
    ]


def test_codex_auto_and_ultra_fast_use_gateway_headers(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("CODEX_GATEWAY_API_KEY", "local-secret")
    monkeypatch.setenv("CODEX_GATEWAY_BASE_URL", "http://127.0.0.1:9876")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")

    async def catalog_capability(_model):
        return {
            "context_limit": 353_400,
            "context_raw_limit": 372_000,
            "context_max_limit": 372_000,
            "context_effective_percent": 95,
            "catalog_auto_compact_threshold": 0,
            "context_limit_source": "gateway_catalog",
            "context_limit_is_estimate": False,
        }

    monkeypatch.setattr(
        chat_mod, "_detect_gateway_context_capability", catalog_capability)

    auto = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-auto", "codex:gpt-5.6-sol", "bypassPermissions", ""))
    assert "effort" not in auto
    assert auto["thinking"] == {"type": "disabled"}
    assert auto["env"]["ANTHROPIC_CUSTOM_HEADERS"] == (
        "X-MuseLab-Effort: auto"
    )
    assert "system_prompt" not in auto

    ultra = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-ultra", "codex:gpt-5.6-sol", "bypassPermissions",
        "ultra", "fast"))
    # Ultra is a client-level mode: maximum wire reasoning plus proactive,
    # bounded delegation through the SDK's existing Agent tool.
    assert ultra["effort"] == "max"
    assert ultra["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert ultra["env"]["ANTHROPIC_CUSTOM_HEADERS"] == (
        "X-MuseLab-Effort: ultra\nX-MuseLab-Service-Tier: fast"
    )
    assert "system_prompt" not in ultra
    ultra_matchers = ultra["hooks"]["UserPromptSubmit"]
    assert len(ultra_matchers) == 1
    assert len(ultra_matchers[0].hooks) == 1
    assert ultra["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert ultra["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "4"

    plan_ultra = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-plan-ultra", "codex:gpt-5.6-sol", "plan",
        "ultra", "fast", plan_return_permission="bypassPermissions"))
    assert plan_ultra["extra_args"]["allow-dangerously-skip-permissions"] is None
    assert "system_prompt" not in plan_ultra
    assert len(plan_ultra["hooks"]["UserPromptSubmit"][0].hooks) == 1

    monkeypatch.setenv("MUSELAB_DISABLE_SKILLS", "1")
    no_skill = _capture_build_options(chat_mod, monkeypatch)
    asyncio.run(chat_mod._build_and_connect_client(
        "sid-codex-ultra-no-skills", "codex:gpt-5.6-sol",
        "bypassPermissions", "ultra", "fast"))
    assert no_skill["effort"] == "max"
    assert no_skill["skills"] == []
    assert "system_prompt" not in no_skill
    assert len(no_skill["hooks"]["UserPromptSubmit"][0].hooks) == 1


def test_bare_gpt_provider_never_inherits_codex_gateway_headers(
    app_module, monkeypatch, tmp_path,
):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("ZHIPUAI_API_KEY", "direct-secret")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-direct-gpt", "gpt-5.6-sol", "bypassPermissions", "high"))

    assert chat_mod._canonical_context_model("gpt-5.6-sol") == "gpt-5.6-sol"
    assert chat_mod._is_codex_gateway_model("gpt-5.6-sol") is False
    assert chat_mod._is_codex_gateway_model("codex:gpt-5.6-sol") is True
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["effort"] == "high"
    assert captured["thinking"] == {"type": "disabled"}
    assert "ANTHROPIC_CUSTOM_HEADERS" not in captured["env"]
    assert [m.matcher for m in captured["hooks"]["PreToolUse"]] == [
        "AskUserQuestion"
    ]


def test_disable_skills_env_still_opts_out(app_module, monkeypatch, tmp_path):
    from backend import chat as chat_mod
    from backend import endpoints

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("MUSELAB_DISABLE_SKILLS", "1")
    monkeypatch.setattr(endpoints, "_VENDOR_CONFIG_DIR", tmp_path / "vendor-cfg")
    captured = _capture_build_options(chat_mod, monkeypatch)

    asyncio.run(chat_mod._build_and_connect_client(
        "sid-third-party-no-skills", "deepseek-v4-pro", "bypassPermissions", ""))

    assert captured["skills"] == []


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
