"""Tests for the server-side message queue.

Two levels:
  - state-machine unit tests on the sessions-layer queue helpers
    (enqueue / dequeue / requeue_head / reorder / set_queue_paused /
    remove_queue_item / clear_queue) — these are pure file-backed CRUD.
  - endpoint round-trips against /api/chat/sessions/{sid}/queue
    (GET / POST / DELETE / pause / reorder).

The drain dispatch is tested with its SDK turn launcher replaced by a narrow
fake, keeping queue permission and rollback semantics hermetic.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest


def _sess(app_module):
    """Pull the reloaded sessions module out of the backend.* tree (resolves
    against conftest's test-isolated SESS_DIR)."""
    from backend import sessions as sess
    return sess


# ---------- sessions-layer state machine ----------

def test_enqueue_preserves_fifo_order(app_module):
    sess = _sess(app_module)
    sid = "s-fifo"
    for t in ("one", "two", "three"):
        res = sess.enqueue_message(sid, t)
        assert res["ok"] is True
    q = sess.get_queue(sid)
    assert [it["text"] for it in q["items"]] == ["one", "two", "three"]
    assert q["paused"] is False


def test_enqueue_rejects_past_cap(app_module):
    sess = _sess(app_module)
    sid = "s-cap"
    for i in range(sess._QUEUE_MAX):
        assert sess.enqueue_message(sid, f"m{i}")["ok"] is True
    over = sess.enqueue_message(sid, "overflow")
    assert over["ok"] is False
    assert over["error"] == "queue_full"
    # Still exactly _QUEUE_MAX items — overflow not stored.
    assert len(sess.get_queue(sid)["items"]) == sess._QUEUE_MAX


def test_claim_binds_and_acknowledges_head_fifo(app_module):
    sess = _sess(app_module)
    sid = "s-deq"
    sess.enqueue_message(sid, "first")
    sess.enqueue_message(sid, "second")
    item = sess.claim_queue_message(sid)
    assert item["text"] == "first"
    queue = sess.get_queue(sid)
    assert queue["inflight"]["item"]["id"] == item["id"]
    assert [it["text"] for it in queue["items"]] == ["second"]
    sess.bind_queue_turn(sid, item["id"], "turn-1")
    assert sess.ack_queue_message(sid, item["id"], "turn-1") is True
    assert sess.get_queue(sid)["inflight"] is None
    assert sess.get_queue(sid)["revision"] > queue["revision"]


def test_dequeue_empty_queue_returns_none(app_module):
    sess = _sess(app_module)
    assert sess.dequeue_message("s-empty") is None


def test_dequeue_paused_returns_none(app_module):
    """A paused queue must not yield items to the drain even if non-empty."""
    sess = _sess(app_module)
    sid = "s-paused"
    sess.enqueue_message(sid, "waiting")
    sess.set_queue_paused(sid, True)
    assert sess.dequeue_message(sid) is None
    # Item still present — pause holds it, doesn't drop it.
    assert len(sess.get_queue(sid)["items"]) == 1


def test_release_claim_restores_to_front(app_module):
    sess = _sess(app_module)
    sid = "s-requeue"
    sess.enqueue_message(sid, "a")
    sess.enqueue_message(sid, "b")
    head = sess.claim_queue_message(sid)
    assert head["text"] == "a"
    sess.release_queue_claim(sid, head["id"])
    queue = sess.get_queue(sid)
    assert queue["inflight"] is None
    assert [it["text"] for it in queue["items"]] == ["a", "b"]


def test_requeue_head_bypasses_cap(app_module):
    """Legacy restore still bypasses the cap for an accepted item."""
    sess = _sess(app_module)
    sid = "s-requeue-cap"
    for i in range(sess._QUEUE_MAX):
        sess.enqueue_message(sid, f"m{i}")
    restored = {"id": "q-restored", "text": "back", "image_ids": "",
                "enqueued_at": 0}
    data = sess.requeue_head(sid, restored)
    assert data["items"][0]["id"] == "q-restored"
    assert len(data["items"]) == sess._QUEUE_MAX + 1


def test_bound_claim_is_not_released_by_wrong_turn(app_module):
    sess = _sess(app_module)
    sid = "s-bound"
    item = sess.enqueue_message(sid, "once")["item"]
    sess.claim_queue_message(sid)
    sess.bind_queue_turn(sid, item["id"], "turn-owner")
    assert sess.release_queue_claim(
        sid, item["id"], turn_id="turn-other") is False
    queue = sess.get_queue(sid)
    assert queue["items"] == []
    assert queue["inflight"]["turn_id"] == "turn-owner"
    assert sess.ack_queue_message(sid, item["id"], "turn-other") is False


def test_recover_bound_inflight_restores_once_but_pauses(app_module):
    sess = _sess(app_module)
    sid = "s-recover-inflight"
    item = sess.enqueue_message(sid, "recover once")["item"]
    sess.claim_queue_message(sid)
    sess.bind_queue_turn(sid, item["id"], "dead-turn")
    first = sess.recover_queue_inflight(sid)
    second = sess.recover_queue_inflight(sid)
    assert first["inflight"] is None
    assert first["paused"] is True
    assert [row["id"] for row in first["items"]] == [item["id"]]
    assert [row["id"] for row in second["items"]] == [item["id"]]


def test_recover_unbound_inflight_restores_paused_for_user_review(app_module):
    sess = _sess(app_module)
    sid = "s-recover-unbound"
    item = sess.enqueue_message(sid, "safe retry")["item"]
    sess.claim_queue_message(sid)
    recovered = sess.recover_queue_inflight(sid)
    assert recovered["paused"] is True
    assert [row["id"] for row in recovered["items"]] == [item["id"]]


def test_restart_recovery_pauses_waiting_queue_without_inflight(app_module):
    """A direct turn can die while follow-ups are still only waiting items.

    There is no queue inflight record in that case, so startup recovery must
    pause the queue itself instead of assuming the absent claim means the
    preceding turn completed successfully.
    """
    sess = _sess(app_module)
    sid = "s-recover-waiting"
    item = sess.enqueue_message(sid, "review after restart")["item"]

    recovered = sess.recover_queue_inflight(sid)

    assert recovered["inflight"] is None
    assert recovered["paused"] is True
    assert [row["id"] for row in recovered["items"]] == [item["id"]]


@pytest.mark.parametrize(
    "payload",
    [
        '{"revision": -1, "items": [], "inflight": null, "paused": false}',
        '{"items": [{"id": "", "text": "lost"}], "paused": false}',
        ('{"items": [], "inflight": {"item": {"text": "lost"}, '
         '"turn_id": "turn-1"}, "paused": false}'),
        ('{"items": [{"id": "same", "text": "waiting"}], '
         '"inflight": {"item": {"id": "same", "text": "running"}, '
         '"turn_id": "turn-1"}, "paused": false}'),
    ],
)
def test_restart_recovery_rejects_unsafe_queue_shapes(app_module, payload):
    sess = _sess(app_module)
    sid = "s-invalid-recovery"
    path = sess._queue_path(sid)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot parse queue sidecar"):
        sess.recover_queue_inflight(sid)

    assert path.read_text(encoding="utf-8") == payload


def test_required_session_enqueue_rejects_orphan_queue(app_module):
    sess = _sess(app_module)
    sid = "s-pruned-before-enqueue"

    result = sess.enqueue_message(sid, "must not orphan", require_session=True)

    assert result["ok"] is False
    assert result["error"] == "session_not_found"
    assert not sess._queue_path(sid).exists()


def test_required_session_enqueue_self_heals_sdk_only_session(app_module):
    sess = _sess(app_module)
    sid = "s-sdk-only-queue"
    sdk_meta = {
        "id": sid,
        "name": "SDK-only conversation",
        "model": "claude-sonnet-4-6",
        "cwd": str(sess.ROOT),
        "created_at": 123.0,
        "updated_at": 456.0,
        "auto_named": True,
    }

    result = sess.enqueue_message(
        sid,
        "accepted on SDK truth",
        require_session=True,
        existing_session=sdk_meta,
        sdk_verified=True,
    )

    assert result["ok"] is True
    assert sid in sess.indexed_session_ids()
    assert sess.get_queue(sid)["items"][0]["text"] == "accepted on SDK truth"


def test_indexed_enqueue_fast_path_reads_index_once_without_lifecycle_probe(
    app_module, monkeypatch,
):
    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    original_load_index = sess._load_index
    index_reads = 0

    def counted_load_index():
        nonlocal index_reads
        index_reads += 1
        return original_load_index()

    @contextlib.contextmanager
    def forbidden_lifecycle(_sid):
        raise AssertionError("indexed enqueue must not take lifecycle stripe")
        yield

    monkeypatch.setattr(sess, "_load_index", counted_load_index)
    monkeypatch.setattr(sess, "session_lifecycle_lock", forbidden_lifecycle)
    monkeypatch.setattr(
        sess,
        "sdk_get_session_info",
        lambda *_args, **_kwargs: pytest.fail("indexed enqueue probed SDK"),
    )

    result = sess.enqueue_existing_message(sid, "fast indexed enqueue")

    assert result["ok"] is True
    assert index_reads == 1
    assert result["queue"]["items"][0]["text"] == "fast indexed enqueue"


def test_indexed_enqueue_and_delete_remain_linearized(
    app_module, monkeypatch,
):
    """The fast path commits before DELETE or observes its tombstone."""
    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    save_entered = threading.Event()
    release_save = threading.Event()
    original_save = sess._save_queue

    def blocked_save(target_sid, data, *, bump=True):
        if (
            target_sid == sid
            and any(
                item.get("text") == "accepted before delete"
                for item in data.get("items", [])
            )
        ):
            save_entered.set()
            assert release_save.wait(timeout=2)
        return original_save(target_sid, data, bump=bump)

    monkeypatch.setattr(sess, "_save_queue", blocked_save)
    enqueued = []
    deleted = []
    enqueue_thread = threading.Thread(
        target=lambda: enqueued.append(
            sess.enqueue_existing_message(sid, "accepted before delete")
        ),
    )
    enqueue_thread.start()
    assert save_entered.wait(timeout=1)
    delete_thread = threading.Thread(
        target=lambda: deleted.append(sess.delete_session(sid)),
    )
    delete_thread.start()
    assert delete_thread.is_alive()

    release_save.set()
    enqueue_thread.join(timeout=2)
    delete_thread.join(timeout=2)

    assert not enqueue_thread.is_alive()
    assert not delete_thread.is_alive()
    assert enqueued[0]["ok"] is True
    assert deleted == [True]
    assert sid not in sess.indexed_session_ids()
    assert not sess._queue_path(sid).exists()


def test_enqueue_never_overwrites_corrupt_queue(app_module):
    sess = _sess(app_module)
    sid = "s-corrupt-runtime-queue"
    original = '{"items": [{"text": "accepted but missing id"}]}'
    path = sess._queue_path(sid)
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot parse queue sidecar"):
        sess.enqueue_message(sid, "new work")

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_lifespan_recovery_never_schedules_surviving_queue(
    app_module, monkeypatch,
):
    """Startup exposes paused work; only the explicit Resume API may drain it."""
    from backend import activity, chat, main, runtime_lifecycle, terminal

    sess = _sess(app_module)
    sid = "s-startup-recovery"
    sess.enqueue_message(sid, "wait for review")
    scheduled = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", scheduled.append)
    monkeypatch.setattr(
        activity.activity, "initialize_runtime_state", lambda: None)

    async def no_optional_services(*_args):
        return None

    async def no_workspace_index(*_args):
        return True

    async def no_terminal_start():
        return None

    async def no_shutdown(*_args, **_kwargs):
        return None

    def close_background_coroutines(coroutines):
        for coroutine in coroutines:
            coroutine.close()

    monkeypatch.setattr(main, "_start_optional_services", no_optional_services)
    monkeypatch.setattr(main, "_start_workspace_index", no_workspace_index)
    monkeypatch.setattr(main, "_launch_background_tasks",
                        close_background_coroutines)
    monkeypatch.setattr(terminal.manager, "start", no_terminal_start)
    monkeypatch.setattr(runtime_lifecycle, "shutdown_runtime", no_shutdown)

    async with main._lifespan(main.app):
        pass

    assert scheduled == []
    assert sess.get_queue(sid)["paused"] is True


@pytest.mark.asyncio
async def test_startup_recovery_continues_after_write_failure_then_fails_closed(
    app_module, monkeypatch,
):
    """One bad sidecar cannot leave later queues live or let boot proceed."""
    from backend import main

    sess = _sess(app_module)
    bad_sid = "s-startup-write-fails"
    good_sid = "s-startup-still-recovers"
    sess.enqueue_message(bad_sid, "must not disappear")
    sess.enqueue_message(good_sid, "must still be paused")
    original_atomic_write = sess.atomic_write_text
    bad_path = sess._queue_path(bad_sid)

    monkeypatch.setattr(
        sess,
        "list_queue_session_ids",
        lambda: [bad_sid, good_sid],
    )

    def fail_one_queue_write(path, *args, **kwargs):
        if path == bad_path:
            raise OSError("simulated disk failure")
        return original_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(sess, "atomic_write_text", fail_one_queue_write)

    with pytest.raises(RuntimeError, match="safely recover 1 message queue"):
        await main._recover_message_queues_at_startup(sess)

    # The failed queue remains unchanged on disk, while the later queue was
    # still visited and durably paused. The aggregate error makes lifespan
    # refuse to expose either one to automatic draining.
    assert sess.get_queue(bad_sid)["paused"] is False
    assert sess.get_queue(good_sid)["paused"] is True


@pytest.mark.asyncio
async def test_lifespan_does_not_yield_or_start_services_after_queue_failure(
    app_module, monkeypatch,
):
    from backend import activity, main

    sess = _sess(app_module)
    services_started = []
    yielded = False
    monkeypatch.setattr(sess, "ensure_private_session_storage", lambda: None)
    monkeypatch.setattr(
        activity.activity, "initialize_runtime_state", lambda: None)

    async def fail_recovery(_session_store):
        raise RuntimeError("simulated queue recovery failure")

    async def observe_optional_services(*_args):
        services_started.append(True)

    monkeypatch.setattr(
        main, "_recover_message_queues_at_startup", fail_recovery)
    monkeypatch.setattr(
        main, "_start_optional_services", observe_optional_services)

    with pytest.raises(RuntimeError, match="not durably completed"):
        async with main._lifespan(main.app):
            yielded = True

    assert yielded is False
    assert services_started == []


def test_prune_empty_sessions_preserves_waiting_and_inflight_queues(
    app_module, monkeypatch,
):
    """Auto-prune may remove an empty stub, never accepted queued work."""
    sess = _sess(app_module)
    monkeypatch.setenv("MUSELAB_PRUNE_EMPTY_SESSIONS", "true")
    monkeypatch.setattr(sess, "sdk_list_sessions", lambda **_kwargs: [])
    deleted_sdk_ids = []
    monkeypatch.setattr(
        "claude_agent_sdk.delete_session",
        lambda sid, **_kwargs: deleted_sdk_ids.append(sid),
    )

    waiting_sid = sess.create_session()["id"]
    inflight_sid = sess.create_session()["id"]
    empty_sid = sess.create_session()["id"]
    waiting = sess.enqueue_message(waiting_sid, "waiting")["item"]
    inflight = sess.enqueue_message(inflight_sid, "claimed")["item"]
    sess.claim_queue_message(inflight_sid)

    pruned = sess.prune_empty_sessions()

    assert empty_sid in pruned
    assert waiting_sid not in pruned
    assert inflight_sid not in pruned
    assert waiting_sid in sess.indexed_session_ids()
    assert inflight_sid in sess.indexed_session_ids()
    assert [row["id"] for row in sess.get_queue(waiting_sid)["items"]] == [
        waiting["id"],
    ]
    assert sess.get_queue(inflight_sid)["inflight"]["item"]["id"] == (
        inflight["id"]
    )
    assert empty_sid in deleted_sdk_ids


def test_prune_winner_cannot_be_revived_by_stale_index_snapshot(
    app_module, monkeypatch,
):
    """Only explicit SDK truth may self-heal; stale index metadata may not."""
    sess = _sess(app_module)
    monkeypatch.setenv("MUSELAB_PRUNE_EMPTY_SESSIONS", "true")
    monkeypatch.setattr(sess, "sdk_list_sessions", lambda **_kwargs: [])
    monkeypatch.setattr(
        "claude_agent_sdk.delete_session",
        lambda _sid, **_kwargs: None,
    )
    sid = sess.create_session()["id"]
    stale_meta, sdk_verified = sess.get_session_for_queue(sid)
    assert stale_meta is not None
    assert sdk_verified is False

    assert sid in sess.prune_empty_sessions()
    result = sess.enqueue_message(
        sid,
        "must not revive a pruned session",
        require_session=True,
        existing_session=stale_meta,
        sdk_verified=sdk_verified,
    )

    assert result["ok"] is False
    assert result["error"] == "session_not_found"
    assert sid not in sess.indexed_session_ids()
    assert not sess._queue_path(sid).exists()


@pytest.mark.asyncio
async def test_sdk_only_enqueue_and_explicit_delete_are_linearized(
    app_module, monkeypatch,
):
    """DELETE cannot land between an SDK-only proof and its queue commit."""
    from backend import chat

    sess = _sess(app_module)
    sid = "s-sdk-delete-race"
    sdk_meta = {
        "id": sid,
        "name": "SDK-only race",
        "cwd": str(sess.ROOT),
        "created_at": 123.0,
        "updated_at": 456.0,
        "auto_named": True,
    }
    probe_entered = threading.Event()
    release_probe = threading.Event()
    delete_entered = threading.Event()

    def blocked_sdk_probe(_sid):
        probe_entered.set()
        assert release_probe.wait(timeout=2)
        return sdk_meta, True

    def fake_delete(_sid):
        delete_entered.set()
        return sess.delete_session(_sid) or True

    monkeypatch.setattr(sess, "get_session_for_queue", blocked_sdk_probe)
    monkeypatch.setattr(chat, "_purge_session_storage_disk_locked", fake_delete)

    enqueue_task = asyncio.create_task(asyncio.to_thread(
        sess.enqueue_existing_message,
        sid,
        "accepted before delete",
    ))
    assert await asyncio.to_thread(probe_entered.wait, 1)
    delete_task = asyncio.create_task(asyncio.to_thread(
        chat.purge_session_storage,
        sid,
    ))
    await asyncio.sleep(0.05)
    assert not delete_entered.is_set()

    release_probe.set()
    result = await enqueue_task
    assert result["ok"] is True
    assert await delete_task is True
    assert delete_entered.is_set()
    assert sid not in sess.indexed_session_ids()
    assert not sess._queue_path(sid).exists()


def test_delete_waits_for_queue_rmw_and_prevents_orphan_rewrite(
    app_module, monkeypatch,
):
    """A claim begun before DELETE is removed; none can save after DELETE."""
    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    queued = sess.enqueue_message(sid, "accepted before delete")["item"]
    save_entered = threading.Event()
    release_save = threading.Event()
    original_save = sess._save_queue

    def blocked_save(target_sid, data, *, bump=True):
        if target_sid == sid and data.get("inflight"):
            save_entered.set()
            assert release_save.wait(timeout=2)
        return original_save(target_sid, data, bump=bump)

    monkeypatch.setattr(sess, "_save_queue", blocked_save)
    claimed = []
    deleted = []
    claim_thread = threading.Thread(
        target=lambda: claimed.append(sess.claim_queue_message(sid)),
    )
    claim_thread.start()
    assert save_entered.wait(timeout=1)
    delete_thread = threading.Thread(
        target=lambda: deleted.append(sess.delete_session(sid)),
    )
    delete_thread.start()
    assert delete_thread.is_alive()

    release_save.set()
    claim_thread.join(timeout=2)
    delete_thread.join(timeout=2)

    assert not claim_thread.is_alive()
    assert not delete_thread.is_alive()
    assert claimed[0]["id"] == queued["id"]
    assert deleted == [True]
    assert sid not in sess.indexed_session_ids()
    assert not sess._queue_path(sid).exists()
    assert sess.get_queue(sid)["inflight"] is None


def test_reorder_by_id(app_module):
    sess = _sess(app_module)
    sid = "s-reorder"
    ids = [sess.enqueue_message(sid, t)["item"]["id"]
           for t in ("x", "y", "z")]
    new_order = [ids[2], ids[0], ids[1]]
    data = sess.reorder_queue(sid, new_order)
    assert [it["id"] for it in data["items"]] == new_order


def test_reorder_appends_missing_ids_defensively(app_module):
    """Ids omitted from `order` keep their relative order at the tail; bogus
    ids in `order` are ignored."""
    sess = _sess(app_module)
    sid = "s-reorder-partial"
    ids = [sess.enqueue_message(sid, t)["item"]["id"]
           for t in ("x", "y", "z")]
    # Only mention the last id + a bogus one — others should trail in order.
    data = sess.reorder_queue(sid, [ids[2], "q-bogus"])
    result = [it["id"] for it in data["items"]]
    assert result[0] == ids[2]
    assert result[1:] == [ids[0], ids[1]]


def test_set_queue_paused_toggles(app_module):
    sess = _sess(app_module)
    sid = "s-toggle"
    sess.enqueue_message(sid, "m")
    assert sess.set_queue_paused(sid, True)["paused"] is True
    assert sess.get_queue(sid)["paused"] is True
    assert sess.set_queue_paused(sid, False)["paused"] is False


def test_pause_empty_queue_is_a_noop(app_module):
    """A pause cannot outlive (or predate) the work it protects."""
    sess = _sess(app_module)
    sid = "s-pause-empty"
    assert sess.set_queue_paused(sid, True)["paused"] is False
    assert sess.get_queue(sid)["items"] == []
    assert sess.get_queue(sid)["paused"] is False
    assert sess._queue_path(sid).exists()


def test_legacy_empty_paused_queue_is_normalized_before_next_enqueue(app_module):
    """Upgrade an already-stranded queue instead of only preventing new ones."""
    sess = _sess(app_module)
    sid = "s-legacy-empty-paused"
    sess._queue_path(sid).write_text(
        '{"items": [], "paused": true}', encoding="utf-8",
    )

    assert sess.get_queue(sid)["items"] == []
    assert sess.get_queue(sid)["paused"] is False
    sess.enqueue_message(sid, "fresh after upgrade")
    assert sess.dequeue_message(sid)["text"] == "fresh after upgrade"


def test_remove_queue_item(app_module):
    sess = _sess(app_module)
    sid = "s-remove"
    a = sess.enqueue_message(sid, "a")["item"]["id"]
    b = sess.enqueue_message(sid, "b")["item"]["id"]
    data = sess.remove_queue_item(sid, a)
    assert [it["id"] for it in data["items"]] == [b]
    # Removing a non-existent id is a no-op, not an error.
    data2 = sess.remove_queue_item(sid, "q-nope")
    assert [it["id"] for it in data2["items"]] == [b]


def test_remove_cannot_steal_executing_inflight(app_module):
    sess = _sess(app_module)
    sid = "s-remove-inflight"
    item = sess.enqueue_message(sid, "executing")["item"]
    sess.claim_queue_message(sid)
    sess.bind_queue_turn(sid, item["id"], "turn-live")
    with pytest.raises(ValueError, match="currently executing"):
        sess.remove_queue_item(sid, item["id"])
    assert sess.get_queue(sid)["inflight"]["turn_id"] == "turn-live"


def test_clear_preserves_executing_inflight(app_module):
    sess = _sess(app_module)
    sid = "s-clear-inflight"
    item = sess.enqueue_message(sid, "executing")["item"]
    sess.enqueue_message(sid, "waiting")
    sess.claim_queue_message(sid)
    sess.bind_queue_turn(sid, item["id"], "turn-live")
    cleared = sess.clear_queue(sid)
    assert cleared["items"] == []
    assert cleared["inflight"]["turn_id"] == "turn-live"


def test_removing_the_last_item_also_clears_paused(app_module):
    """`paused` must not outlive the items it was protecting.

    Real failure, 2026-07-25: a wall-clock-capped turn aborted and paused a
    2-item queue. The user deleted both stale items by hand, then sent two new
    messages — which never went out. dequeue_message returns None while paused,
    so every subsequent completed turn silently skipped the drain, and the
    banner that would have explained it was gated on `!streaming`. The flag
    describes "queued work stopped auto-draining"; with no work left it has no
    referent and is purely a trap for the NEXT enqueue.
    """
    sess = _sess(app_module)
    sid = "s-unpause-on-empty"
    a = sess.enqueue_message(sid, "a")["item"]["id"]
    b = sess.enqueue_message(sid, "b")["item"]["id"]
    sess.set_queue_paused(sid, True)

    # Removing one of two leaves the pause intact — there IS still work.
    data = sess.remove_queue_item(sid, a)
    assert data["paused"] is True

    data = sess.remove_queue_item(sid, b)
    assert data["items"] == []
    assert data["paused"] is False
    # Keep a tiny empty sidecar so queue revision remains monotonic and a stale
    # browser cannot resurrect the removed item.
    assert sess._queue_path(sid).exists()
    # And the next message drains normally instead of vanishing.
    sess.enqueue_message(sid, "fresh")
    assert sess.dequeue_message(sid)["text"] == "fresh"


def test_clear_queue_empties_and_unpauses(app_module):
    sess = _sess(app_module)
    sid = "s-clear"
    sess.enqueue_message(sid, "m")
    sess.set_queue_paused(sid, True)
    sess.clear_queue(sid)
    q = sess.get_queue(sid)
    assert q["items"] == []
    assert q["paused"] is False


def test_empty_queue_keeps_revision_tombstone(app_module):
    sess = _sess(app_module)
    sid = "s-revision-tombstone"
    first = sess.enqueue_message(sid, "m")["queue"]["revision"]
    sess.remove_queue_item(sid, sess.get_queue(sid)["items"][0]["id"])
    empty = sess.get_queue(sid)
    assert empty["items"] == []
    assert empty["revision"] > first
    next_revision = sess.enqueue_message(sid, "next")["queue"]["revision"]
    assert next_revision > empty["revision"]


# ---------- endpoint round-trips ----------

def _mint_session(client, auth) -> str:
    r = client.post("/api/chat/sessions", headers=auth, json={"name": "q-test"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture()
def queue_autodrain_disabled(monkeypatch):
    """Keep CRUD endpoint tests from launching a real headless SDK turn."""
    from backend import chat

    monkeypatch.setattr(chat, "_schedule_queue_drain", lambda _sid: None)


def test_queue_endpoint_enqueue_and_get(client, auth, queue_autodrain_disabled):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "hello"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.get(f"/api/chat/sessions/{sid}/queue", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert [it["text"] for it in body["items"]] == ["hello"]
    assert [it["display_text"] for it in body["items"]] == ["hello"]


def test_queue_endpoint_accepts_and_indexes_sdk_only_session(
    client, auth, app_module, monkeypatch, queue_autodrain_disabled,
):
    sess = _sess(app_module)
    sid = "s-sdk-only-endpoint"
    sdk_meta = {
        "id": sid,
        "name": "SDK-only endpoint conversation",
        "model": "claude-sonnet-4-6",
        "permission": "default",
        "cwd": str(sess.ROOT),
        "created_at": 123.0,
        "updated_at": 456.0,
        "auto_named": True,
    }
    original_get_session = sess.get_session_for_queue
    monkeypatch.setattr(
        sess,
        "get_session_for_queue",
        lambda target_sid: (
            (sdk_meta, True)
            if target_sid == sid
            else original_get_session(target_sid)
        ),
    )

    response = client.post(
        f"/api/chat/sessions/{sid}/queue",
        headers=auth,
        json={"text": "queue against SDK truth", "permission": "default"},
    )

    assert response.status_code == 200, response.text
    assert sid in sess.indexed_session_ids()
    assert sess.get_queue(sid)["items"][0]["text"] == "queue against SDK truth"


def test_queue_endpoint_rejects_missing_session_without_orphan(
    client, auth, app_module, queue_autodrain_disabled,
):
    sess = _sess(app_module)
    sid = "s-missing-endpoint"

    response = client.post(
        f"/api/chat/sessions/{sid}/queue",
        headers=auth,
        json={"text": "must not orphan", "permission": "default"},
    )

    assert response.status_code == 404
    assert not sess._queue_path(sid).exists()


@pytest.mark.asyncio
async def test_cancelled_enqueue_joins_commit_and_schedules_drain(
    app_module, monkeypatch,
):
    """A cancelled HTTP waiter cannot leave committed work unscheduled."""
    from backend import chat

    entered = threading.Event()
    release = threading.Event()
    scheduled = []

    def slow_commit(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return {"ok": True, "item": {"id": "q-committed"}, "queue": {}}

    monkeypatch.setattr(chat.sess, "enqueue_existing_message", slow_commit)
    monkeypatch.setattr(chat, "_schedule_queue_drain", scheduled.append)
    task = asyncio.create_task(chat.enqueue_api(
        "s-cancelled-enqueue",
        chat.QueueEnqueueReq(text="durable"),
        chat.BackgroundTasks(),
    ))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduled == ["s-cancelled-enqueue"]


def test_queue_endpoint_preserves_bounded_selection_quote_presentation(
    client, auth, queue_autodrain_disabled,
):
    sid = _mint_session(client, auth)
    quote = {
        "id": "quote-1",
        "source": "preview",
        "role": "",
        "sessionId": sid,
        "messageId": "",
        "path": "README.md",
        "text": "selected context",
        "truncated": False,
    }
    response = client.post(
        f"/api/chat/sessions/{sid}/queue",
        headers=auth,
        json={
            "text": "引用自 `README.md`：\n\n> selected context\n\nquestion",
            "display_text": "question",
            "selection_quotes": [quote],
        },
    )

    assert response.status_code == 200, response.text
    item = client.get(
        f"/api/chat/sessions/{sid}/queue", headers=auth,
    ).json()["items"][0]
    assert item["display_text"] == "question"
    assert item["selection_quotes"] == [quote]


def test_queue_endpoint_schedules_drain_kick(client, auth, monkeypatch):
    from backend import chat

    kicks = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", kicks.append)
    sid = _mint_session(client, auth)
    response = client.post(
        f"/api/chat/sessions/{sid}/queue",
        headers=auth,
        json={"text": "wake me"},
    )

    assert response.status_code == 200, response.text
    assert kicks == [sid]


@pytest.mark.asyncio
async def test_queue_drain_kick_is_deferred_to_response_background(
    app_module, monkeypatch,
):
    """The queue ACK is built before rollover/drain work can start."""
    from backend import chat

    sid = _sess(app_module).create_session()["id"]
    kicks = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", kicks.append)
    background = chat.BackgroundTasks()

    response = await chat.enqueue_api(
        sid,
        chat.QueueEnqueueReq(text="ack first"),
        background,
    )

    assert response["ok"] is True
    assert kicks == []
    await background()
    assert kicks == [sid]


@pytest.mark.asyncio
async def test_response_sends_final_body_before_queue_background_callback():
    """Pin Starlette's response ordering relied on by the queue ACK path."""
    from fastapi import BackgroundTasks
    from starlette.responses import JSONResponse

    order = []
    background = BackgroundTasks()

    async def kick():
        order.append("drain")

    async def send(message):
        if message["type"] == "http.response.start":
            order.append("start")
        elif message["type"] == "http.response.body":
            assert message.get("more_body", False) is False
            order.append("final-body")

    async def receive():
        return {"type": "http.disconnect"}

    background.add_task(kick)
    response = JSONResponse({"ok": True}, background=background)
    await response({"type": "http"}, receive, send)

    assert order == ["start", "final-body", "drain"]


def test_queue_endpoint_rejects_empty_message(
    client, auth, queue_autodrain_disabled,
):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "   "})
    assert r.status_code == 400


def test_queue_endpoint_full_returns_409(
    client, auth, queue_autodrain_disabled,
):
    sid = _mint_session(client, auth)
    from backend import sessions as sess
    for i in range(sess._QUEUE_MAX):
        ok = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                         json={"text": f"m{i}"})
        assert ok.status_code == 200
    over = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                       json={"text": "overflow"})
    assert over.status_code == 409


def test_queue_endpoint_reorder_roundtrip(
    client, auth, queue_autodrain_disabled,
):
    sid = _mint_session(client, auth)
    ids = []
    for t in ("a", "b", "c"):
        r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                        json={"text": t})
        ids.append(r.json()["item"]["id"])
    new_order = [ids[2], ids[1], ids[0]]
    r = client.post(f"/api/chat/sessions/{sid}/queue/reorder", headers=auth,
                    json={"order": new_order})
    assert r.status_code == 200
    assert [it["id"] for it in r.json()["items"]] == new_order


