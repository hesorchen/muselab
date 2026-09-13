"""SDK output always has a session owner, including the Result handoff."""
import asyncio
import json

import pytest
from types import SimpleNamespace

from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent,
    SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage)

from backend import chat_runtime as runtime
from tests import test_chat_stream

stream_env = test_chat_stream.stream_env


def test_large_idle_output_is_complete_and_does_not_acquire_a_cron_identity(stream_env, monkeypatch):
    chat = stream_env
    deliveries = []
    begin = chat._begin_sdk_delivery
    async def capture(*args, **kwargs):
        delivery = await begin(*args, **kwargs)
        deliveries.append(delivery)
        return delivery
    async def noop(*_args, **_kwargs):
        pass
    monkeypatch.setattr(chat, "_begin_sdk_delivery", capture)
    monkeypatch.setattr(chat, "_start_activity_early", noop)
    monkeypatch.setattr(chat, "_finish_activity", noop)
    monkeypatch.setattr(chat, "_refresh_sdk_session_summary", noop)
    monkeypatch.setattr(chat.sess, "set_message_annotation", lambda *_a, **_k: None)

    async def run():
        key = ("idle-session-a", "model", "", "")
        delivered = asyncio.Event()
        class Client:
            async def receive_messages(self):
                for i in range(1600):
                    yield SystemMessage(subtype="thinking_tokens", data={"count": i})
                    yield StreamEvent(uuid=f"event-{i}", session_id=key[0],
                        event={"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "x"}})
                yield AssistantMessage(content=[TextBlock("x" * 1600),
                    ToolUseBlock(id="fixture-tool", name="Read", input={})], model="model")
                yield UserMessage(content=[ToolResultBlock(tool_use_id="fixture-tool", content="fixture")])
                yield AssistantMessage(content=[TextBlock("complete")], model="model", uuid="terminal-fixture")
                yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id=key[0])
                delivered.set()
                await asyncio.Event().wait()
        stream = chat._SessionStream(key, Client())
        try:
            await asyncio.wait_for(delivered.wait(), 5)
            assert stream._failure is None and not stream._closed
            assert len(deliveries) == 1
            delivery = deliveries[0]
            events = list(delivery.broadcast.replay_events())
            text = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "text")
            assert text == "x" * 1600 + "complete"
            done = [json.loads(e["data"]) for e in events if e["event"] == "done"][-1]
            assert done["assistant_uuid"] == "terminal-fixture"
            assert done["status"] == "completed" and done["tool_results"] == 1
            assert not done["scheduled"] and done["continuation"]
            assert delivery.job_id == "" and not chat._sdk_cron_jobs
            assert stream.attach_turn().empty()
            assert not any(k[0] != key[0] for k in chat._sdk_deliveries)
        finally:
            await stream.aclose()
            await asyncio.gather(*list(chat._maintenance_tasks), return_exceptions=True)
    asyncio.run(run())


def test_result_handoff_cannot_be_overtaken_by_new_wire_output(monkeypatch):
    async def run():
        wire = asyncio.Queue()
        entered, release = asyncio.Event(), asyncio.Event()
        delivered = []
        async def consume(_key, message):
            if message == "old-0":
                entered.set()
                await release.wait()
            delivered.append(message)
        async def evict(_stream):
            raise AssertionError("healthy stream must remain connected")
        monkeypatch.setattr(runtime, "_hooks", SimpleNamespace(
            observe_stream_message=lambda *_: False, consume_idle_message=consume,
            evict_failed_session_stream=evict))
        class Client:
            async def receive_messages(self):
                while True:
                    yield await wire.get()
        stream = runtime.SessionStream(("handoff-session", "model", "", ""), Client())
        try:
            turn = stream.attach_turn()
            for i in range(1200):
                turn.put_nowait(f"old-{i}")
            transfer = asyncio.create_task(stream.release_turn(turn))
            await entered.wait()
            await wire.put("new-wire")
            await asyncio.sleep(0)
            assert not delivered
            release.set()
            await transfer
            for _ in range(10):
                if len(delivered) == 1201:
                    break
                await asyncio.sleep(0)
            assert delivered == [f"old-{i}" for i in range(1200)] + ["new-wire"]
            assert stream.attach_turn().empty()
        finally:
            release.set()
            await stream.aclose()
    asyncio.run(run())


