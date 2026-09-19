"""Notification folds must preserve the owning turn's answer and completion."""

import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage, TaskNotificationMessage
from claude_agent_sdk._internal.message_parser import parse_message

from tests import test_chat_stream

stream_env = test_chat_stream.stream_env


NOTICE = {"kind": "task-notification"}
HUMAN = {"kind": "human"}


def _user(text="synthetic notification", origin=NOTICE):
    return parse_message(
        {
            "type": "user",
            "message": {"role": "user", "content": text},
            "origin": origin,
        }
    )


def _text(text):
    return parse_message(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _delta(text):
    return parse_message(
        {
            "type": "stream_event",
            "uuid": "synthetic-delta",
            "session_id": "synthetic",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


def _result(origin=None, *, is_error=False, uuid=None):
    return parse_message(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "session_id": "synthetic",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": is_error,
            "num_turns": 1,
            "origin": origin,
            "uuid": uuid,
            # Empty result prevents the final-text recovery path masking lost frames.
            "result": "",
            "errors": ["synthetic failure"] if is_error else [],
        }
    )


@pytest.mark.parametrize("origin", [None, HUMAN], ids=["default", "human"])
@pytest.mark.parametrize("is_error", [False, True], ids=["success", "error"])
def test_result_uses_its_own_origin_after_notification(stream_env, origin, is_error):
    boundary = stream_env._TurnResponseBoundary(set())
    assert boundary.classify(_user()) == "forward"
    assert boundary.classify(_result(origin, is_error=is_error)) == "current_result"
    assert boundary.classify(_text("next payload")) == "forward"


def test_background_result_does_not_end_human_turn(stream_env):
    boundary = stream_env._TurnResponseBoundary(set())
    assert boundary.classify(_user()) == "forward"
    assert boundary.classify(_result(NOTICE, is_error=True)) == "background_result"
    assert boundary.classify(_result()) == "current_result"


def test_stale_result_and_task_lifecycle_do_not_consume_pending_owner(stream_env):
    boundary = stream_env._TurnResponseBoundary({"old-result"})
    assert boundary.classify(_user()) == "forward"
    lifecycle = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="synthetic-task",
        status="completed",
        output_file="",
        summary="done",
        uuid="synthetic-lifecycle",
        session_id="synthetic",
    )
    assert boundary.classify(lifecycle) == "forward"
    assert boundary.classify(_result(uuid="old-result")) == "stale_result"
    assert boundary.classify(_result()) == "current_result"


class _ScriptedClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.queue = asyncio.Queue()
        self.drained = asyncio.Event()

    async def query(self, _prompt, **_kwargs):
        self.drained.clear()
        for message in self.scripts.pop(0):
            self.queue.put_nowait(message)

    async def receive_messages(self):
        while True:
            message = await self.queue.get()
            if isinstance(message, asyncio.Event):
                await message.wait()
                continue
            yield message
            if self.queue.empty():
                self.drained.set()

    async def receive_response(self):
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def get_context_usage(self):
        return {"maxTokens": 200_000, "totalTokens": 10}


async def _prepare(chat, monkeypatch, scripts, *, pumped=True):
    from backend import sessions

    sid = sessions.create_session(model="claude-sonnet-4-6")["id"]
    fake = _ScriptedClient(scripts)
    detached = []

    async def get_client(*_args, **_kwargs):
        if pumped:
            chat._ensure_session_stream((sid, "claude-sonnet-4-6", "auto", ""), fake)
        return fake

    async def consume_idle(_key, message):
        detached.append(message)

    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat, "_consume_sdk_idle_message", consume_idle)
    monkeypatch.setattr(chat, "_schedule_queue_drain", lambda _sid: None)
    return sid, fake, detached


async def _finish(chat, sid, *, completion_timeout=2):
    broadcast = await chat._start_turn(sid, "synthetic prompt", model="claude-sonnet-4-6")
    try:
        await asyncio.wait_for(asyncio.shield(broadcast.task), timeout=completion_timeout)
        assert broadcast.done and broadcast.result_forwarded
        assert sid not in chat._active_turns
        events = list(broadcast.replay_events())
        assert [e["event"] for e in events].count("done") == 1
        assert events[-1]["event"] == "done"
        text = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "text")
        done = json.loads(events[-1]["data"])
        return text, done, events
    finally:
        if not broadcast.task.done():
            broadcast.task.cancel()
            await asyncio.gather(broadcast.task, return_exceptions=True)
        broadcast.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, HUMAN], ids=["default", "human"])