def test_queue_endpoint_remove_item(client, auth, queue_autodrain_disabled):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "doomed"})
    item_id = r.json()["item"]["id"]
    r = client.delete(f"/api/chat/sessions/{sid}/queue/{item_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_queue_endpoint_clear(client, auth, queue_autodrain_disabled):
    sid = _mint_session(client, auth)
    client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                json={"text": "m1"})
    client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                json={"text": "m2"})
    r = client.delete(f"/api/chat/sessions/{sid}/queue", headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["items"] == []
    assert r.json()["inflight"] is None
    assert r.json()["paused"] is False
    assert client.get(f"/api/chat/sessions/{sid}/queue",
                      headers=auth).json()["items"] == []


def test_queue_endpoint_pause_toggle(
    client, auth, monkeypatch, queue_autodrain_disabled,
):
    from backend import chat

    drains = []

    async def fake_drain(sid):
        drains.append(sid)

    # Resuming deliberately invokes the headless drain. Keep this endpoint
    # test hermetic: spawning a real Claude SDK subprocess is out of scope.
    monkeypatch.setattr(chat, "_maybe_drain_queue", fake_drain)
    sid = _mint_session(client, auth)
    client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                json={"text": "m"})
    r = client.post(f"/api/chat/sessions/{sid}/queue/pause", headers=auth,
                    json={"paused": True})
    assert r.status_code == 200
    assert r.json()["paused"] is True
    assert client.get(f"/api/chat/sessions/{sid}/queue",
                      headers=auth).json()["paused"] is True
    # Resuming kicks _maybe_drain_queue; with no live turn + no SDK the drain
    # dispatch is out of unit scope, but the endpoint must still return cleanly
    # and clear the paused flag.
    r = client.post(f"/api/chat/sessions/{sid}/queue/pause", headers=auth,
                    json={"paused": False})
    assert r.status_code == 200
    assert r.json()["paused"] is False
    assert drains == [sid]


