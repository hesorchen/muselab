"""Independent startup work overlaps without weakening query commit barriers."""
import asyncio

import pytest
from claude_agent_sdk import ResultMessage
from tests import test_chat_stream

stream_env = test_chat_stream.stream_env


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_preflight", [False, True])
async def test_recall_overlaps_preflight_and_is_cancelled_on_early_failure(
        stream_env, client, monkeypatch, fail_preflight):
    chat = stream_env
    sid = test_chat_stream._make_session(client)
    recall_started, release_recall, recall_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    preflight_started = asyncio.Event()
    original_prepare = chat.mem0.prepare_recall

    async def prepare(*args, **kwargs):
        recall_started.set()
        try:
            await release_recall.wait()
            return await original_prepare(*args, **kwargs)
        finally:
            recall_finished.set()

    class Client(test_chat_stream._FakeStreamClient):
        async def get_context_usage(self):
            # This is a dependency test, not a timing microbenchmark: a serial
            # implementation cannot reach this barrier while recall is pending.
            await asyncio.wait_for(recall_started.wait(), 1)
            preflight_started.set()
            return {"maxTokens": 200_000, "totalTokens": 1}

    fake = Client([ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id=sid)])

    async def get_client(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat.mem0, "prepare_recall", prepare)
    if fail_preflight:
        original_write = chat._write_active_turn_sidecar
        monkeypatch.setattr(chat, "_write_active_turn_sidecar", lambda *args:
                            False if recall_started.is_set() else original_write(*args))
    broadcast = await chat._start_turn(sid, "startup barrier fixture", model="claude-sonnet-4-6")
    if not fail_preflight:
        await asyncio.wait_for(preflight_started.wait(), 2)
        assert not recall_finished.is_set()
        assert fake.queried == []
        release_recall.set()
    async with asyncio.timeout(3):
        while not broadcast.done:
            await asyncio.sleep(.01)
    assert recall_finished.is_set()
    assert fake.queried == ([] if fail_preflight else ["startup barrier fixture"])
    assert sid not in chat.mem0._prepared_recalls
