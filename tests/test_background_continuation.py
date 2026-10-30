"""Continuation boundaries use the real watcher and sole SDK stream router."""
import asyncio
import json
from contextlib import suppress

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, StreamEvent, TaskNotificationMessage,
    TextBlock, ToolUseBlock,
)

from tests.test_chat_stream import stream_env as _stream_env


stream_env = _stream_env


class QueueClient:
    EOF = object()

    def __init__(self):
        self.messages = asyncio.Queue()
        self.queries = []
        self.resume_requested = asyncio.Event()
        self.disconnect_started = asyncio.Event()
        self.allow_disconnect = asyncio.Event()
        self.allow_disconnect.set()
        self.disconnect_calls = 0
        self.fail_first_disconnect = False

    async def receive_messages(self):
        while True:
            msg = await self.messages.get()
            if msg is self.EOF:
                return
            yield msg

    async def query(self, prompt):
        self.queries.append([item async for item in prompt])
        self.resume_requested.set()

    async def disconnect(self):
        self.disconnect_calls += 1
        self.disconnect_started.set()
        if self.fail_first_disconnect and self.disconnect_calls == 1:
            raise RuntimeError("synthetic cleanup retry")
        await self.allow_disconnect.wait()


def notification(sid, *, status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="synthetic-task",
        status=status, output_file=None, summary="synthetic task",
        uuid="synthetic-notification", session_id=sid, tool_use_id="synthetic-tool",
    )


def result(sid, **overrides):
    args = dict(
        subtype="success", duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=0.0, usage={},
    )
    args.update(overrides)
    return ResultMessage(**args)


def assistant(*, content=None):
    return AssistantMessage(
        content=content or [TextBlock(text="synthetic response")],
        model="claude-sonnet-4-6", usage={}, uuid="synthetic-assistant",
    )


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def start_watcher(chat, sid, client, *, generation=None):
    chat._pin_background_task(sid, "synthetic-task")
    key = (sid, "claude-sonnet-4-6", "", "")
    chat._clients[key] = client
    stream = chat._ensure_session_stream(key, client)
    watcher = asyncio.create_task(chat._watch_inflight_tasks(
        sid, client, {"synthetic-task": "synthetic"}, generation))
    chat._task_watchers[sid] = watcher
    return stream, watcher


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["text", "tool", "message_start"])
async def test_started_continuation_survives_long_model_and_tool_silence(
    stream_env, monkeypatch, first,
):
    chat = stream_env
    sid = "continued-after-silence"
    monkeypatch.setattr(chat, "_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_CONTINUATION_TIMEOUT", 1)
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    if first == "message_start":
        signal = StreamEvent(uuid="synthetic-event", session_id=sid,
                             event={"type": "message_start"})
    elif first == "tool":
        signal = assistant(content=[ToolUseBlock(
            id="synthetic-bash", name="Bash", input={"command": "true"})])
    else:
        signal = assistant()
    client.messages.put_nowait(signal)
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await wait_until(lambda: sid in chat._active_turns)
        await asyncio.sleep(0.04)
        assert not watcher.done()
        assert client.queries == []
        client.messages.put_nowait(assistant())
        await asyncio.sleep(0.04)
        assert not watcher.done()
        assert client.queries == []
        client.messages.put_nowait(result(sid))
        await asyncio.wait_for(watcher, 2)
        done = json.loads(chat._recent_turns[sid].events[-1]["data"])
        assert done["status"] == "completed"
        assert done["is_error"] is False
        assert client.disconnect_calls == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_explicit_resume_has_a_full_response_lease(stream_env, monkeypatch):
    chat = stream_env
    sid = "slow-explicit-resume"
    monkeypatch.setattr(chat, "_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_CONTINUATION_TIMEOUT", 1)
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await asyncio.wait_for(client.resume_requested.wait(), 2)
        await asyncio.sleep(0.04)
        assert not watcher.done()
        client.messages.put_nowait(assistant())
        client.messages.put_nowait(result(sid))
        await asyncio.wait_for(watcher, 2)
        assert len(client.queries) == 1
        assert client.queries[0][0]["isMeta"] is True
        assert chat._recent_turns[sid].perf_status == "completed"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["deadline", "eof", "cancel", "stopped"])
