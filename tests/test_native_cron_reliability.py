"""Native Cron survives idle SDK traffic and reports actual execution evidence."""
import asyncio
import json

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, StreamEvent, SystemMessage,
    TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
)

from tests import test_chat_stream

stream_env = test_chat_stream.stream_env


async def create_job(chat, key, *, recurring=True, durable=False, result=None):
    await chat._observe_sdk_stream_message(key, AssistantMessage(
        content=[ToolUseBlock(id="create-1", name="CronCreate", input={
            "cron": "* * * * *", "prompt": "Check the fixture status",
            **({"recurring": recurring} if recurring is not None else {}),
            "durable": durable,
        })], model=key[1],
    ))
    await chat._observe_sdk_stream_message(key, UserMessage(content=[ToolResultBlock(
        tool_use_id="create-1", content=result or (
            "Scheduled recurring job fixture01 (Every minute). "
            "Session-only (not written to disk, dies when Claude exits). "
            "Auto-expires after 7 days. Use CronDelete to cancel sooner."
        ),
    )]))


@pytest.mark.parametrize("recurring,result", [
    (False, "Scheduled one-shot task fixture01 (Tomorrow). Session-only."),
    (None, "Scheduled recurring job fixture01 (Every minute). Session-only."),
])
def test_actual_cli_create_response_defines_kind_and_durability(stream_env, recurring, result):
    chat = stream_env
    key = ("cron-response", "model", "auto", "")
    asyncio.run(create_job(chat, key, recurring=recurring, durable=True, result=result))
    job = chat._sdk_cron_jobs[key[0]]["fixture01"]
    assert job["recurring"] is (recurring is None)
    assert job["durable"] is False  # requested true does not mean persisted


def test_idle_hook_messages_are_consumed_before_the_orphan_queue(stream_env, monkeypatch):
    chat = stream_env
    key = ("cron-idle-hooks", "model", "auto", "")

    class Worker:
        dropped = 0
        def close(self):
            pass
        def submit(self, *_args):
            return True

    monkeypatch.setattr(chat, "_hook_diagnostic_worker", Worker())

    async def run():
        await create_job(chat, key)
        release = asyncio.Event()
        received = asyncio.Event()
        class Client:
            async def receive_messages(self):
                for index in range(1500):
                    yield SystemMessage(subtype="hook_progress", data={
                        "hook_id": "fixture-hook", "hook_event_name": "Stop",
                        "sequence": index,
                    })
                received.set()
                await release.wait()
        client = Client()
        stream = chat._SessionStream(key, client)
        try:
            await asyncio.wait_for(received.wait(), 2)
            assert stream._failure is None
            assert len(stream._orphans) == 0
            assert chat._session_has_scheduled_tasks(key[0])
        finally:
            await stream.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("throws", [False, True])
def test_hook_observation_does_not_depend_on_a_successful_diagnostic_write(stream_env, monkeypatch, throws):
    chat = stream_env
    class Worker:
        dropped = 1
        def close(self):
            pass
        def submit(self, *_args):
            if throws:
                raise OSError("fixture diagnostic failure")
            return False
    monkeypatch.setattr(chat, "_hook_diagnostic_worker", Worker())
    consumed = asyncio.run(chat._observe_sdk_stream_message(
        ("cron-hook-backpressure", "model", "auto", ""),
        SystemMessage(subtype="hook_response", data={"hook_id": "hook-1"}),
    ))
    assert consumed is True


def test_unannounced_native_cron_stream_is_drained_without_stealing_a_user_turn(stream_env, monkeypatch):
    chat = stream_env
    key = ("cron-missing-trigger", "model", "auto", "")
    async def ignore(*_a, **_kw):
        pass
    monkeypatch.setattr(chat, "_start_activity_early", ignore)
    monkeypatch.setattr(chat, "_finish_activity", ignore)
    monkeypatch.setattr(chat, "_refresh_scheduled_session_summary", ignore)

    async def run():
        await create_job(chat, key)
        message = StreamEvent(uuid="event-1", session_id=key[0], event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Fixture is healthy"},
        })
        assert await chat._observe_sdk_stream_message(key, message) is True
        delivery = chat._sdk_scheduled_deliveries[key]
        await chat._observe_sdk_stream_message(key, AssistantMessage(
            content=[TextBlock("Fixture is healthy")], model="model",
        ))
        await chat._observe_sdk_stream_message(key, ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id=key[0],
        ))
        assert delivery.broadcast.done
        assert not chat._sdk_scheduled_deliveries
        foreground = chat.TurnBroadcast(key[0], model="model")
        chat._active_turns[key[0]] = foreground
        try:
            assert await chat._observe_sdk_stream_message(key, message) is False
        finally:
            chat._active_turns.pop(key[0], None)
            foreground.close()
    asyncio.run(run())


