"""Crash, restart, concurrency, and privacy tests for durable chat uploads."""

from __future__ import annotations

import asyncio
import base64
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.attachment_queue_store import DurableAttachmentStore
from backend.private_storage import write_private_bytes


def _image(payload: bytes = b"image-bytes") -> dict:
    return {
        "kind": "image",
        "mime": "image/png",
        "name": "private.png",
        "b64": base64.b64encode(payload).decode("ascii"),
        "ts": time.time(),
    }


def _stage(
    store: DurableAttachmentStore,
    aid: str,
    entry: dict | None = None,
    *,
    ttl: float = 600,
    max_bytes: int = 1024 * 1024,
) -> None:
    published, _evicted = store.stage_batch(
        [(aid, entry or _image())],
        ttl=ttl,
        max_entries=16,
        max_bytes=max_bytes,
    )
    assert published is True


def test_restart_preserves_submitted_queue_ref_until_exact_ack(tmp_path):
    aid = "restart-attachment"
    sid = "session-a"
    item_id = "q-restart-item"
    payload = b"restart-safe-payload"
    store = DurableAttachmentStore(tmp_path)
    _stage(store, aid, _image(payload))
    assert not store.bind_queue_item(
        sid, item_id, [aid], ttl=600
    ).missing

    lease = store.acquire(
        [aid],
        "lease-before-restart",
        lease_seconds=300,
        queue_owner=(sid, item_id),
    )
    assert base64.b64decode(lease.entries[aid]["b64"]) == payload
    assert store.commit(
        "lease-before-restart",
        queue_session_id=sid,
        queue_item_id=item_id,
    )

    restarted = DurableAttachmentStore(tmp_path)
    assert restarted.reconcile_queue_refs({item_id: sid}, ttl=600) == {
        "released": 0,
        "moved": 0,
        "deleted": 0,
    }
    retry = restarted.acquire(
        [aid],
        "lease-after-restart",
        lease_seconds=300,
        queue_owner=(sid, item_id),
    )
    assert base64.b64decode(retry.entries[aid]["b64"]) == payload
    assert restarted.release("lease-after-restart", ttl=600)

    # acquire/mark/release must never downgrade submitted to claimed/queued.
    assert restarted.finish_queue_item(sid, item_id, consume=False) == (aid,)
    assert restarted.load_entry(aid) is None


def test_queue_ref_pins_past_ttl_then_cancel_releases_to_gc(tmp_path):
    aid = "pinned-attachment"
    store = DurableAttachmentStore(tmp_path)
    _stage(store, aid, ttl=60)
    assert not store.bind_queue_item(
        "session-a", "q-pinned-item", [aid], ttl=0
    ).missing
    assert store.gc(now=time.time() + 3600) == ()
    assert store.metadata(aid) is not None

    assert store.finish_queue_item(
        "session-a", "q-pinned-item", consume=False, ttl=1
    ) == ()
    assert store.gc(now=time.time() + 2) == (aid,)


def test_same_item_id_is_scoped_by_session_for_finish_and_migrate(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    _stage(store, "scoped-attachment-a", _image(b"a"))
    _stage(store, "scoped-attachment-b", _image(b"b"))
    shared_item = "q-shared-item"
    assert not store.bind_queue_item(
        "session-a", shared_item, ["scoped-attachment-a"], ttl=600
    ).busy
    assert not store.bind_queue_item(
        "session-b", shared_item, ["scoped-attachment-b"], ttl=600
    ).busy

    assert store.finish_queue_item(
        "session-a", shared_item, consume=True
    ) == ("scoped-attachment-a",)
    assert store.metadata("scoped-attachment-b") is not None

    store.migrate_queue_items(
        "session-a", [shared_item], "wrong-target"
    )
    still_owned = store.acquire(
        ["scoped-attachment-b"],
        "session-b-lease",
        lease_seconds=60,
        queue_owner=("session-b", shared_item),
    )
    assert not still_owned.missing
    assert store.release("session-b-lease", ttl=600)

    store.migrate_queue_items(
        "session-b", [shared_item], "session-c"
    )
    moved = store.acquire(
        ["scoped-attachment-b"],
        "session-c-lease",
        lease_seconds=60,
        queue_owner=("session-c", shared_item),
    )
    assert not moved.missing
    assert store.release("session-c-lease", ttl=600)


def test_two_store_instances_cannot_bind_one_blob_to_two_items(tmp_path):
    first = DurableAttachmentStore(tmp_path)
    second = DurableAttachmentStore(tmp_path)
    aid = "concurrent-attachment"
    _stage(first, aid)
    barrier = threading.Barrier(2)

    def bind(store, sid, item_id):
        barrier.wait(timeout=2)
        return store.bind_queue_item(sid, item_id, [aid], ttl=600)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: bind(*args),
            [
                (first, "session-a", "q-concurrent-a"),
                (second, "session-b", "q-concurrent-b"),
            ],
        ))
    assert sum(not result.busy and not result.missing for result in results) == 1
    assert sum(result.busy == (aid,) for result in results) == 1