def test_queue_endpoint_requires_auth(client):
    """At least one route enforces the token — no header → 401."""
    r = client.get("/api/chat/sessions/whatever/queue")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_late_enqueue_after_empty_completion_check_kicks_idle_queue(
    app_module, monkeypatch,
):
    """Closing the exact lost-wakeup window starts the just-enqueued item."""
    import asyncio
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    starts = []
    started = asyncio.Event()

    async def fake_start_turn(session_id, prompt, **kwargs):
        starts.append((session_id, prompt, kwargs))
        started.set()

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)

    # The previous turn/background watcher has just run its final drain and
    # observed no work. A stale browser then posts while it still renders the
    # session as streaming — this used to leave the item stranded forever.
    await chat._maybe_drain_queue(sid)
    background = chat.BackgroundTasks()
    response = await chat.enqueue_api(
        sid,
        chat.QueueEnqueueReq(text="late"),
        background,
    )
    await background()
    await asyncio.wait_for(started.wait(), timeout=1)
    task = chat._queue_drain_tasks.get(sid)
    if task is not None:
        await task

    assert response["ok"] is True
    assert [(item[0], item[1]) for item in starts] == [(sid, "late")]
    queue = sess.get_queue(sid)
    assert queue["items"] == []
    assert queue["inflight"]["item"]["text"] == "late"


