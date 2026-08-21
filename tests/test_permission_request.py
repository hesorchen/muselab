"""Tests for permission_request — the can_use_tool side-channel bridge."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import PermissionUpdate

from backend import permission_request as perm


@pytest.fixture(autouse=True)
def clean_registry():
    perm._pending.clear()
    perm._pending_plan_modes.clear()
    perm._pending_plan_return_modes.clear()
    perm._plan_transitions.clear()
    perm._session_queues.clear()
    perm._always_allow.clear()
    yield
    perm._pending.clear()
    perm._pending_plan_modes.clear()
    perm._pending_plan_return_modes.clear()
    perm._plan_transitions.clear()
    perm._session_queues.clear()
    perm._always_allow.clear()


def test_register_and_unregister():
    perm.register_session_queue("s1")
    assert "s1" in perm._session_queues
    assert "s1" in perm._always_allow
    perm._plan_transitions[("s1", "tool-1")] = PermissionUpdate(
        type="setMode", mode="default", destination="session")
    perm.unregister_session_queue("s1")
    assert "s1" not in perm._session_queues
    assert "s1" in perm._always_allow
    assert ("s1", "tool-1") not in perm._plan_transitions
    perm.clear_session_permissions("s1")
    assert "s1" not in perm._always_allow


def test_submit_decision_unknown_returns_false():
    assert perm.submit_decision("nope", "qid", "allow") is False


def test_submit_decision_bad_decision_returns_false():
    assert perm.submit_decision("s1", "qid", "maybe") is False


def test_submit_decision_resumes_before_waking_model(monkeypatch):
    order = []

    class FakeFuture:
        def done(self):
            return False

        def set_result(self, value):
            order.append(("resolve", value))

    from backend import activity as activity_module
    monkeypatch.setattr(activity_module.activity, "resume",
                        lambda sid: order.append(("resume", sid)))
    perm._pending[("s1", "qid")] = FakeFuture()

    assert perm.submit_decision("s1", "qid", "allow") is True
    assert order == [
        ("resume", "s1"),
        ("resolve", {"decision": "allow", "message": None}),
    ]


def test_submit_decision_broadcasts_resolution_before_waking_model(monkeypatch):
    order = []

    class OrderedQueue(asyncio.Queue):
        def put_nowait(self, item):
            order.append(("event", item))
            return super().put_nowait(item)

    class FakeFuture:
        def done(self):
            return False

        def set_result(self, value):
            order.append(("resolve", value))

    monkeypatch.setattr(
        "backend.activity.activity.resume",
        lambda _sid: None,
    )
    perm._session_queues["s1"] = OrderedQueue()
    perm._pending[("s1", "qid")] = FakeFuture()

    assert perm.submit_decision("s1", "qid", "deny") is True
    assert order[0][0] == "event"
    assert order[0][1]["event"] == "permission_request_resolved"
    assert json.loads(order[0][1]["data"]) == {
        "id": "qid",
        "kind": "tool",
        "decision": "deny",
        "mode": None,
    }
    assert order[1] == (
        "resolve",
        {"decision": "deny", "message": None},
    )


def test_input_key_bash_safe_binary_uses_first_word():
    # Safe binaries broaden the always-allow cache to the first word so
    # "ls -la X" and "ls Y" share one grant.
    assert perm._input_key("Bash", {"command": "ls -la /tmp"}) == "ls"
    assert perm._input_key("Bash", {"command": ""}) == ""
    # Path-qualified dangerous binary is still recognized (bin name is
    # stripped for the membership check) and keyed by full command.
    assert perm._input_key("Bash", {"command": "/usr/bin/git push"}) == "/usr/bin/git push"


def test_input_key_bash_dangerous_binary_uses_full_command():
    # Dangerous binaries (rm/git/curl/...) key by the FULL command so an
    # always-allow grant for a benign subcommand can't escalate to a
    # destructive one (2026-05-29 privilege-escalation hardening).
    assert perm._input_key("Bash", {"command": "  rm -rf x"}) == "rm -rf x"
    assert perm._input_key("Bash", {"command": "git status"}) == "git status"
    assert perm._input_key("Bash", {"command": "git push --force"}) == "git push --force"


def test_input_key_bash_shell_metachars_use_full_command():
    # Even a "safe" first word keys by full command when the line can chain
    # a second command past it (;, &&, |, $(), redirects, backticks).
    assert perm._input_key("Bash", {"command": "ls; rm -rf /"}) == "ls; rm -rf /"
    assert perm._input_key("Bash", {"command": "echo $(rm x)"}) == "echo $(rm x)"


def test_input_key_file_tools_use_path():
    assert perm._input_key("Read", {"file_path": "/etc/hosts"}) == "/etc/hosts"
    assert perm._input_key("Edit", {"file_path": "x.py"}) == "x.py"


@pytest.mark.asyncio
async def test_full_roundtrip_allow():
    sid = "sess-A"
    perm.register_session_queue(sid)
    cb = perm.build_callback_for_session(sid)

    async def driver():
        # Wait for the request event to appear in the queue
        evt = await asyncio.wait_for(perm._session_queues[sid].get(), timeout=2)
        assert evt["event"] == "permission_request"
        data = json.loads(evt["data"])
        assert data["kind"] == "tool"
        rid = data["id"]
        # User clicks Allow
        assert perm.submit_decision(sid, rid, "allow") is True

    driver_task = asyncio.create_task(driver())
    result = await cb("Bash", {"command": "ls"}, None)
    await driver_task
    # SDK contract: callback returns PermissionResultAllow / PermissionResultDeny
    # objects, not dicts. Accessing as attribute, not subscript.
    assert result.behavior == "allow"


@pytest.mark.asyncio
async def test_full_roundtrip_deny_with_message():
    sid = "sess-B"
    perm.register_session_queue(sid)
    cb = perm.build_callback_for_session(sid)

    async def driver():
        evt = await asyncio.wait_for(perm._session_queues[sid].get(), timeout=2)
        rid = json.loads(evt["data"])["id"]
        assert perm.submit_decision(sid, rid, "deny", "no thanks") is True

    driver_task = asyncio.create_task(driver())
    result = await cb("Bash", {"command": "rm -rf /"}, None)
    await driver_task
    assert result.behavior == "deny"
    assert result.message == "no thanks"


@pytest.mark.asyncio
async def test_bypass_pretool_hook_routes_native_question_to_ui():
    # permission_request may outlive backend module reloads in the chat-stream
    # suite; exercise the exact question registry captured by this hook.
    auq = perm.auq

    sid = "sess-bypass-question"
    queue = auq.register_session_queue(sid)
    hook = perm.build_ask_user_question_hook_for_session(sid)
    questions = [{
        "question": "Pick one",
        "header": "test",
        "multiSelect": False,
        "options": ["A", "B"],
    }]

    async def driver():
        event = await asyncio.wait_for(queue.get(), timeout=2)
        assert event["event"] == "ask_user_question"
        payload = json.loads(event["data"])
        assert payload["questions"][0]["options"] == [
            {"label": "A", "description": ""},
            {"label": "B", "description": ""},
        ]
        assert auq.submit_answer(
            sid, payload["id"], {"Pick one": "B"}) is True

    driver_task = asyncio.create_task(driver())
    result = await hook({
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
    }, "tool-1", None)
    await driver_task
    auq.unregister_session_queue(sid)

    specific = result["hookSpecificOutput"]
    assert specific["permissionDecision"] == "allow"
    assert specific["updatedInput"]["answers"] == {"Pick one": "B"}


@pytest.mark.asyncio
async def test_always_allow_caches_subsequent_calls():
    sid = "sess-C"
    perm.register_session_queue(sid)
    cb = perm.build_callback_for_session(sid)

    async def driver():
        evt = await asyncio.wait_for(perm._session_queues[sid].get(), timeout=2)
        rid = json.loads(evt["data"])["id"]
        assert perm.submit_decision(sid, rid, "always") is True

    driver_task = asyncio.create_task(driver())
    r1 = await cb("Bash", {"command": "ls -la"}, None)
    await driver_task
    assert r1.behavior == "allow"
    resolved = perm._session_queues[sid].get_nowait()
    assert resolved["event"] == "permission_request_resolved"

    # Second call to same tool+key — should NOT prompt (queue stays empty)
    r2 = await cb("Bash", {"command": "ls /tmp"}, None)
    assert r2.behavior == "allow"
    assert perm._session_queues[sid].empty()

    # A turn boundary unregisters the prompt queue but the session-level
    # grant survives and applies after the next turn registers its queue.
    perm.unregister_session_queue(sid)
    perm.register_session_queue(sid)
    r3 = await cb("Bash", {"command": "ls /var/tmp"}, None)
    assert r3.behavior == "allow"
    assert perm._session_queues[sid].empty()


@pytest.mark.asyncio
async def test_no_active_session_denies():
    cb = perm.build_callback_for_session("never-registered")
    result = await cb("Bash", {"command": "ls"}, None)
    assert result.behavior == "deny"


@pytest.mark.asyncio
async def test_unregister_cancels_pending():
    sid = "sess-D"
    perm.register_session_queue(sid)
    cb = perm.build_callback_for_session(sid)
    # Don't drive — just cancel mid-await
    cb_task = asyncio.create_task(cb("Bash", {"command": "ls"}, None))
    await asyncio.sleep(0.05)
    perm.unregister_session_queue(sid)
    result = await cb_task
    assert result.behavior == "deny"
    assert not perm._pending
    assert not perm._pending_plan_modes


@pytest.mark.asyncio
async def test_emit_session_event_json_encodes_data():
    assert await perm.emit_session_event(
        "missing", "permission_mode_changed", {"permission": "default"}
    ) is False

    q = perm.register_session_queue("s1")
    delivered = asyncio.create_task(q.get())
    await asyncio.sleep(0)
    assert await perm.emit_session_event(
        "s1",
        "permission_mode_changed",
        {"permission": "acceptEdits", "label": "已批准"},
    ) is True
    assert delivered.done()
    event = delivered.result()
    assert event["event"] == "permission_mode_changed"
    assert json.loads(event["data"]) == {
        "permission": "acceptEdits",
        "label": "已批准",
    }


@pytest.mark.asyncio
async def test_exit_plan_filters_suggestions_and_stages_selected_mode(
        monkeypatch):
    from backend import sessions

    sid = "plan-filter"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            "plan_return_permission": "acceptEdits",
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    context = SimpleNamespace(
        tool_use_id="tool-plan-1",
        title="Approve this plan?",
        display_name="Exit plan mode",
        description="Review before implementation",
        suggestions=[
            PermissionUpdate(
                type="setMode", mode="default", destination="session"),
            PermissionUpdate(
                type="setMode", mode="acceptEdits", destination="session"),
            # Duplicate modes are collapsed.
            PermissionUpdate(
                type="setMode", mode="acceptEdits", destination="session"),
            # Every entry below must be discarded.
            PermissionUpdate(
                type="setMode", mode="plan", destination="session"),
            PermissionUpdate(
                type="setMode", mode="bypassPermissions",
                destination="localSettings"),
            PermissionUpdate(
                type="addRules", mode="default", destination="session"),
            PermissionUpdate(
                type="setMode", mode="not-a-mode", destination="session"),
            {"type": "setMode", "mode": "dontAsk", "destination": "session"},
        ],
    )
    plan_input = {"plan": "1. inspect\n2. implement"}

    async def driver():
        event = await asyncio.wait_for(
            perm._session_queues[sid].get(), timeout=2)
        data = json.loads(event["data"])
        assert data == {
            "id": data["id"],
            "kind": "exit_plan",
            "tool": "ExitPlanMode",
            "tool_use_id": "tool-plan-1",
            "suggestions": [
                {
                    "type": "setMode",
                    "destination": "session",
                    "mode": "default",
                },
                {
                    "type": "setMode",
                    "destination": "session",
                    "mode": "acceptEdits",
                },
            ],
            "return_mode": "acceptEdits",
            "title": "Approve this plan?",
            "display_name": "Exit plan mode",
            "description": "Review before implementation",
            "input": plan_input,
        }
        request_id = data["id"]
        assert perm.submit_decision(
            sid, request_id, "always", mode="acceptEdits") is False
        assert perm.submit_decision(
            sid, request_id, "allow", mode="bypassPermissions") is False
        assert perm.submit_decision(
            sid, request_id, "allow", mode="acceptEdits") is True

    driver_task = asyncio.create_task(driver())
    result = await callback("ExitPlanMode", plan_input, context)
    await driver_task

    assert result.behavior == "allow"
    assert result.updated_input == plan_input
    assert result.updated_permissions is not None
    assert [update.to_dict() for update in result.updated_permissions] == [{
        "type": "setMode",
        "destination": "session",
        "mode": "acceptEdits",
    }]
    transition = perm.consume_plan_transition(sid, "tool-plan-1")
    assert transition is not None
    assert transition.mode == "acceptEdits"
    assert perm.consume_plan_transition(sid, "tool-plan-1") is None


@pytest.mark.asyncio
async def test_exit_plan_empty_suggestions_uses_session_return_mode(monkeypatch):
    from backend import sessions

    sid = "plan-fallback"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            "plan_return_permission": "dontAsk",
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    context = SimpleNamespace(
        tool_use_id="tool-plan-2",
        suggestions=[],
        title=None,
        display_name=None,
        description=None,
    )

    async def driver():
        event = await perm._session_queues[sid].get()
        data = json.loads(event["data"])
        assert data["return_mode"] == "dontAsk"
        assert data["suggestions"] == [{
            "type": "setMode",
            "destination": "session",
            "mode": "dontAsk",
        }]
        assert perm.submit_decision(
            sid, data["id"], "allow", mode="dontAsk") is True

    driver_task = asyncio.create_task(driver())
    result = await callback(
        "ExitPlanMode", {"plan": "approved plan"}, context)
    await driver_task

    assert result.updated_permissions is not None
    assert result.updated_permissions[0].mode == "dontAsk"
    discarded = perm.discard_plan_transition(sid, "tool-plan-2")
    assert discarded is not None
    assert discarded.mode == "dontAsk"
    assert perm.discard_plan_transition(sid, "tool-plan-2") is None


@pytest.mark.asyncio
async def test_exit_plan_cached_frontend_allow_uses_offered_return_mode(
        monkeypatch):
    from backend import sessions

    sid = "plan-legacy-ui"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            "plan_return_permission": "acceptEdits",
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    context = SimpleNamespace(
        tool_use_id="tool-plan-legacy-ui",
        suggestions=[
            PermissionUpdate(
                type="setMode", mode="default", destination="session"),
            PermissionUpdate(
                type="setMode", mode="acceptEdits", destination="session"),
        ],
        title=None,
        display_name=None,
        description=None,
    )

    async def driver():
        data = json.loads((await perm._session_queues[sid].get())["data"])
        # Old cached clients sent only generic Allow. Compatibility is safe
        # because acceptEdits is also present in this exact SDK suggestion set.
        assert perm.submit_decision(sid, data["id"], "allow") is True

    driver_task = asyncio.create_task(driver())
    result = await callback("ExitPlanMode", {"plan": "draft"}, context)
    await driver_task
    assert result.behavior == "allow"
    assert result.updated_permissions[0].mode == "acceptEdits"
    perm.discard_plan_transition(sid, "tool-plan-legacy-ui")


@pytest.mark.asyncio
async def test_exit_plan_callback_uses_runtime_queue_snapshot(monkeypatch):
    from backend import sessions

    sid = "plan-queue-snapshot"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            # A newer browser selection must not rewrite the queued turn's
            # launch/exit contract.
            "plan_return_permission": "default",
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(
        sid, plan_return_permission="bypassPermissions")
    context = SimpleNamespace(
        tool_use_id="tool-plan-queue",
        suggestions=[],
        title=None,
        display_name=None,
        description=None,
    )

    async def driver():
        data = json.loads((await perm._session_queues[sid].get())["data"])
        assert data["return_mode"] == "bypassPermissions"
        assert data["suggestions"][0]["mode"] == "bypassPermissions"
        assert perm.submit_decision(
            sid, data["id"], "allow", mode="bypassPermissions") is True

    driver_task = asyncio.create_task(driver())
    result = await callback("ExitPlanMode", {"plan": "queued"}, context)
    await driver_task
    assert result.updated_permissions[0].mode == "bypassPermissions"
    perm.discard_plan_transition(sid, "tool-plan-queue")


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "", "plan", "bogus"])
async def test_exit_plan_legacy_or_invalid_return_mode_defaults(
        monkeypatch, stored):
    from backend import sessions

    sid = f"plan-legacy-{stored}"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            **({"plan_return_permission": stored} if stored is not None else {}),
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    context = SimpleNamespace(
        tool_use_id="tool-legacy",
        suggestions=[],
        title=None,
        display_name=None,
        description=None,
    )

    async def driver():
        data = json.loads((await perm._session_queues[sid].get())["data"])
        assert data["return_mode"] == "default"
        assert data["suggestions"][0]["mode"] == "default"
        assert perm.submit_decision(
            sid, data["id"], "deny", message="keep planning") is True

    driver_task = asyncio.create_task(driver())
    result = await callback("ExitPlanMode", {"plan": "draft"}, context)
    await driver_task
    assert result.behavior == "deny"
    assert result.message == "keep planning"
    assert not perm._plan_transitions


@pytest.mark.asyncio
async def test_exit_plan_rejects_missing_tool_use_id():
    sid = "plan-no-id"
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    result = await callback(
        "ExitPlanMode",
        {"plan": "draft"},
        SimpleNamespace(
            tool_use_id=None,
            suggestions=[],
            title=None,
            display_name=None,
            description=None,
        ),
    )
    assert result.behavior == "deny"
    assert "tool_use_id" in result.message
    assert perm._session_queues[sid].empty()
    assert not perm._pending


@pytest.mark.asyncio
async def test_exit_plan_unsafe_nonempty_suggestions_fail_closed(monkeypatch):
    from backend import sessions

    sid = "plan-no-safe-suggestion"
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda requested: {
            "id": requested,
            "plan_return_permission": "acceptEdits",
        },
    )
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    result = await callback(
        "ExitPlanMode",
        {"plan": "draft"},
        SimpleNamespace(
            tool_use_id="tool-no-safe",
            suggestions=[
                PermissionUpdate(
                    type="setMode",
                    mode="bypassPermissions",
                    destination="localSettings",
                ),
            ],
            title=None,
            display_name=None,
            description=None,
        ),
    )
    assert result.behavior == "deny"
    assert "no safe session mode" in result.message
    assert perm._session_queues[sid].empty()


@pytest.mark.asyncio
async def test_exit_plan_timeout_cleans_pending_state(monkeypatch):
    sid = "plan-timeout"
    monkeypatch.setattr(perm, "DECISION_TIMEOUT_S", 0.01)
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    result = await callback(
        "ExitPlanMode",
        {"plan": "draft"},
        SimpleNamespace(
            tool_use_id="tool-timeout",
            suggestions=[],
            title=None,
            display_name=None,
            description=None,
        ),
    )
    assert result.behavior == "deny"
    request_event = perm._session_queues[sid].get_nowait()
    resolved_event = perm._session_queues[sid].get_nowait()
    request_data = json.loads(request_event["data"])
    assert request_event["event"] == "permission_request"
    assert resolved_event["event"] == "permission_request_resolved"
    assert json.loads(resolved_event["data"]) == {
        "id": request_data["id"],
        "kind": "exit_plan",
        "decision": "expired",
        "mode": None,
        "reason": "timeout",
    }
    assert not perm._pending
    assert not perm._pending_plan_modes
    assert not perm._pending_plan_return_modes
    assert not perm._plan_transitions


@pytest.mark.asyncio
async def test_unregister_cancels_pending_exit_plan_and_cleans_state():
    sid = "plan-cancel"
    perm.register_session_queue(sid)
    callback = perm.build_callback_for_session(sid)
    task = asyncio.create_task(callback(
        "ExitPlanMode",
        {"plan": "draft"},
        SimpleNamespace(
            tool_use_id="tool-cancel",
            suggestions=[],
            title=None,
            display_name=None,
            description=None,
        ),
    ))
    await asyncio.wait_for(perm._session_queues[sid].get(), timeout=2)
    perm.unregister_session_queue(sid)
    result = await task
    assert result.behavior == "deny"
    assert not perm._pending
    assert not perm._pending_plan_modes
    assert not perm._pending_plan_return_modes
    assert not perm._plan_transitions