async def test_missing_result_fences_exact_runtime_before_queue_release(
    stream_env, monkeypatch, exit_kind,
):
    chat = stream_env
    sid = "missing-continuation-boundary"
    monkeypatch.setattr(chat, "_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_STOPPED_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_CONTINUATION_TIMEOUT", 0.04)
    monkeypatch.setattr(chat, "_TASK_TERMINATION_RETRY_S", 0.001)
    drains = []

    async def drain(sid):
        drains.append(sid)

    monkeypatch.setattr(chat, "_maybe_drain_queue", drain)
    client = QueueClient()
    client.fail_first_disconnect = exit_kind == "deadline"
    client.allow_disconnect.clear()
    client.messages.put_nowait(notification(
        sid, status="stopped" if exit_kind == "stopped" else "completed"))
    if exit_kind != "stopped":
        client.messages.put_nowait(assistant())
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await wait_until(lambda: sid in chat._active_turns)
        if exit_kind == "cancel":
            watcher.cancel()
        elif exit_kind == "eof":
            client.messages.put_nowait(client.EOF)
        await asyncio.wait_for(client.disconnect_started.wait(), 2)
        assert not watcher.done()
        assert chat._task_watchers[sid] is watcher
        assert drains == []
        assert sid in chat._active_turns
        # A late SDK boundary cannot enter a new user turn, even while process
        # cleanup is awaiting confirmation. The sole reader is closed first.
        assert stream._closed
        client.messages.put_nowait(result(sid))
        new_queue = stream.attach_turn()
        await asyncio.sleep(0.015)
        assert new_queue.empty()
        client.allow_disconnect.set()
        if exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(watcher, 2)
        else:
            await asyncio.wait_for(watcher, 2)
        assert drains == [sid]
        done = json.loads(chat._recent_turns[sid].events[-1]["data"])
        if exit_kind not in {"cancel", "stopped"}:
            assert done["kind"] == "background_continuation_incomplete"
            assert done["is_error"] is True
        if exit_kind == "cancel":
            assert done["cancelled"] is True
        if exit_kind == "deadline":
            assert client.disconnect_calls >= 2
        assert client.queries == []
    finally:
        client.allow_disconnect.set()
        if not watcher.done():
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        await stream.aclose()


@pytest.mark.asyncio
async def test_progress_messages_do_not_extend_absolute_continuation_lease(
    stream_env, monkeypatch,
):
    chat = stream_env
    sid = "bounded-continuation"
    monkeypatch.setattr(chat, "_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_CONTINUATION_TIMEOUT", 0.04)
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    client.messages.put_nowait(assistant())
    stream, watcher = start_watcher(chat, sid, client)

    async def progress():
        while not watcher.done():
            client.messages.put_nowait(StreamEvent(
                uuid="synthetic-progress", session_id=sid,
                event={"type": "message_delta"}))
            await asyncio.sleep(0.002)

    producer = asyncio.create_task(progress())
    try:
        await asyncio.wait_for(watcher, 1)
        assert client.disconnect_calls == 1
        assert chat._recent_turns[sid].perf_status == "failed"
        assert client.queries == []
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
        await stream.aclose()


