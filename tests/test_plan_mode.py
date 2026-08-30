"""Cross-module contracts for MuseLab's SDK-native Plan Mode."""

import asyncio
import json

from claude_agent_sdk.types import PermissionUpdate


def test_exit_plan_hooks_commit_only_after_success(app_module):
    from backend import chat
    from backend import permission_request as perm
    from backend import sessions as sess

    success = sess.create_session(
        name="plan-success",
        permission="plan",
        plan_return_permission="default",
    )
    failure = sess.create_session(
        name="plan-failure",
        permission="plan",
        plan_return_permission="default",
    )
    success_id = success["id"]
    failure_id = failure["id"]
    success_tool_id = "exit-plan-success"
    failure_tool_id = "exit-plan-failure"
    perm._plan_transitions[(success_id, success_tool_id)] = PermissionUpdate(
        type="setMode",
        mode="acceptEdits",
        destination="session",
    )
    perm._plan_transitions[(failure_id, failure_tool_id)] = PermissionUpdate(
        type="setMode",
        mode="dontAsk",
        destination="session",
    )
    success_q = perm.register_session_queue(success_id)

    success_hook, _ = chat._build_plan_exit_hooks(success_id)
    _, failure_hook = chat._build_plan_exit_hooks(failure_id)

    async def exercise():
        success_result = await success_hook(
            {
                "tool_name": "ExitPlanMode",
                "tool_use_id": success_tool_id,
                "permission_mode": "acceptEdits",
            },
            success_tool_id,
            {},
        )
        failure_result = await failure_hook(
            {
                "tool_name": "ExitPlanMode",
                "tool_use_id": failure_tool_id,
                "permission_mode": "plan",
            },
            failure_tool_id,
            {},
        )
        return success_result, failure_result

    success_result, failure_result = asyncio.run(exercise())
    assert success_result == {}
    assert failure_result["continue_"] is False
    assert failure_result["stopReason"]

    success_meta = sess.get_session(success_id)
    assert success_meta["permission"] == "acceptEdits"
    assert success_meta["plan_return_permission"] == ""
    event = success_q.get_nowait()
    assert event["event"] == "permission_mode_changed"
    assert json.loads(event["data"]) == {
        "permission": "acceptEdits",
        "previous_permission": "plan",
        "source": "exit_plan",
        "tool_use_id": success_tool_id,
    }

    failure_meta = sess.get_session(failure_id)
    assert failure_meta["permission"] == "plan"
    assert failure_meta["plan_return_permission"] == "default"
    assert success_id in chat._pending_runtime_rebuilds
    assert failure_id in chat._pending_runtime_rebuilds
    assert (success_id, success_tool_id) not in perm._plan_transitions
    assert (failure_id, failure_tool_id) not in perm._plan_transitions

    perm.unregister_session_queue(success_id)
    chat._pending_runtime_rebuilds.discard(success_id)
    chat._pending_runtime_rebuilds.discard(failure_id)


def test_native_plan_cycle_from_bypass_persists_return_mode(app_module):
    from backend import chat
    from backend import permission_request as perm
    from backend import sessions as sess

    meta = sess.create_session(
        name="native-plan-from-bypass",
        permission="bypassPermissions",
    )
    sid = meta["id"]
    q = perm.register_session_queue(sid)
    enter_id = "enter-plan-from-bypass"
    exit_id = "exit-plan-to-bypass"
    enter_hook, _ = chat._build_plan_enter_hooks(sid, "bypassPermissions")

    enter_result = asyncio.run(enter_hook(
        {
            "tool_name": "EnterPlanMode",
            "tool_use_id": enter_id,
            "permission_mode": "plan",
        },
        enter_id,
        {},
    ))

    assert enter_result == {}
    entered = sess.get_session(sid)
    assert entered["permission"] == "plan"
    assert entered["plan_return_permission"] == "bypassPermissions"
    enter_event = q.get_nowait()
    assert enter_event["event"] == "permission_mode_changed"
    assert json.loads(enter_event["data"]) == {
        "permission": "plan",
        "previous_permission": "bypassPermissions",
        "source": "enter_plan",
        "tool_use_id": enter_id,
    }

    perm._plan_transitions[(sid, exit_id)] = PermissionUpdate(
        type="setMode",
        mode="bypassPermissions",
        destination="session",
    )
    exit_hook, _ = chat._build_plan_exit_hooks(sid, "bypassPermissions")
    exit_result = asyncio.run(exit_hook(
        {
            "tool_name": "ExitPlanMode",
            "tool_use_id": exit_id,
            "permission_mode": "bypassPermissions",
        },
        exit_id,
        {},
    ))

    assert exit_result == {}
    exited = sess.get_session(sid)
    assert exited["permission"] == "bypassPermissions"
    assert exited["plan_return_permission"] == ""
    exit_event = q.get_nowait()
    assert exit_event["event"] == "permission_mode_changed"
    assert json.loads(exit_event["data"])["permission"] == "bypassPermissions"
    assert sid in chat._pending_runtime_rebuilds

    perm.unregister_session_queue(sid)
    chat._pending_runtime_rebuilds.discard(sid)


