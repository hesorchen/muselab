"""Bulk cleanup must respect updates made while an earlier deletion waits."""
import asyncio
import time
from types import SimpleNamespace

import pytest


@pytest.fixture()
def purge_env(app_module, monkeypatch):
    from backend import chat

    monkeypatch.setattr(chat.sess, "sdk_list_sessions", lambda **_kwargs: [])
    monkeypatch.setattr(chat.sess, "sdk_get_session_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat, "sdk_delete_session", lambda *_args, **_kwargs: None)
    sids = ["bulk-blocking", "bulk-changed", "bulk-unchanged"]
    for sid in sids:
        chat.sess.register_session(sid, name=sid)
    with chat.sess._INDEX_LOCK:
        rows = chat.sess._load_index()
        for i, row in enumerate(rows):
            row["updated_at"] = time.time() - 30 * 86400 - i
        chat.sess._save_index(rows)
    return chat, sids


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["pin", "recent_activity", "recent_sdk_cache", "none"])
async def test_bulk_delete_rechecks_waiting_victim(purge_env, monkeypatch, mutation):
    chat, (blocking, changed, unchanged) = purge_env
    entered = asyncio.Event()
    release = asyncio.Event()
    original_purge = chat.purge_session_storage_async

    async def gated_purge(sid):
        if sid == blocking:
            entered.set()
            await release.wait()
        return await original_purge(sid)

    monkeypatch.setattr(chat, "purge_session_storage_async", gated_purge)
    request = asyncio.create_task(chat.purge_old_sessions_api(chat.PurgeOldReq(days=7)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "pin":
            assert chat.sess.set_pin(changed, True) is True
        elif mutation in {"recent_activity", "recent_sdk_cache"}:
            assert chat.sess.rename_session(changed, "Recent user update") is True
            if mutation == "recent_sdk_cache":
                old = (time.time() - 30 * 86400) * 1000
                info = SimpleNamespace(
                    session_id=changed, custom_title="Earlier native title",
                    first_prompt="Synthetic prompt", tag=None,
                    created_at=old, last_modified=old,
                )
                monkeypatch.setattr(
                    chat.sess, "sdk_get_session_info",
                    lambda sid, **_kwargs: info if sid == changed else None,
                )
                # A GET between the local rename and SDK mirror caches the
                # old native mtime. Complete the mirror before deletion resumes.
                cached = chat.sess.get_session_meta(changed)
                assert cached["updated_at"] < time.time() - 7 * 86400
                info.last_modified = time.time() * 1000
                assert chat.sess.get_session_meta(changed) is cached
        release.set()
        result = await asyncio.wait_for(request, timeout=5)
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    expected = {blocking, unchanged} if mutation != "none" else {blocking, changed, unchanged}
    assert set(result["ids"]) == expected
    assert result["deleted"] == len(expected)
    stored = chat.sess.get_session_meta(changed)
    if mutation == "pin":
        assert stored is not None and stored["pinned"] is True
        assert not chat.sess.session_is_deleting(changed)
    elif mutation in {"recent_activity", "recent_sdk_cache"}:
        assert stored is not None and stored["name"] == "Recent user update"
        assert not chat.sess.session_is_deleting(changed)
    else:
        assert stored is None
    assert chat.sess.get_session_meta(blocking) is None
    assert chat.sess.get_session_meta(unchanged) is None


@pytest.mark.asyncio
async def test_bulk_dry_run_preserves_pinned_and_current_session(purge_env):
    chat, (pinned, current, eligible) = purge_env
    chat.sess.set_pin(pinned, True)
    result = await chat.purge_old_sessions_api(
        chat.PurgeOldReq(days=7, keep_id=current, dry_run=True))
    assert result["ids"] == [eligible]
    assert result["count"] == 1
    assert all(chat.sess.get_session_meta(sid) is not None for sid in (pinned, current, eligible))
    assert not any(chat.sess.session_is_deleting(sid) for sid in (pinned, current, eligible))
