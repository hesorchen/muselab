"""Independent startup work overlaps without weakening query commit barriers."""
import asyncio
import threading

import pytest
from claude_agent_sdk import ResultMessage
from tests import test_chat_stream

stream_env = test_chat_stream.stream_env


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_preflight", [False, "intent", "boundary"])
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

        def write_at_boundary(broadcast, *args):
            in_pump = broadcast.task is not None
            if recall_started.is_set() and in_pump == (fail_preflight == "boundary"):
                return False
            return original_write(broadcast, *args)

        monkeypatch.setattr(chat, "_write_active_turn_sidecar", write_at_boundary)
    start = chat._start_turn(sid, "startup barrier fixture", model="claude-sonnet-4-6")
    if fail_preflight == "intent":
        with pytest.raises(chat._TurnStartError):
            await start
        broadcast = chat._recent_turns[sid]
    else:
        broadcast = await start
    if not fail_preflight:
        await asyncio.wait_for(preflight_started.wait(), 2)
        assert not recall_finished.is_set()
        assert fake.queried == []
        release_recall.set()
    async with asyncio.timeout(3):
        while not broadcast.done:
            await asyncio.sleep(.01)
    assert recall_finished.is_set()
    assert broadcast.recall_task.done()
    assert fake.queried == ([] if fail_preflight else ["startup barrier fixture"])
    assert sid not in chat.mem0._prepared_recalls


def _result(sid):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id=sid)


async def _wait_done(broadcast):
    async with asyncio.timeout(3):
        while not broadcast.done:
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_context_probe_and_git_baseline_overlap_sidecar_write(
        stream_env, client, monkeypatch):
    """Read-only probes start before the sidecar write; writes stay ordered."""
    chat = stream_env
    sid = test_chat_stream._make_session(client)
    probe_started, baseline_started = threading.Event(), threading.Event()
    overlapped: list[bool] = []
    order: list[str] = []

    class Client(test_chat_stream._FakeStreamClient):
        async def get_context_usage(self):
            probe_started.set()
            await asyncio.sleep(.05)
            order.append("probe_done")
            return {"maxTokens": 200_000, "totalTokens": 1}

        async def query(self, prompt_or_gen):
            order.append("query")
            await super().query(prompt_or_gen)

    fake = Client([_result(sid)])

    async def get_client(*_args, **_kwargs):
        return fake

    original_write = chat._write_active_turn_sidecar
    original_baseline = chat._turn_delivery_baseline

    def write(broadcast, *args):
        if broadcast.task is not None and not overlapped:
            # A serial implementation reaches this thread before either probe.
            overlapped.append(probe_started.wait(2) and baseline_started.wait(2))
            order.append("sidecar")
        return original_write(broadcast, *args)

    def baseline(session_id):
        baseline_started.set()
        return original_baseline(session_id)

    def begin(*_args):
        order.append("delivery_begin")

    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat, "_write_active_turn_sidecar", write)
    monkeypatch.setattr(chat, "_turn_delivery_baseline", baseline)
    monkeypatch.setattr(chat, "_begin_turn_delivery", begin)
    broadcast = await chat._start_turn(sid, "overlap fixture", model="claude-sonnet-4-6")
    await _wait_done(broadcast)

    assert overlapped == [True]
    assert fake.queried == ["overlap fixture"]
    # The post-turn meter refresh may probe again after the query.
    assert order[:4] == ["sidecar", "probe_done", "delivery_begin", "query"]
    assert broadcast.perf_preflight_ms >= 0 and broadcast.perf_delivery_ms >= 0


@pytest.mark.asyncio
async def test_failed_boundary_write_cancels_early_probe_without_delivery(
        stream_env, client, monkeypatch):
    chat = stream_env
    sid = test_chat_stream._make_session(client)
    probe_cancelled = asyncio.Event()
    began: list[tuple] = []

    class Client(test_chat_stream._FakeStreamClient):
        async def get_context_usage(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                probe_cancelled.set()
                raise

    fake = Client([_result(sid)])

    async def get_client(*_args, **_kwargs):
        return fake

    original_write = chat._write_active_turn_sidecar

    def write(broadcast, *args):
        if broadcast.task is not None:
            return False
        return original_write(broadcast, *args)

    monkeypatch.setattr(chat, "get_client", get_client)
    monkeypatch.setattr(chat, "_write_active_turn_sidecar", write)
    monkeypatch.setattr(chat, "_begin_turn_delivery", lambda *a: began.append(a))
    broadcast = await chat._start_turn(sid, "boundary failure", model="claude-sonnet-4-6")
    await _wait_done(broadcast)

    await asyncio.wait_for(probe_cancelled.wait(), 1)
    assert began == []
    assert fake.queried == []


def test_task_delivery_begin_uses_precomputed_baseline(tmp_path, monkeypatch):
    from backend import task_delivery

    monkeypatch.setattr(task_delivery, "path", lambda sid: tmp_path / f"{sid}.json")
    monkeypatch.setattr(
        task_delivery, "inspect_workspace",
        lambda *_a, **_k: pytest.fail("baseline must not be re-inspected"))
    baseline = {"git": {"head": "abc"}}
    task_delivery.begin("s1", "t1", tmp_path, baseline=baseline)
    assert task_delivery.load("s1")["turns"][0]["baseline"] == baseline
