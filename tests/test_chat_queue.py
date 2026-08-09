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


def test_dequeue_pops_head_fifo(app_module):
    sess = _sess(app_module)
    sid = "s-deq"
    sess.enqueue_message(sid, "first")
    sess.enqueue_message(sid, "second")
    item = sess.dequeue_message(sid)
    assert item["text"] == "first"
    assert [it["text"] for it in sess.get_queue(sid)["items"]] == ["second"]


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


def test_requeue_head_restores_to_front(app_module):
    sess = _sess(app_module)
    sid = "s-requeue"
    sess.enqueue_message(sid, "a")
    sess.enqueue_message(sid, "b")
    head = sess.dequeue_message(sid)            # pops "a"
    assert head["text"] == "a"
    sess.requeue_head(sid, head)                # restore at front
    assert [it["text"] for it in sess.get_queue(sid)["items"]] == ["a", "b"]


def test_requeue_head_bypasses_cap(app_module):
    """requeue_head restores an already-accepted item, so it ignores the cap."""
    sess = _sess(app_module)
    sid = "s-requeue-cap"
    for i in range(sess._QUEUE_MAX):
        sess.enqueue_message(sid, f"m{i}")
    restored = {"id": "q-restored", "text": "back", "image_ids": "",
                "enqueued_at": 0}
    data = sess.requeue_head(sid, restored)
    assert data["items"][0]["id"] == "q-restored"
    assert len(data["items"]) == sess._QUEUE_MAX + 1


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
    assert sess.get_queue(sid) == {"items": [], "paused": False}
    assert not sess._queue_path(sid).exists()


def test_legacy_empty_paused_queue_is_normalized_before_next_enqueue(app_module):
    """Upgrade an already-stranded queue instead of only preventing new ones."""
    sess = _sess(app_module)
    sid = "s-legacy-empty-paused"
    sess._queue_path(sid).write_text(
        '{"items": [], "paused": true}', encoding="utf-8",
    )

    assert sess.get_queue(sid) == {"items": [], "paused": False}
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
    # A queue that is empty AND un-paused leaves no file behind; a stale
    # `paused` used to block that cleanup too (sessions/ had zombie
    # {items: [], paused: true} files up to a week old).
    assert not sess._queue_path(sid).exists()
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


def test_empty_unpaused_queue_leaves_no_file(app_module):
    """_save_queue removes the file for an empty, un-paused queue so sessions/
    doesn't accumulate empty queue.json files."""
    sess = _sess(app_module)
    sid = "s-nofile"
    sess.enqueue_message(sid, "m")
    assert sess._queue_path(sid).exists()
    sess.remove_queue_item(sid, sess.get_queue(sid)["items"][0]["id"])
    assert not sess._queue_path(sid).exists()


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
    assert body["paused"] is False


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
    assert r.json() == {"ok": True, "items": [], "paused": False}
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
    response = await chat.enqueue_api(sid, chat.QueueEnqueueReq(text="late"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task = chat._queue_drain_tasks.get(sid)
    if task is not None:
        await task

    assert response["ok"] is True
    assert [(item[0], item[1]) for item in starts] == [(sid, "late")]
    assert sess.get_queue(sid)["items"] == []


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

    async def fake_start_turn(_sid, prompt, **_kwargs):
        starts.append(prompt)
        if prompt == "first":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()

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
    assert queue["paused"] is False


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
    assert sess.get_session(sid)["permission"] == "plan"


@pytest.mark.asyncio
async def test_drain_waits_until_background_reader_releases_session(
    app_module, monkeypatch,
):
    """Queued follow-ups stay queued while the previous turn's task watcher
    owns the SDK stream."""
    from backend import chat

    sess = _sess(app_module)
    sid = sess.create_session()["id"]
    sess.enqueue_message(sid, "follow-up")
    starts = []

    async def fake_start_turn(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(chat, "_start_turn", fake_start_turn)
    chat._sessions_with_inflight_tasks[sid] = {"task-1"}
    try:
        await chat._maybe_drain_queue(sid)
    finally:
        chat._sessions_with_inflight_tasks.pop(sid, None)

    assert starts == []
    assert [item["text"] for item in sess.get_queue(sid)["items"]] == [
        "follow-up"
    ]
