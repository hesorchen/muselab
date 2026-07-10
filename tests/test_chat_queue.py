"""Tests for the server-side message queue.

Two levels:
  - state-machine unit tests on the sessions-layer queue helpers
    (enqueue / dequeue / requeue_head / reorder / set_queue_paused /
    remove_queue_item / clear_queue) — these are pure file-backed CRUD.
  - endpoint round-trips against /api/chat/sessions/{sid}/queue
    (GET / POST / DELETE / pause / reorder).

The drain dispatch (_maybe_drain_queue → _start_turn) needs the Claude SDK
+ a live model, so it isn't unit-tested here — we only cover the queue STATE
it reads/writes (mirrors how test_scheduler.py leaves _execute_task out).
"""
from __future__ import annotations


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


def test_pause_empty_queue_persists_flag(app_module):
    """Pausing an empty queue still records paused=True (the file is written
    because paused is truthy even with no items)."""
    sess = _sess(app_module)
    sid = "s-pause-empty"
    sess.set_queue_paused(sid, True)
    assert sess.get_queue(sid)["paused"] is True


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


def test_queue_endpoint_enqueue_and_get(client, auth):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "hello"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.get(f"/api/chat/sessions/{sid}/queue", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert [it["text"] for it in body["items"]] == ["hello"]
    assert body["paused"] is False


def test_queue_endpoint_rejects_empty_message(client, auth):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "   "})
    assert r.status_code == 400


def test_queue_endpoint_full_returns_409(client, auth):
    sid = _mint_session(client, auth)
    from backend import sessions as sess
    for i in range(sess._QUEUE_MAX):
        ok = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                         json={"text": f"m{i}"})
        assert ok.status_code == 200
    over = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                       json={"text": "overflow"})
    assert over.status_code == 409


def test_queue_endpoint_reorder_roundtrip(client, auth):
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


def test_queue_endpoint_remove_item(client, auth):
    sid = _mint_session(client, auth)
    r = client.post(f"/api/chat/sessions/{sid}/queue", headers=auth,
                    json={"text": "doomed"})
    item_id = r.json()["item"]["id"]
    r = client.delete(f"/api/chat/sessions/{sid}/queue/{item_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_queue_endpoint_clear(client, auth):
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


def test_queue_endpoint_pause_toggle(client, auth, monkeypatch):
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