@pytest.mark.asyncio
async def test_concurrent_drain_kicks_serialize_dequeue_and_preserve_fifo(
    app_module, monkeypatch,
):
    import asyncio
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "first")
    sess.enqueue_message(sid, "second")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    starts = []

    async def fake_start_turn(_sid, prompt, **kwargs):
        starts.append(prompt)
        item_id = kwargs["queue_item_id"]
        turn_id = f"turn-{prompt}"
        sess.bind_queue_turn(sid, item_id, turn_id)
        if prompt == "first":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        sess.ack_queue_message(sid, item_id, turn_id)

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    first = asyncio.create_task(chat._maybe_drain_queue(sid))
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(chat._maybe_drain_queue(sid))
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first, second)
    assert starts == ["first", "second"]
    assert sess.get_queue(sid)["items"] == []


@pytest.mark.asyncio
async def test_cancelled_scheduled_drain_restores_accepted_message(
    app_module, monkeypatch,
):
    import asyncio
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "survive restart")
    entered = asyncio.Event()

    async def stalled_start(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat, "_start_turn", stalled_start)
    task = asyncio.create_task(chat._maybe_drain_queue(sid))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    queue = sess.get_queue(sid)
    assert [item["text"] for item in queue["items"]] == ["survive restart"]
    assert queue["inflight"] is None
    assert queue["paused"] is False


