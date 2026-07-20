"""Tests for permission_request — the can_use_tool side-channel bridge."""
import asyncio
import pytest
from backend import permission_request as perm


@pytest.fixture(autouse=True)
def clean_registry():
    perm._pending.clear()
    perm._session_queues.clear()
    perm._always_allow.clear()
    yield
    perm._pending.clear()
    perm._session_queues.clear()
    perm._always_allow.clear()


def test_register_and_unregister():
    perm.register_session_queue("s1")
    assert "s1" in perm._session_queues
    assert "s1" in perm._always_allow
    perm.unregister_session_queue("s1")
    assert "s1" not in perm._session_queues
    assert "s1" in perm._always_allow
    perm.clear_session_permissions("s1")
    assert "s1" not in perm._always_allow


def test_submit_decision_unknown_returns_false():
    assert perm.submit_decision("nope", "qid", "allow") is False


def test_submit_decision_bad_decision_returns_false():
    assert perm.submit_decision("s1", "qid", "maybe") is False


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
        import json
        rid = json.loads(evt["data"])["id"]
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
        import json
        rid = json.loads(evt["data"])["id"]
        assert perm.submit_decision(sid, rid, "deny", "no thanks") is True

    driver_task = asyncio.create_task(driver())
    result = await cb("Bash", {"command": "rm -rf /"}, None)
    await driver_task
    assert result.behavior == "deny"
    assert result.message == "no thanks"


@pytest.mark.asyncio
async def test_always_allow_caches_subsequent_calls():
    sid = "sess-C"
    perm.register_session_queue(sid)
    cb = perm.build_callback_for_session(sid)

    async def driver():
        evt = await asyncio.wait_for(perm._session_queues[sid].get(), timeout=2)
        import json
        rid = json.loads(evt["data"])["id"]
        assert perm.submit_decision(sid, rid, "always") is True

    driver_task = asyncio.create_task(driver())
    r1 = await cb("Bash", {"command": "ls -la"}, None)
    await driver_task
    assert r1.behavior == "allow"

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
