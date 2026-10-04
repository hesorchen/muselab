"""A stale metadata reader must not refill a cache after invalidation."""

import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "cancel_reader"),
    [("count", False), ("delete", True), ("invalidate", True)],
)
async def test_metadata_reader_cannot_publish_after_invalidation(
    app_module, monkeypatch, mutation, cancel_reader,
):
    from backend import sessions as sess

    # Keep the unrelated entry warm independently of runner scheduling.
    monkeypatch.setattr(sess, "_META_CACHE_TTL_S", 60)
    sid = sess.create_session("before")["id"]
    other = sess.create_session("unrelated")["id"]
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    probes = []
    sdk_title = "before invalidation"

    def gated_info(requested_sid, **_kwargs):
        probes.append(requested_sid)
        info = None
        if requested_sid == sid:
            if mutation == "invalidate":
                info = SimpleNamespace(
                    session_id=sid, custom_title=sdk_title, first_prompt="",
                    created_at=0, last_modified=0, tag=None,
                )
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "metadata probe was not released"
        return info

    monkeypatch.setattr(sess, "sdk_get_session_info", gated_info)
    unrelated_cached = sess.get_session_meta(other)

    def read_metadata():
        try:
            return sess.get_session_meta(sid)
        finally:
            loop.call_soon_threadsafe(finished.set)

    reader = asyncio.create_task(asyncio.to_thread(read_metadata))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if cancel_reader:
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
        if mutation == "count":
            sess.set_message_count(sid, 9, turn_count=4)
        elif mutation == "delete":
            assert sess.delete_session(sid)
        else:
            sdk_title = "after invalidation"
            sess.invalidate_sessions_cache()
    finally:
        release.set()
        # Cancelling to_thread's awaiter does not stop the actual reader.
        # Wait for its cache publication before checking the next read.
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.gather(reader, return_exceptions=True)

    refilled_after_invalidation = sid in sess._META_CACHE
    current = sess.get_session_meta(sid)
    if mutation == "delete":
        assert current is None
        assert all(row["id"] != sid for row in sess._load_index())
        assert not sess._sidecar_path(sid).exists()
    elif mutation == "count":
        assert current["message_count"] == 9
        assert current["turn_count"] == 4
        # A one-session write retains warm metadata for other sessions.
        assert sess.get_session_meta(other) is unrelated_cached
        assert probes.count(other) == 1
    else:
        assert current["name"] == "after invalidation"
        assert probes.count(sid) == 2
    assert not refilled_after_invalidation
