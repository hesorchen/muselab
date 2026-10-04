"""Pin writes admitted before DELETE must not restore its removed index row."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def pin_session(app_module, monkeypatch, tmp_path):
    from backend import chat

    sess = chat.sess
    row = sess.create_session(name="pin lifecycle fixture")
    sid = row["id"]
    sdk_file = tmp_path / "synthetic-sdk" / f"{sid}.jsonl"
    sdk_file.parent.mkdir()
    sdk_file.write_text('{"type":"user","message":{"content":"fixture"}}\n', encoding="utf-8")
    # Every SDK boundary is synthetic; the actual local files/index are real.
    def sdk_info():
        return SimpleNamespace(
            session_id=sid, custom_title=None, first_prompt="fixture",
            created_at=1_000, last_modified=2_000, tag=None,
        ) if sdk_file.exists() else None

    monkeypatch.setattr(sess, "sdk_get_session_info", lambda target_sid, **_kwargs:
                        sdk_info() if target_sid == sid else None)
    monkeypatch.setattr(sess, "sdk_list_sessions", lambda **_kwargs:
                        [sdk_info()] if sdk_file.exists() else [])

    def delete_sdk(target_sid, *, directory):
        assert target_sid == sid
        assert directory == str(sess.ROOT)
        sdk_file.unlink()

    monkeypatch.setattr(chat, "sdk_delete_session", delete_sdk)
    sess._save_sidecar(sid, {"messages": {}})
    return chat, sid, sdk_file


def _indexed_ids(sess):
    return {row["id"] for row in json.loads(sess.INDEX.read_text(encoding="utf-8"))}


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_late_pin_worker_cannot_restore_deleted_session(
    app_module, auth, pin_session, monkeypatch, pinned,
):
    chat, sid, sdk_file = pin_session
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    release = threading.Event()
    real_set_pin = chat.sess.set_pin

    def delayed_pin(target_sid, value):
        loop.call_soon_threadsafe(entered.set_result, True)
        assert release.wait(10), "pin worker was not released"
        return real_set_pin(target_sid, value)

    monkeypatch.setattr(chat.sess, "set_pin", delayed_pin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://audit",
    ) as client:
        patch = asyncio.create_task(client.patch(
            f"/api/chat/sessions/{sid}", headers=auth, json={"pinned": pinned},
        ))
        try:
            await entered
            deleted = await client.delete(f"/api/chat/sessions/{sid}", headers=auth)
            assert deleted.status_code == 200
            assert sid not in _indexed_ids(chat.sess)
            assert not sdk_file.exists()
            assert not chat.sess._sidecar_path(sid).exists()
            release.set()
            response = await patch
            assert (response.status_code, sid in _indexed_ids(chat.sess)) == (404, False)
            assert not chat.sess._sidecar_path(sid).exists()
            # A fresh process loses tombstones: the durable row must remain absent.
            chat.sess._DELETED_SESSION_IDS.clear()
            chat.sess.invalidate_sessions_cache()
            assert sid not in {row["id"] for row in chat.sess.list_sessions()}
        finally:
            release.set()
            await asyncio.gather(patch, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_pin_without_local_row_retains_sdk_only_stub_contract(
    app_module, auth, pin_session, pinned,
):
    chat, sid, sdk_file = pin_session
    chat.sess._save_index([])
    assert sdk_file.exists()
    assert chat.sess.get_session_meta(sid)["id"] == sid
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://audit",
    ) as client:
        response = await client.patch(
            f"/api/chat/sessions/{sid}", headers=auth, json={"pinned": pinned},
        )
    assert response.status_code == 200
    rows = json.loads(chat.sess.INDEX.read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["id"] == sid
    assert rows[0]["pinned"] is pinned
    assert chat.sess.set_pin(sid, not pinned) is (not pinned)
    assert chat.sess.set_pin(sid, pinned) is pinned


@pytest.mark.asyncio
async def test_delete_waits_for_admitted_pin_disk_write(
    app_module, auth, pin_session, monkeypatch,
):
    chat, sid, sdk_file = pin_session
    loop = asyncio.get_running_loop()
    entered = loop.create_future()
    deleting = loop.create_future()
    release = threading.Event()
    real_save_index = chat.sess._save_index
    real_lineage = chat.sess.runtime_lineage

    def paused_save(rows):
        if not entered.done() and any(row.get("pinned") for row in rows):
            loop.call_soon_threadsafe(entered.set_result, True)
            assert release.wait(10), "pin index write was not released"
        return real_save_index(rows)

    def read_lineage(target_sid):
        if not deleting.done():
            loop.call_soon_threadsafe(deleting.set_result, True)
        return real_lineage(target_sid)

    monkeypatch.setattr(chat.sess, "_save_index", paused_save)
    # DELETE first reads the lineage under the index lock. Observe that
    # actual admission attempt rather than a later fence it cannot reach yet.
    monkeypatch.setattr(chat.sess, "runtime_lineage", read_lineage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://audit",
    ) as client:
        patch = asyncio.create_task(client.patch(
            f"/api/chat/sessions/{sid}", headers=auth, json={"pinned": True},
        ))
        delete = None
        try:
            await entered
            delete = asyncio.create_task(client.delete(
                f"/api/chat/sessions/{sid}", headers=auth,
            ))
            await deleting
            assert not delete.done()
            release.set()
            assert (await patch).status_code == 200
            assert (await delete).status_code == 200
            assert sid not in _indexed_ids(chat.sess)
            assert not sdk_file.exists()
            assert not chat.sess._sidecar_path(sid).exists()
        finally:
            release.set()
            await asyncio.gather(patch, *([delete] if delete else []), return_exceptions=True)
