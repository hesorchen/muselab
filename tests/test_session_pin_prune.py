"""Pruning must not recreate an empty session through an admitted pin write."""
import asyncio
import json
import threading

import pytest


@pytest.fixture
def empty_session(app_module, monkeypatch):
    import claude_agent_sdk
    from backend import sessions as sess

    monkeypatch.setenv("MUSELAB_PRUNE_EMPTY_SESSIONS", "false")
    monkeypatch.setattr(sess, "sdk_list_sessions", lambda **_kwargs: [])
    monkeypatch.setattr(sess, "sdk_get_session_info", lambda *_args, **_kwargs: None)
    sdk_deletes = []

    def delete_empty_sdk(sid, *, directory):
        assert directory == str(sess.ROOT)
        sdk_deletes.append(sid)

    monkeypatch.setattr(claude_agent_sdk, "delete_session", delete_empty_sdk)
    row = sess.create_session()
    assert row["auto_named"] is True
    sid = row["id"]
    sess._save_sidecar(sid, {"messages": {}})
    sess._save_queue(sid, sess.get_queue(sid))
    return sess, sid, sdk_deletes


class _PruneQueueProbe:
    """Observe actual mutex admission, then delegate to the same real mutex."""
    def __init__(self, real_lock, loop, attempted):
        self.real = real_lock
        self.loop = loop
        self.attempted = attempted
        self.prune_thread_id = None
        self.reported = False

    def __enter__(self):
        if threading.get_ident() == self.prune_thread_id and not self.reported:
            self.reported = True
            acquired = self.real.acquire(blocking=False)
            self.loop.call_soon_threadsafe(self.attempted.set_result, acquired)
            if not acquired:
                self.real.acquire()
        else:
            self.real.acquire()
        return self

    def __exit__(self, *_args):
        self.real.release()

    def __getattr__(self, name):
        return getattr(self.real, name)


class _PinIndexProbe:
    """Pause only the pin caller before its real index mutex acquisition."""
    def __init__(self, real_lock, loop, checked, release):
        self.real = real_lock
        self.loop = loop
        self.checked = checked
        self.release = release
        self.pin_thread_id = None
        self.reported = False

    def __enter__(self):
        if threading.get_ident() == self.pin_thread_id and not self.reported:
            self.reported = True
            self.loop.call_soon_threadsafe(self.checked.set_result, True)
            assert self.release.wait(10), "pin index admission was not released"
        self.real.acquire()
        return self

    def __exit__(self, *_args):
        self.real.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_prune_cannot_recreate_row_before_pin_index_commit(
    empty_session, monkeypatch, record_property, pinned,
):
    sess, sid, sdk_deletes = empty_session
    monkeypatch.setenv("MUSELAB_PRUNE_EMPTY_SESSIONS", "true")
    loop = asyncio.get_running_loop()
    checked = loop.create_future()
    attempted = loop.create_future()
    release = threading.Event()
    real_is_deleting = sess.session_is_deleting
    probe = _PruneQueueProbe(sess._QUEUE_LOCK, loop, attempted)
    monkeypatch.setattr(sess, "_QUEUE_LOCK", probe)

    def prune():
        probe.prune_thread_id = threading.get_ident()
        return sess.prune_empty_sessions()

    index_probe = _PinIndexProbe(sess._INDEX_LOCK, loop, checked, release)
    monkeypatch.setattr(sess, "_INDEX_LOCK", index_probe)

    def pin():
        index_probe.pin_thread_id = threading.get_ident()
        return sess.set_pin(sid, pinned)

    pin_task = asyncio.create_task(asyncio.to_thread(pin))
    prune_task = None
    try:
        await asyncio.wait_for(checked, 10)
        prune_task = asyncio.create_task(asyncio.to_thread(prune))
        acquired = await asyncio.wait_for(attempted, 10)
        # Reproduce the old window without a sleep: if prune owns the real
        # queue lock, let its actual disk deletion finish before pin resumes.
        # If pin still owns it, release pin so the genuine blocked prune runs.
        if acquired:
            await prune_task
        release.set()
        pin_result = await pin_task
        removed = await prune_task
        index = json.loads(sess.INDEX.read_text(encoding="utf-8"))
        indexed = any(row["id"] == sid for row in index)
        pruned = sid in removed
        outcome = {
            "pin_result": pin_result, "pruned": pruned, "indexed": indexed,
            "sidecar_exists": sess._sidecar_path(sid).exists(),
            "queue_exists": sess._queue_path(sid).exists(),
            "tombstone": real_is_deleting(sid),
            "sdk_delete_calls": len(sdk_deletes),
        }
        record_property("prune_acquired_before_pin_release", acquired)
        record_property("actual_outcome", json.dumps(outcome, sort_keys=True))
        if pruned:
            assert not indexed, outcome
            assert not outcome["sidecar_exists"] and not outcome["queue_exists"]
            assert outcome["tombstone"] is True
            assert outcome["sdk_delete_calls"] == 1
            assert pin_result is None or pin_result is False
        else:
            assert pin_result is pinned
            assert indexed and index[0]["pinned"] is pinned
            assert outcome["sidecar_exists"] and outcome["queue_exists"]
            assert outcome["tombstone"] is False
            assert outcome["sdk_delete_calls"] == 0
    finally:
        release.set()
        await asyncio.gather(
            pin_task, *([prune_task] if prune_task else []), return_exceptions=True,
        )


@pytest.mark.parametrize("pinned", [True, False])
def test_pin_without_enabled_prune_keeps_normal_state(
    empty_session, record_property, pinned,
):
    sess, sid, sdk_deletes = empty_session
    assert sess.prune_empty_sessions() == []
    assert sess.set_pin(sid, pinned) is pinned
    index = json.loads(sess.INDEX.read_text(encoding="utf-8"))
    assert index[0]["id"] == sid and index[0]["pinned"] is pinned
    assert sess._sidecar_path(sid).exists() and sess._queue_path(sid).exists()
    assert not sess.session_is_deleting(sid)
    assert sdk_deletes == []
    record_property("actual_outcome", json.dumps({
        "pin_result": pinned, "pruned": False, "indexed": True,
        "sidecar_exists": True, "queue_exists": True,
        "tombstone": False, "sdk_delete_calls": 0,
    }, sort_keys=True))

@pytest.mark.parametrize("pinned", [True, False])
def test_completed_prune_fences_later_pin(
    empty_session, monkeypatch, record_property, pinned,
):
    sess, sid, sdk_deletes = empty_session
    monkeypatch.setenv("MUSELAB_PRUNE_EMPTY_SESSIONS", "true")
    assert sess.prune_empty_sessions() == [sid]
    assert sess.set_pin(sid, pinned) is None
    index = json.loads(sess.INDEX.read_text(encoding="utf-8"))
    assert not any(row["id"] == sid for row in index)
    assert not sess._sidecar_path(sid).exists()
    assert not sess._queue_path(sid).exists()
    assert sess.session_is_deleting(sid)
    assert sdk_deletes == [sid]
    record_property("actual_outcome", json.dumps({
        "pin_result": None, "pruned": True, "indexed": False,
        "sidecar_exists": False, "queue_exists": False,
        "tombstone": True, "sdk_delete_calls": 1,
    }, sort_keys=True))