@pytest.mark.asyncio
async def test_bound_start_failure_releases_and_pauses_exact_claim(
    app_module, monkeypatch,
):
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    queued = sess.enqueue_message(sid, "startup fails")["item"]

    async def bound_failure(session_id, _prompt, **kwargs):
        broadcast = chat.TurnBroadcast(session_id)
        broadcast.queue_item_id = kwargs["queue_item_id"]
        sess.bind_queue_turn(
            session_id, broadcast.queue_item_id, broadcast.turn_id)
        settled = sess.release_queue_claim(
            session_id,
            broadcast.queue_item_id,
            turn_id=broadcast.turn_id,
            pause=True,
        )
        raise chat._TurnStartError(
            "cold start failed", queue_claim_settled=settled)

    monkeypatch.setattr(chat, "_start_turn", bound_failure)
    await chat._maybe_drain_queue(sid)

    queue = sess.get_queue(sid)
    assert queue["inflight"] is None
    assert queue["paused"] is True
    assert [row["id"] for row in queue["items"]] == [queued["id"]]


@pytest.mark.asyncio
async def test_cancelled_bound_drain_does_not_duplicate_live_owner(
    app_module, monkeypatch,
):
    import asyncio
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    queued = sess.enqueue_message(sid, "exactly once")["item"]
    entered = asyncio.Event()

    async def bound_then_stall(session_id, _prompt, **kwargs):
        broadcast = chat.TurnBroadcast(session_id)
        broadcast.queue_item_id = kwargs["queue_item_id"]
        chat._active_turns[session_id] = broadcast
        sess.bind_queue_turn(
            session_id, kwargs["queue_item_id"], broadcast.turn_id)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat, "_start_turn", bound_then_stall)
    task = asyncio.create_task(chat._maybe_drain_queue(sid))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    queue = sess.get_queue(sid)
    assert queue["items"] == []
    assert queue["inflight"]["item"]["id"] == queued["id"]
    chat._active_turns.pop(sid, None)


