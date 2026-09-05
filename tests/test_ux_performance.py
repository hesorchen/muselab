"""Bounded diagnostic work and exact-generation context caching."""
import asyncio
import threading
import time

import pytest


def test_diagnostic_queue_is_nonblocking_bounded_and_ordered():
    from backend.diagnostic_worker import DiagnosticWorker
    worker = DiagnosticWorker("test-diagnostics", capacity=2)
    entered, release = threading.Event(), threading.Event()
    seen = []

    def slow():
        entered.set()
        release.wait(timeout=3)
        seen.append(0)

    try:
        assert worker.submit(slow)
        assert entered.wait(timeout=1)
        started = time.monotonic()
        assert worker.submit(seen.append, 1)
        assert worker.submit(seen.append, 2)
        assert not worker.submit(seen.append, 3)
        assert time.monotonic() - started < 0.1
        assert worker.dropped == 1
    finally:
        release.set()
        worker.close()
    assert seen == [0, 1, 2]


@pytest.mark.asyncio
async def test_context_snapshot_requires_unchanged_client_generation(monkeypatch):
    from backend.sdk_compat import MuseLabSDKClient, ClaudeSDKClient
    client = MuseLabSDKClient()
    release = asyncio.Event()

    async def context(self):
        await release.wait()
        return {"totalTokens": 100, "maxTokens": 10000}

    monkeypatch.setattr(ClaudeSDKClient, "get_context_usage", context)
    pending = asyncio.create_task(client.get_context_usage())
    await asyncio.sleep(0)
    client._invalidate_context_snapshot()
    release.set()
    await pending
    assert client.cached_context_usage() is None
    await client.get_context_usage()
    snapshot = client.cached_context_usage()
    snapshot["totalTokens"] = 999
    assert client.cached_context_usage()["totalTokens"] == 100
    assert client.cached_context_usage(max_age_s=-1) is None
    client._invalidate_context_snapshot()
    assert client.cached_context_usage() is None


@pytest.mark.asyncio
async def test_slow_hook_diagnostic_does_not_block_sdk_reader(app_module, monkeypatch):
    from backend import chat
    from claude_agent_sdk import SystemMessage
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return None

    monkeypatch.setattr(chat.hook_traces, "observe", slow)
    try:
        await asyncio.wait_for(chat._observe_sdk_stream_message(
            ("synthetic-hook-session", "model", "auto", ""),
            SystemMessage(subtype="hook_response", data={"hook_id": "synthetic"}),
        ), timeout=0.2)
        assert await asyncio.to_thread(entered.wait, 1)
        # Another lifecycle/read cycle is runnable while the disk is blocked.
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    finally:
        release.set()
        if chat._hook_diagnostic_worker:
            await asyncio.to_thread(chat._hook_diagnostic_worker.close)
            chat._hook_diagnostic_worker = None


@pytest.mark.asyncio
async def test_old_hook_generation_cannot_recreate_purged_trace(app_module, monkeypatch):
    from backend import chat
    calls = []
    monkeypatch.setattr(chat.hook_traces, "observe", lambda *args, **kwargs: calls.append(1))
    sid = "purged-hook-fixture"
    chat._hook_diagnostic_generations[sid] = 1
    chat._hook_trace_job(asyncio.get_running_loop(), sid, object(), "", "foreground", None, 0)
    assert calls == []