def test_cancelled_handoff_still_delivers_the_entire_ordered_tail(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def consume(_key, message):
            if not seen:
                entered.set()
                await release.wait()
            seen.append(message)
        class Client:
            async def receive_messages(self):
                await asyncio.Event().wait()
                yield
        monkeypatch.setattr(runtime, "_hooks", SimpleNamespace(
            observe_stream_message=lambda *_: False, consume_idle_message=consume))
        stream = runtime.SessionStream(("cancel-handoff", "model", "", ""), Client())
        try:
            queue = stream.attach_turn()
            for index in range(600):
                queue.put_nowait(index)
            transfer = asyncio.create_task(stream.release_turn(queue))
            await entered.wait()
            transfer.cancel()
            await asyncio.sleep(0)
            transfer.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await transfer
            assert seen == list(range(600)) and stream._turn is None
        finally:
            release.set()
            await stream.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("native_command", ["/compact", "/clear"])
def test_native_command_cancellation_drains_its_own_result_before_releasing(
    stream_env, monkeypatch, native_command,
):
    chat = stream_env
    idle = []
    async def consume(_key, message):
        idle.append(message)
    monkeypatch.setattr(chat, "_consume_sdk_idle_message", consume)
    async def run():
        key = ("compact-cancel", "model", "", "")
        wire = asyncio.Queue()
        queried = asyncio.Event()
        interrupted = []
        class Client:
            async def receive_messages(self):
                while True:
                    yield await wire.get()
            async def query(self, _command):
                queried.set()
            async def interrupt(self):
                interrupted.append(True)
                await wire.put(AssistantMessage(content=[TextBlock("cancelled summary")], model="model"))
                await wire.put(ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1,
                    is_error=True, num_turns=0, session_id=key[0]))
        client = Client()
        stream = chat._SessionStream(key, client)
        chat.chat_runtime.SESSION_STREAMS[key] = stream
        try:
            operation = (chat._run_sdk_reset_checked(client, key[0]) if native_command == "/clear"
                         else chat._run_sdk_command_checked(client, native_command))
            command = asyncio.create_task(operation)
            await queried.wait()
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
            assert interrupted == [True]
            assert idle == [] and stream._failure is None
            assert stream.attach_turn().empty()
        finally:
            await stream.aclose()
            chat.chat_runtime.SESSION_STREAMS.pop(key, None)
    asyncio.run(run())


def test_unknown_capacity_does_not_start_speculative_compaction(stream_env, client, monkeypatch):
    chat = stream_env
    sid = test_chat_stream._make_session(client)
    fake = test_chat_stream._FakeStreamClient([])
    async def context():
        return {"maxTokens": 200000, "totalTokens": 167466}
    fake.get_context_usage = context
    monkeypatch.setattr(chat, "get_client", lambda *_a, **_k: asyncio.sleep(0, result=fake))
    monkeypatch.setattr(chat, "_is_codex_gateway_model", lambda _m: True)
    monkeypatch.setattr(chat.endpoints, "is_third_party", lambda _m: True)
    monkeypatch.setattr(chat, "MODEL_CONTEXT_LIMITS", {})
    monkeypatch.setattr(chat, "_detect_gateway_context_capability",
                        lambda _m: asyncio.sleep(0, result=None))
    response = client.get(
        f"/api/chat/stream?token={test_chat_stream.TEST_TOKEN}&session_id={sid}"
        "&prompt=fixture&model=codex:gpt-5.6-sol")
    events = test_chat_stream._parse_sse(response.text)
    errors = [json.loads(data) for kind, data in events if kind == "error"]
    assert errors and errors[0]["kind"] == "context_unavailable"
    assert fake.queried == []


def test_foreground_result_hands_immediate_background_tail_to_watcher(
    stream_env, client, monkeypatch,
):
    from claude_agent_sdk import TaskStartedMessage, TaskNotificationMessage
    chat = stream_env
    sid = test_chat_stream._make_session(client)

    async def run():
        wire = asyncio.Queue()
        key = (sid, "claude-sonnet-4-6", "", "")
        def terminal():
            return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id=sid)
        class Client(test_chat_stream._FakeStreamClient):
            async def query(self, prompt):
                await super().query(prompt)
                for message in [
                    TaskStartedMessage(subtype="task_started", data={}, task_id="instant-task",
                        description="fixture", uuid="start", session_id=sid, tool_use_id="fixture-tool"),
                    AssistantMessage(content=[TextBlock("parent answer")],
                        model=key[1], uuid="parent-answer"),
                    terminal(),
                    TaskNotificationMessage(subtype="task_notification", data={},
                        task_id="instant-task", status="completed", output_file=None,
                        summary="fixture", uuid="settled", session_id=sid, tool_use_id="fixture-tool"),
                    AssistantMessage(content=[TextBlock("child final answer")],
                        model=key[1], uuid="child-final-answer"),
                    terminal(),
                ]:
                    wire.put_nowait(message)
            async def receive_messages(self):
                while True:
                    yield await wire.get()
        fake = Client([])
        stream = chat._ensure_session_stream(key, fake)
        chat._clients[key] = fake
        monkeypatch.setattr(chat, "get_client", lambda *_a, **_k: asyncio.sleep(0, result=fake))
        try:
            parent = await chat._start_turn(sid, "fixture")
            parent_rows = list(parent.replay_events())
            publish = parent.publish
            def capture(event):
                parent_rows.append(event)
                return publish(event)
            monkeypatch.setattr(parent, "publish", capture)
            await asyncio.wait_for(parent.task, 4)
            watcher = chat._task_watchers.get(sid)
            if watcher is not None:
                await asyncio.wait_for(watcher, 4)
            child = chat._recent_turns[sid]
            assert child is not parent and child.is_continuation
            parent_text = "".join(json.loads(row["data"])["text"]
                for row in parent_rows if row["event"] == "text")
            child_text = "".join(json.loads(row["data"])["text"]
                for row in child.replay_events() if row["event"] == "text")
            assert parent_text == "parent answer" and child_text == "child final answer"
            done = [json.loads(row["data"]) for row in child.replay_events()
                    if row["event"] == "done"][-1]
            assert done["assistant_uuid"] == "child-final-answer"
            assert done["status"] == "completed"
            assert not chat._sessions_with_inflight_tasks.get(sid)
            assert not chat._sdk_deliveries
            assert stream._failure is None and stream.attach_turn().empty()
        finally:
            await stream.aclose()
            chat._clients.pop(key, None)
    asyncio.run(run())