def test_synthetic_no_response_is_not_successful_cron_execution(stream_env, monkeypatch):
    chat = stream_env
    key = ("cron-no-response", "model", "auto", "")
    async def ignore(*_a, **_kw):
        pass
    monkeypatch.setattr(chat, "_start_activity_early", ignore)
    monkeypatch.setattr(chat, "_finish_activity", ignore)
    monkeypatch.setattr(chat, "_refresh_scheduled_session_summary", ignore)

    async def run():
        await create_job(chat, key)
        await chat._observe_sdk_stream_message(key, UserMessage(
            content="Check the fixture status", uuid="trigger-1",
            origin={"kind": "task-notification", "subkind": "scheduled-trigger"},
        ))
        delivery = chat._sdk_scheduled_deliveries[key]
        await chat._observe_sdk_stream_message(key, AssistantMessage(
            content=[TextBlock("No response requested.")], model="<synthetic>",
        ))
        await chat._observe_sdk_stream_message(key, ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=0,
            is_error=False, num_turns=0, session_id=key[0],
            result="No response requested.",
        ))
        done = [json.loads(e["data"]) for e in delivery.broadcast.replay_events()
                if e["event"] == "done"][-1]
        assert done["status"] == "failed"
        assert done["execution_status"] == "not_executed"
        assert done["is_error"] is True
    asyncio.run(run())


def test_native_receipt_survives_restart_and_requires_native_confirmation(stream_env, monkeypatch):
    chat = stream_env
    from backend import native_cron
    meta = chat.sess.create_session(name="Native receipt", model="claude-sonnet-4-6")
    sid = meta["id"]
    key = (sid, meta["model"], "auto", "")
    resumed = []
    async def fake_get_client(*args, **kwargs):
        resumed.append((args, kwargs))
        return object()
    async def settled(*_a, **_kw):
        return True
    monkeypatch.setattr(chat, "get_client", fake_get_client)
    monkeypatch.setattr(chat, "_join_session_disconnects", settled)

    async def run():
        await create_job(chat, key)
        receipt = native_cron.path(sid)
        assert receipt.is_file()
        assert receipt.stat().st_mode & 0o777 == 0o600
        chat._sdk_cron_jobs.clear()
        assert await chat.recover_native_cron_at_startup() == 1
        task = chat._native_cron_recovery_tasks[sid]
        await asyncio.wait_for(asyncio.shield(task), 3)
        job = chat._sdk_cron_jobs[sid]["fixture01"]
        assert job["runtime_state"] == "unconfirmed"
        assert len(resumed) == 1
        # Resuming does not ask the model to create another job or fire now.
        assert resumed[0][0][0] == sid
        hook = chat._native_cron_inventory_hook(sid)
        await hook({"session_crons": [{"id": "fixture01"}]}, None, {})
        assert job["runtime_state"] == "active"
        await hook({}, None, {})
        assert job["runtime_state"] == "active"  # old CLI, not an empty list
        await hook({"session_crons": []}, None, {})
        assert job["runtime_state"] == "missing"
        assert chat._sdk_scheduled_snapshot(sid) == {"scheduled_active": False, "scheduled_count": 1}
        assert len(resumed) == 1
    asyncio.run(run())