@pytest.mark.parametrize("is_error", [False, True], ids=["success", "error"])
async def test_folded_notification_preserves_text_and_terminal(
    stream_env,
    monkeypatch,
    origin,
    is_error,
):
    chat = stream_env
    script = [_user(), _delta("Final "), _text("Final answer."), _result(origin, is_error=is_error)]
    sid, fake, detached = await _prepare(chat, monkeypatch, [script])
    try:
        text, done, _ = await _finish(chat, sid)
        assert text == "Final answer."
        assert done["is_error"] is is_error
        assert fake.queue.empty()
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("pumped", [True, False], ids=["pump", "bounded-iterator"])
@pytest.mark.parametrize("side_error", [False, True], ids=["side-success", "side-error"])
async def test_side_response_streams_once_without_ending_human_turn(
    stream_env,
    monkeypatch,
    pumped,
    side_error,
):
    chat = stream_env
    separate = [_user(), _text("Background-only answer."), _result(NOTICE, is_error=side_error)]
    folded = [_user(), _delta("Parent "), _text("Parent answer."), _result()]
    sid, _, detached = await _prepare(chat, monkeypatch, [separate + folded], pumped=pumped)
    try:
        text, done, _ = await _finish(chat, sid)
        assert text == "Background-only answer.Parent answer."
        assert done["is_error"] is False
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)


@pytest.mark.asyncio
async def test_multiple_folds_and_human_steering_keep_stream_order(stream_env, monkeypatch):
    chat = stream_env
    script = [
        _user(),
        _delta("Before "),
        _user(origin=HUMAN),
        _delta("steering; "),
        _user(),
        _delta("after."),
        _text("Before steering; after."),
        _result(HUMAN),
    ]
    sid, _, detached = await _prepare(chat, monkeypatch, [script])
    try:
        text, done, _ = await _finish(chat, sid)
        assert text == "Before steering; after."
        assert done["is_error"] is False
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)


@pytest.mark.asyncio
async def test_folded_turn_does_not_replay_into_successor(stream_env, monkeypatch):
    chat = stream_env
    scripts = [[_user(), _text("First answer."), _result()], [_text("Second answer."), _result()]]
    sid, _, detached = await _prepare(chat, monkeypatch, scripts)
    try:
        assert (await _finish(chat, sid))[0] == "First answer."
        assert (await _finish(chat, sid))[0] == "Second answer."
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)


@pytest.mark.asyncio
async def test_cancellation_does_not_replay_already_visible_payload(stream_env, monkeypatch):
    chat = stream_env
    pending = [_user(), _text("Unresolved answer.")]
    sid, fake, detached = await _prepare(chat, monkeypatch, [pending])
    broadcast = await chat._start_turn(sid, "synthetic prompt", model="claude-sonnet-4-6")
    try:
        await asyncio.wait_for(fake.drained.wait(), timeout=2)
        async def wait_for_text():
            while not any(e["event"] == "text" for e in broadcast.replay_events()):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_text(), timeout=2)
        broadcast.task.cancel()
        await asyncio.wait_for(asyncio.gather(broadcast.task, return_exceptions=True), 2)
        assert detached == []
        assert any(e["event"] == "text" for e in broadcast.replay_events())
        assert sid not in chat._active_turns
    finally:
        if not broadcast.task.done():
            broadcast.task.cancel()
            await asyncio.gather(broadcast.task, return_exceptions=True)
        await chat._drop_session_streams(sid)
        broadcast.close()