@pytest.mark.asyncio
async def test_replacement_generation_retains_runtime_ownership(stream_env):
    chat = stream_env
    sid = "replacement-keeps-runtime"
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    client.messages.put_nowait(assistant())
    chat._continuation_generations[sid] = 1
    stream, watcher = start_watcher(chat, sid, client, generation=1)
    try:
        await wait_until(lambda: sid in chat._active_turns)
        chat._continuation_generations[sid] = 2
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert client.disconnect_calls == 0
        assert not stream.task.done()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    {"subtype": "error_max_turns", "errors": ["synthetic turn limit"]},
    {"subtype": "error_during_execution", "result": "synthetic 429 rate limit",
     "api_error_status": 429},
    {"subtype": "error_max_turns", "errors": ["synthetic turn limit"],
     "terminal_reason": "max_turns"},
])
async def test_sdk_error_reaches_done_footer_and_runtime_outbox(
    stream_env, monkeypatch, terminal,
):
    chat = stream_env
    sid = "f8fb8250-4c95-4a95-806c-77eac5f4820f"
    chat.sess.register_session(sid, name="synthetic", model="claude-sonnet-4-6")
    outbox = []

    def persist(_sid, _broadcast, **fields):
        outbox.append(fields)
        return ""

    monkeypatch.setattr(chat, "_persist_runtime_continuation_outbox", persist)
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    client.messages.put_nowait(assistant())
    client.messages.put_nowait(result(sid, is_error=True, **terminal))
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await asyncio.wait_for(watcher, 2)
        bc = chat._recent_turns[sid]
        done = json.loads(bc.events[-1]["data"])
        expected_status = "stopped" if terminal.get("terminal_reason") == "max_turns" else "failed"
        assert done["is_error"] is True
        assert done["status"] == expected_status
        assert done["result_subtype"] == terminal["subtype"]
        assert done["api_error_status"] == terminal.get("api_error_status")
        assert bc.perf_status == "failed"
        if terminal.get("api_error_status") == 429:
            assert done["kind"] == "quota"
        assert chat.sess.get_message_annotations(sid)["synthetic-assistant"]["turn_status"] == expected_status
        assert outbox[0]["terminal_status"] == expected_status
        assert outbox[0]["incomplete_error"] == done["error"]
        assert client.disconnect_calls == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_sdk_aborted_result_stays_cancelled_in_background_accounting(
    stream_env, monkeypatch,
):
    chat = stream_env
    sid = "sdk-cancelled-continuation"
    perf = []
    monkeypatch.setattr(chat, "_perf_event", lambda event, **fields: perf.append((event, fields)))
    client = QueueClient()
    client.messages.put_nowait(notification(sid))
    client.messages.put_nowait(assistant())
    client.messages.put_nowait(result(sid, terminal_reason="aborted_streaming"))
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await asyncio.wait_for(watcher, 2)
        done = json.loads(chat._recent_turns[sid].events[-1]["data"])
        assert done["status"] == "cancelled"
        assert done["cancelled"] is True
        background = next(fields for event, fields in perf if event == "chat.background")
        assert background["status"] == "cancelled"
        assert client.disconnect_calls == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_output_before_typed_notification_opens_the_continuation(
    stream_env, monkeypatch,
):
    """The CLI may answer a finished task before its typed notification.

    2026-10-30: those first frames were dropped (no continuation open yet),
    the reply counted as "not started", and after the grace period the watcher
    sent a resume prompt that the CLI queued into the visible conversation as a
    stray "<system-reminder>All background tasks…" user bubble.
    """
    chat = stream_env
    sid = "output-before-notification"
    monkeypatch.setattr(chat, "_CONTINUATION_GRACE", 0.01)
    monkeypatch.setattr(chat, "_CONTINUATION_TIMEOUT", 1)
    client = QueueClient()
    client.messages.put_nowait(assistant())
    client.messages.put_nowait(notification(sid))
    stream, watcher = start_watcher(chat, sid, client)
    try:
        await wait_until(lambda: sid in chat._active_turns)
        await asyncio.sleep(0.05)
        assert client.queries == []
        client.messages.put_nowait(result(sid))
        await asyncio.wait_for(watcher, 2)
        bc = chat._recent_turns[sid]
        texts = [json.loads(e["data"]).get("text", "") for e in bc.events
                 if e.get("event") == "text"]
        assert "synthetic response" in "".join(texts)
        assert json.loads(bc.events[-1]["data"])["status"] == "completed"
        assert client.queries == []
    finally:
        await stream.aclose()
