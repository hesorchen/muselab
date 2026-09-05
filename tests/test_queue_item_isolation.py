"""Queue failures belong to an item, not to the session's execution policy."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def test_cancelled_adjustment_does_not_block_followers_or_consume_capacity(app_module):
    from backend import sessions as sess

    sid = "item-isolation"
    old = sess.enqueue_message(
        sid, "cancel this input", delivery="adjust",
        target_turn_id="old-turn", command_uuid="old-command",
        steering_state="queued",
    )["item"]
    sess.update_queue_steering_state(sid, "cancelled", item_id=old["id"])
    for i in range(sess._QUEUE_MAX):
        assert sess.enqueue_message(sid, f"follow-up {i}")["ok"]
    assert not sess.enqueue_message(sid, "over capacity")["ok"]
    snapshot = sess.get_queue(sid)
    assert snapshot["paused"] is False
    assert snapshot["items"][0]["queue_issue"] == "cancelled"
    assert sess.claim_queue_message(sid)["text"] == "follow-up 0"
    assert sess.get_queue(sid)["items"][0]["id"] == old["id"]


@pytest.mark.parametrize("issue", [
    "cancelled", "failed", "delivery_unknown", "attachment_unavailable",
])
def test_terminal_item_cannot_be_replayed_by_legacy_resume(app_module, issue):
    from backend import sessions as sess

    sid = "terminal-item"
    first = sess.enqueue_message(sid, "do not replay", image_ids="opaque-attachment")["item"]
    second = sess.enqueue_message(sid, "independent follow-up")["item"]
    sess.claim_queue_message(sid)
    sess.bind_queue_turn(sid, first["id"], "owner")
    assert not sess.release_queue_claim(sid, first["id"], turn_id="stale", issue=issue)
    assert sess.release_queue_claim(sid, first["id"], turn_id="owner", issue=issue)
    sess.set_queue_paused(sid, False)
    assert sess.claim_queue_message(sid)["id"] == second["id"]
    assert sess.ack_queue_message(sid, second["id"], "")
    assert sess.claim_queue_message(sid) is None
    retained = sess.get_queue(sid)["items"][0]
    assert retained["queue_issue"] == issue
    assert retained["image_ids"] == "opaque-attachment"


@pytest.mark.parametrize("bound", [False, True])
def test_restart_resumes_only_provably_unstarted_input(app_module, bound):
    from backend import sessions as sess

    sid = "restart-item-isolation"
    first = sess.enqueue_message(sid, "claimed input")["item"]
    second = sess.enqueue_message(sid, "unstarted input")["item"]
    sess.claim_queue_message(sid)
    if bound:
        sess.bind_queue_turn(sid, first["id"], "dead-runtime")
    sess.recover_queue_inflight(sid)
    snapshot = sess.recover_queue_inflight(sid)
    assert sess.recover_queue_inflight(sid)["revision"] == snapshot["revision"]
    assert snapshot["paused"] is False
    assert len(snapshot["items"]) == 2
    assert sess.claim_queue_message(sid)["id"] == (second if bound else first)["id"]
    if bound:
        assert snapshot["items"][0]["queue_issue"] == "delivery_unknown"


def test_legacy_paused_head_is_isolated_without_replaying_it(app_module):
    from backend import sessions as sess

    sid = "legacy-item-isolation"
    sess._queue_path(sid).write_text(json.dumps({
        "items": [
            {"id": "old-head", "text": "possibly executed"},
            {"id": "new-tail", "text": "unstarted"},
        ],
        "paused": True,
    }))
    recovered = sess.recover_queue_inflight(sid)
    assert recovered["policy_version"] == 2
    assert not recovered["paused"]
    assert recovered["items"][0]["queue_issue"] == "delivery_unknown"
    assert sess.claim_queue_message(sid)["id"] == "new-tail"


def test_legacy_cancelled_record_does_not_quarantine_another_head(app_module):
    from backend import sessions as sess

    sid = "legacy-cancel-isolation"
    sess._queue_path(sid).write_text(json.dumps({
        "items": [
            {"id": "old-cancel", "text": "cancelled", "steering_state": "cancelled"},
            {"id": "new-tail", "text": "unstarted"},
        ],
        "paused": True,
    }))
    sess.recover_queue_inflight(sid)
    assert sess.claim_queue_message(sid)["id"] == "new-tail"


def test_runtime_migration_preserves_issues_without_poisoning_child(app_module):
    from backend import sessions as sess

    first = sess.enqueue_message("source", "failed input")["item"]
    sess.claim_queue_message("source")
    sess.release_queue_claim("source", first["id"], issue="failed")
    follower = sess.enqueue_message("source", "next input")["item"]
    migrated = sess.migrate_queue("source", "child")
    assert not migrated["target"]["paused"]
    assert sess.claim_queue_message("child")["id"] == follower["id"]
    assert sess.get_queue("child")["items"][0]["queue_issue"] == "failed"


@pytest.mark.asyncio
async def test_failed_start_wakes_next_item_without_retrying_failure(app_module, monkeypatch):
    from backend import chat, sessions as sess

    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "failed input")
    sess.enqueue_message(sid, "next input")
    launched = []
    finished = asyncio.Event()

    async def start(session_id, prompt, **kwargs):
        launched.append(prompt)
        if prompt == "failed input":
            raise chat._TurnStartError("synthetic startup failure")
        item_id = kwargs["queue_item_id"]
        sess.bind_queue_turn(session_id, item_id, "synthetic-owner")
        assert sess.ack_queue_message(session_id, item_id, "synthetic-owner")
        finished.set()

    monkeypatch.setattr(chat, "_start_turn", start)
    monkeypatch.setattr(chat, "_notify_queue_paused_on_error", lambda _: None)
    await chat._maybe_drain_queue(sid)
    await asyncio.wait_for(finished.wait(), 2)
    assert launched == ["failed input", "next input"]
    assert sess.get_queue(sid)["items"][0]["queue_issue"] == "failed"
    assert not sess.get_queue(sid)["paused"]
    await chat._maybe_drain_queue(sid)
    assert len(launched) == 2


@pytest.mark.asyncio
async def test_shutdown_cannot_start_successor_while_cancelling_owner(app_module, monkeypatch):
    from backend import chat, sessions as sess

    sid = "shutdown-fence"
    sess.enqueue_message(sid, "unstarted")
    monkeypatch.setattr(chat, "_queue_runtime_closing", True)
    chat._schedule_queue_drain(sid)
    chat._schedule_queue_drain_retry(sid)
    await chat._maybe_drain_queue(sid)
    assert sid not in chat._queue_drain_tasks
    assert sid not in chat._queue_drain_retry_tasks
    assert sess.get_queue(sid)["inflight"] is None


@pytest.mark.asyncio
async def test_late_stop_control_cannot_hit_next_queued_turn(app_module, monkeypatch):
    from backend import chat, sessions as sess

    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "next input")
    old = chat.TurnBroadcast(sid)
    chat._active_turns[sid] = old
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    launched = []

    async def interrupt_sdk():
        # The reply may end before the SDK finishes acknowledging Stop.
        old.finish()
        chat._active_turns.pop(sid, None)
        entered.set()
        await release.wait()

    async def no_watchdog(*_args, **_kwargs):
        pass

    async def start(session_id, prompt, **kwargs):
        assert release.is_set()
        launched.append(prompt)
        assert sess.ack_queue_message(session_id, kwargs["queue_item_id"], "")
        finished.set()

    key = (sid, "synthetic-model", "auto", "")
    chat._clients[key] = SimpleNamespace(interrupt=interrupt_sdk)
    monkeypatch.setattr(chat, "_force_stop_after_grace", no_watchdog)
    monkeypatch.setattr(chat, "_start_turn", start)
    task = asyncio.create_task(chat.interrupt(sid, turn_id=old.turn_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert chat._interrupt_control_owners[sid] is old
        with pytest.raises(chat._TurnBusy):
            await chat._admit_turn(sid, "must not overtake a stop control")
        await chat._maybe_drain_queue(sid)
        assert launched == []
        assert sess.get_queue(sid)["inflight"] is None
        assert not sess.get_queue(sid)["paused"]
        release.set()
        await asyncio.wait_for(task, 1)
        await asyncio.wait_for(finished.wait(), 1)
        assert sid not in chat._interrupt_control_owners
        assert launched == ["next input"]
    finally:
        release.set()
        await task
        chat._clients.pop(key, None)
        chat._active_turns.pop(sid, None)
        old.close()


@pytest.mark.asyncio
async def test_removing_head_wakes_other_pending_messages(app_module, monkeypatch):
    from backend import chat, sessions as sess

    sid = sess.create_session()["id"]
    first = sess.enqueue_message(sid, "remove me")["item"]
    sess.enqueue_message(sid, "keep me")
    scheduled = []
    monkeypatch.setattr(chat, "_schedule_queue_drain", scheduled.append)
    result = await chat.remove_queue_item_api(sid, first["id"])
    assert scheduled == [sid]
    assert [row["text"] for row in result["items"]] == ["keep me"]
    assert not result["paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "failed"])
async def test_startup_terminal_settlement_continues_other_items(app_module, monkeypatch, terminal):
    from backend import chat, sessions as sess

    sid = sess.create_session()["id"]
    current = sess.enqueue_message(sid, "current input")["item"]
    sess.enqueue_message(sid, "next input")
    sess.claim_queue_message(sid)
    old = chat.TurnBroadcast(sid)
    old.queue_item_id = current["id"]
    sess.bind_queue_turn(sid, current["id"], old.turn_id)
    chat._active_turns[sid] = old
    finished = asyncio.Event()
    launched = []

    async def start(session_id, prompt, **kwargs):
        assert chat._active_turns.get(session_id) is not old
        launched.append(prompt)
        assert sess.ack_queue_message(session_id, kwargs["queue_item_id"], "")
        finished.set()

    monkeypatch.setattr(chat, "_start_turn", start)
    try:
        if terminal == "cancelled":
            old.cancelled = True
            await chat._finish_cancelled_startup(sid, old)
        else:
            await chat._abort_turn_startup(sid, old, "failed", error_text="synthetic failure")
        await asyncio.wait_for(finished.wait(), 2)
        assert launched == ["next input"]
        retained = sess.get_queue(sid)
        assert retained["items"][0]["queue_issue"] == terminal
        assert retained["items"][0]["id"] == current["id"]
        assert retained["paused"] is False
    finally:
        chat._active_turns.pop(sid, None)
        old.close()