@pytest.mark.asyncio
async def test_folded_tool_round_trip_and_long_answer_survive(stream_env, monkeypatch):
    chat = stream_env
    tool = parse_message(
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "synthetic-read",
                        "name": "Read",
                        "input": {"file_path": "fixture.txt"},
                    }
                ],
            },
        }
    )
    result = parse_message(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "synthetic-read",
                        "content": "synthetic tool output",
                    }
                ],
            },
        }
    )
    answer = "chunk " * 512
    script = [
        _user(),
        tool,
        result,
        *[_delta("chunk ") for _ in range(512)],
        _text(answer),
        _result(),
    ]
    sid, _, detached = await _prepare(chat, monkeypatch, [script])
    try:
        # The 512-frame burst exercises ordering, not runner throughput. macOS
        # CI can exceed two seconds under pytest-xdist; the separate gated
        # progress test below still requires output before Result is released.
        text, done, events = await _finish(chat, sid, completion_timeout=10)
        assert text == answer
        assert done["is_error"] is False
        kinds = [e["event"] for e in events]
        assert kinds.index("tool_use") < kinds.index("tool_result") < kinds.index("text")
        assert kinds.count("tool_use") == kinds.count("tool_result") == 1
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("pumped", [True, False], ids=["pump", "bounded-iterator"])
@pytest.mark.parametrize("side_result", [False, True], ids=["fold", "side-result"])
async def test_notification_progress_is_visible_before_terminal_result(
    stream_env, monkeypatch, pumped, side_result,
):
    """A long tool round trip must not wait for the query's Result to render."""
    chat = stream_env
    gate = asyncio.Event()
    tool = parse_message({"type": "assistant", "message": {
        "model": "claude-sonnet-4-6", "content": [{
            "type": "tool_use", "id": "synthetic-live-tool", "name": "Read",
            "input": {"file_path": "fixture.txt"},
        }],
    }})
    result = parse_message({"type": "user", "message": {
        "role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "synthetic-live-tool",
            "content": "synthetic output",
        }],
    }})
    script = [_user(), _delta("Working now."), _text("Working now."),
              tool, result, *([_result(NOTICE)] if side_result else []),
              gate, _text("Finished."), _result()]
    sid, _, detached = await _prepare(chat, monkeypatch, [script], pumped=pumped)
    broadcast = await chat._start_turn(sid, "synthetic prompt", model="claude-sonnet-4-6")
    try:
        async def progress_visible():
            while True:
                events = list(broadcast.replay_events())
                kinds = [event["event"] for event in events]
                if "text" in kinds and "tool_use" in kinds and "tool_result" in kinds:
                    return events
                await asyncio.sleep(0.01)
        events = await asyncio.wait_for(progress_visible(), timeout=1)
        assert not broadcast.done and not broadcast.result_forwarded
        assert sid in chat._active_turns
        assert not any(e["event"] == "done" for e in events)
        assert "synthetic-live-tool" not in broadcast.active_tool_use_ids
        gate.set()
        await asyncio.wait_for(asyncio.shield(broadcast.task), timeout=2)
        assert broadcast.done and broadcast.result_forwarded
        assert [e["event"] for e in broadcast.replay_events()].count("done") == 1
        assert detached == []
    finally:
        gate.set()
        if not broadcast.task.done():
            broadcast.task.cancel()
            await asyncio.gather(broadcast.task, return_exceptions=True)
        await chat._drop_session_streams(sid)
        broadcast.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_owner", ["side", "human"])
@pytest.mark.parametrize("error_source", ["system", "assistant"])
async def test_burst_error_stays_with_its_result_owner(
    stream_env, monkeypatch, error_owner, error_source,
):
    from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock

    error = (SystemMessage(subtype="local_command", data={
        "content": "API Error: synthetic provider unavailable",
    }) if error_source == "system" else AssistantMessage(
        content=[TextBlock(text="API Error: synthetic provider unavailable")],
        model="<synthetic>", error="unknown",
    ))
    side = [_user(), _text("Side progress."),
            *([error] if error_owner == "side" else []), _result(NOTICE)]
    human = [*([error] if error_owner == "human" else []),
             _text("Human progress."), _result()]
    chat = stream_env
    sid, _, detached = await _prepare(chat, monkeypatch, [side + human])
    try:
        _, done, _ = await _finish(chat, sid)
        assert done["is_error"] is (error_owner == "human")
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)
