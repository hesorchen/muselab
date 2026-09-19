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
    assert boundary.classify(_user()) == "background"
    assert boundary.classify(_result(origin, is_error=is_error)) == "current_result"
    assert boundary.classify(_text("next payload")) == "forward"


def test_background_result_does_not_end_human_turn(stream_env):
    boundary = stream_env._TurnResponseBoundary(set())
    assert boundary.classify(_user()) == "background"
    assert boundary.classify(_result(NOTICE, is_error=True)) == "background_result"
    assert boundary.classify(_result()) == "current_result"


def test_stale_result_and_task_lifecycle_do_not_consume_pending_owner(stream_env):
    boundary = stream_env._TurnResponseBoundary({"old-result"})
    assert boundary.classify(_user()) == "background"
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


async def _finish(chat, sid):
    broadcast = await chat._start_turn(sid, "synthetic prompt", model="claude-sonnet-4-6")
    try:
        await asyncio.wait_for(asyncio.shield(broadcast.task), timeout=2)
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
async def test_separate_background_result_keeps_its_payload_out_of_parent(
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
        assert text == "Parent answer."
        assert done["is_error"] is False
        if pumped:
            assert detached == separate
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
async def test_cancellation_releases_unresolved_payload_once(stream_env, monkeypatch):
    chat = stream_env
    pending = [_user(), _text("Unresolved answer.")]
    sid, fake, detached = await _prepare(chat, monkeypatch, [pending])
    broadcast = await chat._start_turn(sid, "synthetic prompt", model="claude-sonnet-4-6")
    try:
        await asyncio.wait_for(fake.drained.wait(), timeout=2)
        # Let the turn consumer drain the pump queue into its provisional buffer.
        for _ in range(10):
            await asyncio.sleep(0)
        broadcast.task.cancel()
        await asyncio.wait_for(asyncio.gather(broadcast.task, return_exceptions=True), 2)
        assert detached == pending
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
        text, done, events = await _finish(chat, sid)
        assert text == answer
        assert done["is_error"] is False
        kinds = [e["event"] for e in events]
        assert kinds.index("tool_use") < kinds.index("tool_result") < kinds.index("text")
        assert kinds.count("tool_use") == kinds.count("tool_result") == 1
        assert detached == []
    finally:
        await chat._drop_session_streams(sid)