def test_native_plan_enter_cas_does_not_overwrite_newer_permission(app_module):
    from backend import chat
    from backend import permission_request as perm
    from backend import sessions as sess

    meta = sess.create_session(
        name="native-plan-enter-cas",
        permission="bypassPermissions",
    )
    sid = meta["id"]
    q = perm.register_session_queue(sid)
    sess.update_permission(sid, "acceptEdits")
    enter_hook, _ = chat._build_plan_enter_hooks(sid, "bypassPermissions")

    result = asyncio.run(enter_hook(
        {"tool_name": "EnterPlanMode", "tool_use_id": "stale-enter"},
        "stale-enter",
        {},
    ))

    assert result["continue_"] is False
    assert sess.get_session(sid)["permission"] == "acceptEdits"
    event = q.get_nowait()
    assert event["event"] == "permission_mode_change_failed"
    assert sid in chat._pending_runtime_rebuilds
    perm.unregister_session_queue(sid)
    chat._pending_runtime_rebuilds.discard(sid)


def test_exit_plan_persist_failure_still_discards_runtime(
    app_module, monkeypatch,
):
    from backend import chat
    from backend import permission_request as perm

    sid = "plan-persist-failure"
    tool_id = "exit-plan-persist-failure"
    perm._plan_transitions[(sid, tool_id)] = PermissionUpdate(
        type="setMode",
        mode="default",
        destination="session",
    )
    def fail_plan_commit(_sid, _target, **_kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(chat.sess, "commit_plan_exit", fail_plan_commit)
    success_hook, _ = chat._build_plan_exit_hooks(sid)

    hook_result = asyncio.run(success_hook(
        {"tool_name": "ExitPlanMode", "tool_use_id": tool_id},
        tool_id,
        {},
    ))

    assert hook_result["continue_"] is False
    assert hook_result["stopReason"]
    assert sid in chat._pending_runtime_rebuilds
    assert (sid, tool_id) not in perm._plan_transitions
    chat._pending_runtime_rebuilds.discard(sid)


def test_external_hook_exit_plan_commits_reported_runtime_mode(app_module):
    from backend import chat
    from backend import permission_request as perm
    from backend import sessions as sess

    meta = sess.create_session(
        name="external-plan-hook",
        permission="plan",
        plan_return_permission="acceptEdits",
    )
    sid = meta["id"]
    q = perm.register_session_queue(sid)
    success_hook, _ = chat._build_plan_exit_hooks(sid, "acceptEdits")

    hook_result = asyncio.run(success_hook(
        {
            "tool_name": "ExitPlanMode",
            "tool_use_id": "external-exit",
            "permission_mode": "acceptEdits",
        },
        "external-exit",
        {},
    ))

    assert hook_result == {}
    current = sess.get_session(sid)
    assert current["permission"] == "acceptEdits"
    event = q.get_nowait()
    assert event["event"] == "permission_mode_changed"
    assert json.loads(event["data"]) == {
        "permission": "acceptEdits",
        "previous_permission": "plan",
        "source": "external_hook",
        "tool_use_id": "external-exit",
    }
    assert sid in chat._pending_runtime_rebuilds
    perm.unregister_session_queue(sid)
    chat._pending_runtime_rebuilds.discard(sid)


def test_external_hook_exit_plan_without_target_only_discards_runtime(
    app_module,
):
    from backend import chat
    from backend import permission_request as perm
    from backend import sessions as sess

    meta = sess.create_session(
        name="external-plan-unknown",
        permission="plan",
        plan_return_permission="default",
    )
    sid = meta["id"]
    q = perm.register_session_queue(sid)
    success_hook, failure_hook = chat._build_plan_exit_hooks(sid, "default")

    async def exercise():
        success_result = await success_hook(
            {
                "tool_name": "ExitPlanMode",
                "tool_use_id": "unknown-success",
                "permission_mode": "plan",
            },
            "unknown-success",
            {},
        )
        failure_result = await failure_hook(
            {
                "tool_name": "ExitPlanMode",
                "tool_use_id": "unknown-failure",
                "permission_mode": "acceptEdits",
                "error": "hook failed",
            },
            "unknown-failure",
            {},
        )
        return success_result, failure_result

    success_result, failure_result = asyncio.run(exercise())
    assert success_result["continue_"] is False
    assert failure_result["continue_"] is False

    assert sess.get_session(sid)["permission"] == "plan"
    events = [q.get_nowait(), q.get_nowait()]
    assert [event["event"] for event in events] == [
        "permission_mode_change_failed",
        "permission_mode_change_failed",
    ]
    assert sid in chat._pending_runtime_rebuilds
    perm.unregister_session_queue(sid)
    chat._pending_runtime_rebuilds.discard(sid)


def test_unmatched_plan_transition_is_reported_on_queue_teardown(app_module):
    from backend import permission_request as perm

    sid = "plan-eof"
    perm.register_session_queue(sid)
    perm._plan_transitions[(sid, "exit-plan-eof")] = PermissionUpdate(
        type="setMode",
        mode="default",
        destination="session",
    )

    assert perm.unregister_session_queue(sid) is True
    assert not any(key[0] == sid for key in perm._plan_transitions)
    assert perm.unregister_session_queue(sid) is False


def test_queue_endpoint_preserves_plan_return_capability(
    client, auth, app_module, monkeypatch,
):
    from backend import chat

    # This case deliberately exercises the persisted item through one manual
    # drain below. Enqueue now schedules an immediate background kick for idle
    # sessions, so disable that production wakeup here or it can consume the
    # item before the test installs its fake _start_turn.
    monkeypatch.setattr(chat, "_schedule_queue_drain", lambda _sid: None)

    created = client.post(
        "/api/chat/sessions",
        headers=auth,
        json={"name": "queued-plan", "permission": "bypassPermissions"},
    )
    assert created.status_code == 200, created.text
    sid = created.json()["id"]

    entered = client.patch(
        f"/api/chat/sessions/{sid}",
        headers=auth,
        json={"permission": "plan"},
    )
    assert entered.status_code == 200, entered.text

    queued = client.post(
        f"/api/chat/sessions/{sid}/queue",
        headers=auth,
        json={
            "text": "make a plan",
            "permission": "plan",
        },
    )
    assert queued.status_code == 200, queued.text
    item = queued.json()["item"]
    assert item["permission"] == "plan"
    assert item["plan_return_permission"] == "bypassPermissions"

    started = {}

    async def fake_start_turn(session_id, prompt, **kwargs):
        started.update(session_id=session_id, prompt=prompt, **kwargs)

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    asyncio.run(chat._maybe_drain_queue(sid))

    assert started["session_id"] == sid
    assert started["permission"] == "plan"
    assert started["plan_return_permission"] == "bypassPermissions"
    assert started["persist_permission"] is False
    meta = chat.sess.get_session(sid)
    assert meta["permission"] == "plan"
    assert meta["plan_return_permission"] == "bypassPermissions"