@pytest.mark.asyncio
async def test_drain_replays_snapshot_without_reverting_session_permission(
    app_module, monkeypatch,
):
    from backend import chat

    sess = _sess(app_module)
    meta = sess.create_session(permission="plan")
    sid = meta["id"]
    sess.enqueue_message(sid, "queued", permission="default")
    observed = {}

    async def fake_start_turn(session_id, prompt, **kwargs):
        observed.update(session_id=session_id, prompt=prompt, **kwargs)

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    await chat._maybe_drain_queue(sid)

    assert observed["permission"] == "default"
    assert observed["persist_permission"] is False
    assert observed["queue_item_id"]
    assert sess.get_session(sid)["permission"] == "plan"


@pytest.mark.asyncio
async def test_drain_waits_while_background_task_pending(
    app_module, monkeypatch,
):
    """The watcher advances FIFO only after its response boundary closes."""
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "follow-up")
    starts = []

    async def fake_start_turn(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    chat._sessions_with_inflight_tasks[sid] = {"task-1"}
    watcher = asyncio.create_task(asyncio.sleep(60))
    chat._task_watchers[sid] = watcher
    try:
        await chat._maybe_drain_queue(sid)
    finally:
        chat._sessions_with_inflight_tasks.pop(sid, None)
        chat._task_watchers.pop(sid, None)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    assert starts == []
    queue = sess.get_queue(sid)
    assert [item["text"] for item in queue["items"]] == ["follow-up"]
    assert queue.get("inflight") is None


@pytest.mark.asyncio
async def test_drain_pauses_missing_attachments_without_sending_text(
    app_module, monkeypatch,
):
    """Expired/restart-lost uploads must never become a text-only turn."""
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    missing_id = "expired-upload-id"
    queued = sess.enqueue_message(
        sid,
        "keep this recoverable",
        image_ids=missing_id,
    )["item"]
    starts = []

    async def fake_start_turn(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    chat._image_store.pop(missing_id, None)

    await chat._maybe_drain_queue(sid)

    queue = sess.get_queue(sid)
    assert starts == []
    assert queue["inflight"] is None
    assert queue["paused"] is True
    assert [row["id"] for row in queue["items"]] == [queued["id"]]
    assert queue["items"][0]["text"] == "keep this recoverable"
    public = chat.get_queue_api(sid, chat.Response())
    assert public["items"][0]["attachments"] == [
        {"id": missing_id, "available": False},
    ]


@pytest.mark.asyncio
async def test_drain_rechecks_and_atomically_rolls_back_attachment_after_slow_startup(
    app_module, monkeypatch,
):
    """A valid precheck can expire while the SDK client is starting.

    The authoritative all-or-none claim lives inside `_start_turn`, after that
    await. It must not query text-only, and must close every piece of startup
    state while retaining the exact durable item for edit/reattach.
    """
    from backend import chat
    from backend.activity import activity as activity_service

    sess = _sess(app_module)
    sid = sess.create_session(model="claude-sonnet-4-6")["id"]
    aid = "expires-during-client-start"
    retained_aid = "still-valid-after-rollback"
    chat._image_store[aid] = {
        "kind": "text",
        "mime": "text/plain",
        "name": "evidence.txt",
        "raw": b"required evidence",
        "text": "required evidence",
        "ts": chat.time.time(),
    }
    retained_entry = {
        "kind": "text",
        "mime": "text/plain",
        "name": "still-valid.txt",
        "raw": b"still valid",
        "text": "still valid",
        "ts": chat.time.time(),
    }
    chat._image_store[retained_aid] = retained_entry
    queued = sess.enqueue_message(
        sid,
        "answer using the attachment",
        image_ids=f"{retained_aid},{aid}",
    )["item"]

    # Simulate a restart breadcrumb that this newly accepted queued turn
    # supersedes. The failed pre-query startup must not leave it behind.
    stale = chat.TurnBroadcast(sid)
    stale.user_text = "prior interrupted turn"
    chat._write_active_turn_sidecar(stale)
    chat._interrupted_at_startup[sid] = {"sid": sid}
    assert chat._active_turn_path(sid).exists()

    queried = []
    activity_transitions = []
    notifications = []

    class NeverQueriedClient:
        async def query(self, prompt):
            queried.append(prompt)

    async def slow_get_client(*_args, **_kwargs):
        # `_maybe_drain_queue` already passed its preliminary availability
        # check. Expire the upload across this real startup await so only the
        # final atomic claim can catch it.
        assert aid in chat._image_store
        chat._image_store[aid]["ts"] = (
            chat.time.time() - chat._IMAGE_TTL_S - 1
        )
        await asyncio.sleep(0)
        return NeverQueriedClient()

    original_start_activity = chat._start_activity_early
    original_finish_activity = chat._finish_activity

    async def tracked_start_activity(_sid, broadcast, prompt):
        assert _sid == sid
        await original_start_activity(_sid, broadcast, prompt)
        activity_transitions.append(("start", broadcast.turn_id))

    async def tracked_finish_activity(_sid, broadcast, status):
        assert _sid == sid
        # Keep the reservation until the old Activity row is closed; otherwise
        # a direct turn could start a new row and this cleanup would finish it.
        assert chat._active_turns.get(sid) is broadcast
        await original_finish_activity(_sid, broadcast, status)
        activity_transitions.append((status, broadcast.turn_id))

    monkeypatch.setattr(chat, "get_client", slow_get_client)
    monkeypatch.setattr(chat, "_start_activity_early", tracked_start_activity)
    monkeypatch.setattr(chat, "_finish_activity", tracked_finish_activity)
    monkeypatch.setattr(
        chat, "_notify_queue_paused_on_error", notifications.append)

    await chat._maybe_drain_queue(sid)

    queue = sess.get_queue(sid)
    assert queried == []
    assert queue["inflight"] is None
    assert queue["paused"] is True
    assert [row["id"] for row in queue["items"]] == [queued["id"]]
    assert queue["items"][0]["image_ids"] == f"{retained_aid},{aid}"
    # All-or-none means the valid sibling was not partially consumed when the
    # other id failed final validation; editing can still reuse it.
    assert chat._image_store[retained_aid] is retained_entry
    assert aid not in chat._image_store
    assert sid not in chat._active_turns
    assert sid not in chat._interrupted_at_startup
    assert not chat._active_turn_path(sid).exists()
    assert [kind for kind, _turn_id in activity_transitions] == ["start", "failed"]
    assert activity_transitions[0][1] == activity_transitions[1][1]
    activity_row = next(
        row for row in activity_service.list() if row["session_id"] == sid)
    assert activity_row["state"] == "failed"
    assert activity_row["activity_source"] == "queued"
    assert notifications == [sid]