def test_startup_removes_orphan_final_temp_and_missing_metadata(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    aid = "missing-attachment"
    _stage(store, aid)
    store._blob_path(aid).unlink()
    orphan = store.blobs / "orphan-attachment.blob"
    temp = store.blobs / ".orphan-attachment.blob.deadbeef.tmp"
    write_private_bytes(orphan, b"orphan")
    write_private_bytes(temp, b"partial")

    restarted = DurableAttachmentStore(tmp_path)
    assert restarted.metadata(aid) is None
    assert not orphan.exists()
    assert not temp.exists()


def test_same_size_tamper_and_symlink_are_never_returned(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    _stage(store, "tamper-attachment", _image(b"abc"))
    write_private_bytes(store._blob_path("tamper-attachment"), b"xyz")
    assert store.metadata("tamper-attachment") is None
    restarted = DurableAttachmentStore(tmp_path)
    assert restarted.load_entry("tamper-attachment") is None

    _stage(restarted, "symlink-attachment", _image(b"private"))
    secret = tmp_path / "outside-secret"
    secret.write_bytes(b"do-not-read")
    blob = restarted._blob_path("symlink-attachment")
    blob.unlink()
    blob.symlink_to(secret)
    assert restarted.load_entry("symlink-attachment") is None
    recovered = DurableAttachmentStore(tmp_path)
    assert recovered.metadata("symlink-attachment") is None
    assert secret.read_bytes() == b"do-not-read"


def test_transcription_is_private_counted_blob_not_sqlite_plaintext(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    marker = "PRIVATE_TRANSCRIPTION_MARKER"
    workbook = {
        "kind": "xlsx",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "name": "private.xlsx",
        "raw": b"zip-workbook",
        "text": marker,
    }
    _stage(store, "xlsx-attachment", workbook)
    loaded = store.load_entry("xlsx-attachment")
    assert loaded is not None and loaded["text"] == marker
    assert store._blob_path("xlsx-attachment", "text").read_text() == marker
    empty_workbook = {**workbook, "text": ""}
    _stage(store, "xlsx-empty-text", empty_workbook)
    empty_loaded = store.load_entry("xlsx-empty-text")
    assert empty_loaded is not None and empty_loaded["text"] == ""

    with store._connect() as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(attachments)")
        }
    assert "text_payload" not in columns
    for path in store.base.glob("registry.sqlite3*"):
        assert marker.encode() not in path.read_bytes()

    too_small = DurableAttachmentStore(tmp_path / "small")
    published, _ = too_small.stage_batch(
        [("xlsx-too-large", workbook)],
        ttl=600,
        max_entries=1,
        max_bytes=len(workbook["raw"]),
    )
    assert published is False
    assert too_small.metadata("xlsx-too-large") is None


def test_private_modes_and_internal_ignored_path(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    _stage(store, "mode-attachment")
    assert store.base == tmp_path / ".muselab" / "staged-attachments"
    for path in (store.internal, store.base, store.blobs):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (
        store.path,
        store.lock_path,
        store._blob_path("mode-attachment"),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    ignore = Path(__file__).resolve().parents[1] / ".gitignore"
    assert ".muselab/staged-attachments/" in ignore.read_text()


def test_active_expired_lease_is_protected_from_runtime_gc(tmp_path):
    store = DurableAttachmentStore(tmp_path)
    aid = "protected-attachment"
    _stage(store, aid)
    lease = store.acquire(
        [aid], "active-token", lease_seconds=1
    )
    assert aid in lease.entries
    assert store.gc(
        now=time.time() + 3600,
        protected_tokens={"active-token"},
    ) == ()
    assert store.commit("active-token") is True
    assert store.load_entry(aid) is None


def test_hot_cache_lease_does_not_read_blob(app_module, monkeypatch):
    from backend import chat

    aid = "cache-hit-attachment"
    assert chat._put_staged_attachment(aid, _image(b"hot-cache"))
    reads = 0
    original = chat._durable_attachment_store._read_blob

    def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(chat._durable_attachment_store, "_read_blob", counted)
    lease, missing, busy = chat._lease_staged_attachments(
        aid, require_all=True
    )
    assert lease is not None
    assert missing == [] and busy == []
    assert reads == 0
    assert chat._release_staged_attachment_lease(lease)


def test_internal_store_is_absent_from_file_surfaces(
    app_module,
    client,
    auth,
    temp_root,
):
    from backend import chat
    from backend.file_events import Change, _watch_filter_for

    aid = "hidden-attachment"
    marker = b"UNSEARCHABLE_PRIVATE_ATTACHMENT"
    assert chat._put_staged_attachment(aid, _image(marker))

    bootstrap = client.get("/api/files/bootstrap", headers=auth)
    assert bootstrap.status_code == 200
    assert aid not in bootstrap.text
    search = client.get(f"/api/files/search?q={aid}", headers=auth)
    assert search.status_code == 200
    assert search.json()["entries"] == []
    grep = client.get(
        "/api/files/grep?q=UNSEARCHABLE_PRIVATE_ATTACHMENT", headers=auth
    )
    assert grep.status_code == 200
    assert grep.json()["hits"] == []
    read = client.get(
        "/api/files/read",
        headers=auth,
        params={
            "path": f".muselab/staged-attachments/blobs/{aid}.blob"
        },
    )
    assert read.status_code == 403
    include = _watch_filter_for(temp_root)
    assert include(
        Change.modified,
        str(temp_root / ".muselab" / "staged-attachments" / "registry.sqlite3"),
    ) is False


def test_queue_receipts_linearize_with_claim(app_module):
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    waiting = sess.enqueue_message(sid, "claim wins")["item"]
    assert sess.claim_queue_message(sid)["id"] == waiting["id"]
    queue, removed = sess.clear_queue_with_removed(sid)
    assert removed == ()
    assert queue["inflight"]["item"]["id"] == waiting["id"]
    assert sess.release_queue_claim(sid, waiting["id"])
    sess.clear_queue(sid)

    first = sess.enqueue_message(sid, "clear wins one")["item"]
    second = sess.enqueue_message(sid, "clear wins two")["item"]
    queue, removed = sess.clear_queue_with_removed(sid)
    assert removed == (first["id"], second["id"])
    assert queue["inflight"] is None
    assert sess.claim_queue_message(sid) is None


@pytest.mark.asyncio
async def test_mark_queue_turn_failure_restores_exact_claim(
    app_module,
    monkeypatch,
):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    queued = sess.enqueue_message(sid, "mark fails")["item"]
    claimed = sess.claim_queue_message(sid)
    assert claimed is not None

    def fail_mark(*_args, **_kwargs):
        raise OSError("synthetic sqlite failure")

    monkeypatch.setattr(
        chat._durable_attachment_store, "mark_queue_turn", fail_mark
    )
    with pytest.raises(chat._TurnStartError) as raised:
        await chat._start_turn(
            sid,
            queued["text"],
            queue_item_id=queued["id"],
            persist_permission=False,
        )
    assert raised.value.queue_claim_settled is True
    queue = sess.get_queue(sid)
    assert queue["inflight"] is None
    assert queue["paused"] is False
    assert queue["items"][0]["queue_issue"] == "failed"
    assert [item["id"] for item in queue["items"]] == [queued["id"]]
    assert sid not in chat._active_turns


@pytest.mark.asyncio
async def test_invalid_persisted_attachment_restores_and_pauses_claim(
    app_module,
    monkeypatch,
):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    queued = sess.enqueue_message(
        sid, "invalid", image_ids="../../private"
    )["item"]
    started = False

    async def should_not_start(*_args, **_kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr(chat, "_start_turn", should_not_start)
    await chat._maybe_drain_queue(sid)
    queue = sess.get_queue(sid)
    assert started is False
    assert queue["inflight"] is None
    assert queue["paused"] is False
    assert queue["items"][0]["queue_issue"] == "attachment_unavailable"
    assert [item["id"] for item in queue["items"]] == [queued["id"]]


@pytest.mark.asyncio
async def test_enqueue_io_error_after_queue_commit_retains_durable_owner(
    app_module,
    monkeypatch,
):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session()["id"]
    aid = "ambiguous-attachment"
    assert chat._put_staged_attachment(aid, _image(b"ambiguous"))
    original = sess.enqueue_existing_message

    def commit_then_raise(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result["ok"] is True
        raise OSError("synthetic post-rename error")

    scheduled: list[str] = []
    monkeypatch.setattr(sess, "enqueue_existing_message", commit_then_raise)

    def capture_drain(queued_sid: str) -> None:
        scheduled.append(queued_sid)

    monkeypatch.setattr(
        chat, "_schedule_queue_drain", capture_drain
    )
    with pytest.raises(OSError):
        await chat.enqueue_api(
            sid,
            chat.QueueEnqueueReq(text="queued", image_ids=aid),
            chat.BackgroundTasks(),
        )

    queue = sess.get_queue(sid)
    assert len(queue["items"]) == 1
    item_id = queue["items"][0]["id"]
    assert scheduled == [sid]
    lease = chat._durable_attachment_store.acquire(
        [aid],
        "ambiguous-owner-lease",
        lease_seconds=60,
        queue_owner=(sid, item_id),
    )
    assert aid in lease.entries
    assert chat._durable_attachment_store.release(
        "ambiguous-owner-lease", ttl=600
    )


@pytest.mark.asyncio
async def test_restart_blob_load_does_not_block_event_loop(
    app_module,
    monkeypatch,
):
    from backend import chat
    from backend import sessions as sess

    sid = sess.create_session(model="claude-sonnet-4-6")["id"]
    aid = "restart-large-attachment"
    entry = {
        "kind": "text",
        "mime": "text/plain",
        "name": "large.txt",
        "raw": b"x" * (2 * 1024 * 1024),
        "text": "x" * (2 * 1024 * 1024),
        "ts": time.time(),
    }
    assert chat._put_staged_attachment(aid, entry)
    with chat._image_store_lock:
        chat._image_store.clear()

    read_windows: list[list[float]] = []
    original_read = chat._durable_attachment_store._read_blob

    def slow_read(*args, **kwargs):
        window = [time.monotonic(), 0.0]
        read_windows.append(window)
        time.sleep(0.08)
        result = original_read(*args, **kwargs)
        window[1] = time.monotonic()
        return result

    class EmptyClient:
        async def get_context_usage(self):
            return {"maxTokens": 200_000, "totalTokens": 1}

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            if False:
                yield None

    async def fake_get_client(*_args, **_kwargs):
        return EmptyClient()

    monkeypatch.setattr(chat._durable_attachment_store, "_read_blob", slow_read)
    monkeypatch.setattr(chat, "get_client", fake_get_client)
    tick_times: list[float] = []
    ticking = True

    async def ticker() -> None:
        while ticking:
            tick_times.append(time.monotonic())
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    try:
        broadcast = await chat._start_turn(
            sid,
            "inspect",
            model="claude-sonnet-4-6",
            image_ids=aid,
        )
        assert read_windows
        first_start = min(window[0] for window in read_windows)
        last_end = max(window[1] for window in read_windows)
        assert sum(first_start < tick < last_end for tick in tick_times) >= 5
        deadline = time.monotonic() + 2
        while not broadcast.done and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert broadcast.done is True
    finally:
        ticking = False
        await ticker_task