def test_disconnect_keeps_tasks_visible_and_deduplicates_recovery(stream_env, monkeypatch):
    chat = stream_env
    meta = chat.sess.create_session(name="Disconnect fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    key = (sid, meta["model"], "auto", "")
    releases = []
    async def fake_get_client(*_a, **_kw):
        releases.append("resume")
        return object()
    monkeypatch.setattr(chat, "get_client", fake_get_client)

    async def run():
        await create_job(chat, key)
        chat._on_sdk_runtime_disconnected(sid)
        first = chat._native_cron_recovery_tasks[sid]
        chat._on_sdk_runtime_disconnected(sid)
        assert chat._native_cron_recovery_tasks[sid] is first
        assert chat._sdk_scheduled_snapshot(sid)["scheduled_count"] == 1
        assert chat._sdk_cron_jobs[sid]["fixture01"]["runtime_state"] == "interrupted"
        await asyncio.wait_for(asyncio.shield(first), 3)
        assert releases == ["resume"]
    asyncio.run(run())


def test_deleted_session_cannot_be_recreated_by_a_late_cron_receipt(stream_env):
    chat = stream_env
    from backend import native_cron
    meta = chat.sess.create_session(name="Deleted fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    async def run():
        await create_job(chat, (sid, meta["model"], "auto", ""))
        native_cron.purge(sid)
        # Deletion's lifecycle fence must reject a previously queued writer.
        original = chat.sess.session_is_deleting
        chat.sess.session_is_deleting = lambda value: value == sid
        try:
            assert await chat._persist_native_cron_state(sid) is False
            assert not native_cron.path(sid).exists()
        finally:
            chat.sess.session_is_deleting = original
    asyncio.run(run())


def test_out_of_order_receipt_cannot_restore_a_deleted_task(stream_env):
    from backend import native_cron
    native_cron.save("receipt-order", {}, 20)
    native_cron.save("receipt-order", {"old-job": {"prompt": "fixture"}}, 10)
    assert "receipt-order" not in native_cron.load_all()


def test_receipt_rejects_symlink_and_never_serializes_unknown_fields(stream_env, tmp_path):
    from backend import native_cron
    native_cron.save("receipt-fields", {"job": {"prompt": "fixture", "raw_protocol": "must-not-persist"}}, 1)
    assert "must-not-persist" not in native_cron.path("receipt-fields").read_text()
    other = tmp_path / "outside.json"
    other.write_text("do not touch")
    native_cron.path("receipt-symlink").symlink_to(other)
    with pytest.raises(Exception):
        native_cron.save("receipt-symlink", {}, 1)
    assert other.read_text() == "do not touch"


def test_buffer_diagnostics_identify_owner_and_envelope_without_content(stream_env, monkeypatch):
    from backend import runtime_buffer
    events = []
    actual_perf_event = runtime_buffer.obs.perf_event
    def record(name, **data):
        actual_perf_event(name, **data)  # enforce the real privacy-field validator
        events.append((name, data))
    monkeypatch.setattr(runtime_buffer.obs, "perf_event", record)
    queue = runtime_buffer.RuntimeMessageQueue(lane="orphan", session_id="fixture-owner", max_events=1)
    queue.put_nowait(SystemMessage(subtype="status", data={"private": "do-not-log"}))
    with pytest.raises(runtime_buffer.RuntimeBufferExceeded):
        queue.put_nowait(SystemMessage(subtype="status", data={"private": "do-not-log"}))
    event = events[-1][1]
    assert event["session"] == "fixture-"
    assert event["envelope_counts"] == {"SystemMessage:status": 1}
    assert event["incoming_kind"] == "SystemMessage:status"
    assert "do-not-log" not in json.dumps(event)
    queue.get_nowait()
    assert not queue.message_kinds


def test_bad_receipt_does_not_prevent_other_sessions_recovering(stream_env):
    from backend import native_cron
    native_cron.save("good-receipt", {"job": {"runtime_state": "active"}}, 1)
    native_cron.path("bad-receipt").write_bytes(b'{broken')
    assert set(native_cron.load_all()) == {"good-receipt"}


def test_finished_history_cannot_crowd_out_new_active_jobs(stream_env):
    from backend import native_cron
    jobs = {f"old-{index}": {"runtime_state": "finished", "created_at_ms": index}
            for index in range(150)}
    jobs["current-job"] = {"runtime_state": "active", "created_at_ms": 200}
    native_cron.save("receipt-retention", jobs, 1)
    loaded = native_cron.load_all()["receipt-retention"]
    assert "current-job" in loaded
    assert len(loaded) == 51


def test_recovery_is_not_started_before_exact_client_cleanup(stream_env, monkeypatch):
    chat = stream_env
    from backend import chat_runtime, runtime_buffer
    meta = chat.sess.create_session(name="Cleanup fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    key = (sid, meta["model"], "auto", "")
    async def run():
        await create_job(chat, key)
        interrupted, release = asyncio.Event(), asyncio.Event()
        resumed = []
        class Client:
            async def interrupt(self):
                interrupted.set()
                await release.wait()
            async def disconnect(self):
                resumed.append("old-disconnected")
        async def fake_get_client(*_a, **_kw):
            resumed.append("new-resumed")
            return object()
        monkeypatch.setattr(chat, "get_client", fake_get_client)
        client = Client()
        chat._clients[key] = client
        from types import SimpleNamespace
        stream = SimpleNamespace(key=key, client=client, _failure=runtime_buffer.RuntimeBufferExceeded())
        cleanup = asyncio.create_task(chat_runtime.evict_failed_session_stream(stream))
        await interrupted.wait()
        assert sid not in chat._native_cron_recovery_tasks
        release.set()
        await cleanup
        await asyncio.wait_for(chat._native_cron_recovery_tasks[sid], 3)
        assert resumed == ["old-disconnected", "new-resumed"]
    asyncio.run(run())


def test_expired_and_explicitly_paused_receipts_do_not_autorun(stream_env, monkeypatch):
    chat = stream_env
    from backend import native_cron
    meta = chat.sess.create_session(name="Inactive fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    native_cron.save(sid, {
        "expired-job": {"runtime_state": "active", "expires_at_ms": 1},
        "paused-job": {"runtime_state": "paused"},
    }, 1)
    async def run():
        assert await chat.recover_native_cron_at_startup() == 2
        assert sid not in chat._native_cron_recovery_tasks
        assert chat._sdk_scheduled_snapshot(sid)["scheduled_active"] is False
    asyncio.run(run())


def test_cron_run_requires_successful_matching_tool_result(stream_env, monkeypatch):
    chat = stream_env
    from backend import native_cron
    meta = chat.sess.create_session(name="Execution fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    key = (sid, meta["model"], "auto", "")
    async def ignore(*_a, **_kw):
        pass
    for name in ("_start_activity_early", "_finish_activity", "_refresh_scheduled_session_summary"):
        monkeypatch.setattr(chat, name, ignore)
    async def run():
        await create_job(chat, key)
        await chat._observe_sdk_stream_message(key, UserMessage(
            content="Check the fixture status", uuid="trigger-1",
            origin={"kind": "task-notification", "subkind": "scheduled-trigger"},
        ))
        await chat._observe_sdk_stream_message(key, AssistantMessage(
            model=key[1], content=[ToolUseBlock(id="check-1", name="Bash", input={"command": "true"})],
        ))
        await chat._observe_sdk_stream_message(key, UserMessage(content=[
            ToolResultBlock(tool_use_id="unknown-tool", content="unrelated"),
            ToolResultBlock(tool_use_id="check-1", content="fixture succeeded", is_error=False),
        ]))
        await chat._observe_sdk_stream_message(key, ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id=sid,
        ))
        job = native_cron.load_all()[sid]["fixture01"]
        assert job["last_status"] == "completed"
        assert job["tool_calls"] == job["tool_results"] == 1
        assert job["last_success_at_ms"] > 0
    asyncio.run(run())


def test_receipt_write_failure_is_visible_and_recovers_on_a_later_write(stream_env, monkeypatch):
    chat = stream_env
    from backend import native_cron
    meta = chat.sess.create_session(name="Write retry fixture", model="claude-sonnet-4-6")
    sid = meta["id"]
    save = native_cron.save
    def fail(*_a, **_kw):
        raise OSError("fixture disk unavailable")
    async def run():
        monkeypatch.setattr(native_cron, "save", fail)
        await create_job(chat, (sid, meta["model"], "auto", ""))
        job = chat._sdk_cron_jobs[sid]["fixture01"]
        assert job["record_saved"] is False
        monkeypatch.setattr(native_cron, "save", save)
        assert await chat._persist_native_cron_state(sid)
        assert job["record_saved"] is True
        assert native_cron.load_all()[sid]["fixture01"]["record_saved"] is True
    asyncio.run(run())


def test_cron_list_cannot_import_another_sessions_creation(stream_env):
    chat = stream_env
    owner = ("cron-owner-a", "model", "auto", "")
    other = ("cron-owner-b", "model", "auto", "")

    async def run():
        await create_job(chat, owner)
        # Also repair a legacy list-only duplicate from an earlier runtime.
        chat._sdk_cron_jobs[other[0]] = {"fixture01": {"runtime_state": "active"}}
        await chat._observe_sdk_stream_message(other, AssistantMessage(
            content=[ToolUseBlock(id="list-b", name="CronList", input={})], model="model"))
        await chat._observe_sdk_stream_message(other, UserMessage(content=[ToolResultBlock(
            tool_use_id="list-b", content="fixture01 — Every minute\nrestored-b — Every hour")]))
        assert set(chat._sdk_cron_jobs[other[0]]) == {"restored-b"}
        assert chat._sdk_cron_jobs[owner[0]]["fixture01"]["owner_session_id"] == owner[0]
        assert not chat._matching_sdk_cron_job(other, "Check the fixture status")

    asyncio.run(run())


def test_native_origin_does_not_require_a_host_creation_receipt(stream_env):
    chat = stream_env
    key = ("native-resumed-session", "model", "auto", "")
    assert chat._is_sdk_scheduled_trigger(key, UserMessage(content="Native resumed prompt", origin={
        "kind": "task-notification", "subkind": "scheduled-trigger"}))
    assert not chat._is_sdk_scheduled_trigger(key, UserMessage(content="Peer message", origin={
        "kind": "task-notification", "subkind": "peer-send-message"}))


def test_originless_cron_fallback_ignores_terminal_or_foreign_receipts(stream_env):
    chat = stream_env
    owner = ("cron-fingerprint-a", "model", "auto", "")
    other = ("cron-fingerprint-b", "model", "auto", "")

    async def run():
        await create_job(chat, owner)
        prompt = "Check the fixture status"
        duplicate = dict(chat._sdk_cron_jobs[owner[0]]["fixture01"])
        chat._sdk_cron_jobs[other[0]] = {"fixture01": duplicate}
        assert not chat._is_sdk_scheduled_trigger(other, UserMessage(content=prompt))
        # Two independently created tasks may have identical prompts.
        await create_job(chat, other, result="Scheduled recurring job other02. Session-only.")
        assert chat._matching_sdk_cron_job(other, prompt) == "other02"
        chat._sdk_cron_jobs[other[0]]["other02"]["runtime_state"] = "finished"
        assert not chat._is_sdk_scheduled_trigger(other, UserMessage(content=prompt))
        assert chat._matching_sdk_cron_job(owner, prompt) == "fixture01"

    asyncio.run(run())


def test_startup_loads_creation_owners_before_recovering_legacy_list_copies(stream_env, monkeypatch):
    chat = stream_env
    from backend import native_cron
    scheduled = []
    # The wrong-session copy is deliberately loaded first.
    monkeypatch.setattr(native_cron, "load_all", lambda: {
        "copy-b": {"job-shared": {"runtime_state": "active"}},
        "owner-a": {"job-shared": {"runtime_state": "active", "created_at_ms": 1}},
    })
    monkeypatch.setattr(chat.sess, "get_session_meta", lambda _sid: {"model": "model"})
    monkeypatch.setattr(chat, "_schedule_native_cron_recovery", lambda sid:
                        scheduled.append(sid) if chat._sdk_cron_jobs.get(sid) else None)
    assert asyncio.run(chat.recover_native_cron_at_startup()) == 1
    assert scheduled == ["owner-a"]
    assert not chat._sdk_cron_jobs["copy-b"]


def test_replayed_creation_cannot_move_an_existing_native_job(stream_env):
    chat = stream_env
    owner = ("create-owner-a", "model", "auto", "")
    other = ("create-replay-b", "model", "auto", "")

    async def run():
        await create_job(chat, owner)
        await create_job(chat, other)
        assert "fixture01" in chat._sdk_cron_jobs[owner[0]]
        assert "fixture01" not in chat._sdk_cron_jobs.get(other[0], {})

    asyncio.run(run())
