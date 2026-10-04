"""Workspace CRUD owns real registry/index writes through cancellation."""
from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def workspace_fixture(app_module, tmp_path, monkeypatch):
    from backend import file_events, workspaces

    registry = workspaces.registry
    manager = file_events.manager
    assert registry.primary.is_relative_to(tmp_path)
    assert manager.store.path.is_relative_to(tmp_path)
    manager.store.initialize()
    # These tests exercise registry/index publication, not background scans.
    # All real workspace paths and state still belong to this app fixture.
    monkeypatch.setattr(manager, "_queue_reconcile_locked", lambda *_args, **_kwargs: None)
    other = tmp_path / "synthetic-workspace"
    other.mkdir()
    (other / "synthetic.txt").write_text("synthetic data", encoding="utf-8")
    return SimpleNamespace(api=workspaces, manager=manager, registry=registry, other=other)


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def _index_rows(fixture, workspace_id):
    with fixture.manager.store._connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,path,name FROM workspaces WHERE id = ?", (workspace_id,),
        )]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", [False, True])
async def test_cancelled_registration_then_delete_cannot_recreate_index(workspace_fixture, monkeypatch, repeated):
    fixture = workspace_fixture
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    deletion_started = asyncio.Event()
    cancellations = []
    real_register = fixture.manager.store.register_workspace
    first = True

    def delayed_register(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(3)
        try:
            return real_register(*args, **kwargs)
        finally:
            finished.set()

    async def register():
        try:
            return await fixture.api.register_workspace(
                fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic pending"),
            )
        except asyncio.CancelledError as exc:
            cancellations.append(exc.args)
            raise

    async def delete():
        deletion_started.set()
        return await fixture.api.remove_workspace(path=str(fixture.other))

    monkeypatch.setattr(fixture.manager.store, "register_workspace", delayed_register)
    request = asyncio.create_task(register())
    deletion = None
    try:
        await _until(entered.is_set)
        entry = fixture.registry.entry_for(fixture.other)
        request.cancel("synthetic registration cancelled")
        deletion = asyncio.create_task(delete())
        await deletion_started.wait()
        await asyncio.sleep(0)
        if repeated:
            request.cancel("second synthetic cancellation")
            await asyncio.sleep(0)
        request_completed_before_worker = request.done()
        deletion_completed_before_worker = False
        before_release_rows = None
        if request_completed_before_worker:
            # Preserve the concrete old failure window: DELETE completes while
            # the abandoned real registration thread is still before its write.
            assert await asyncio.wait_for(deletion, 3) == {"ok": True}
            deletion_completed_before_worker = True
            assert not finished.is_set()
            assert not fixture.registry.contains(fixture.other)
            before_release_rows = len(_index_rows(fixture, entry.id))
        else:
            assert fixture.manager._lifecycle_locks[fixture.other.resolve()].locked()
            assert not deletion.done()
            assert fixture.registry.contains(fixture.other)
        release.set()
        await _until(finished.is_set)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 3)
        assert await asyncio.wait_for(deletion, 3) == {"ok": True}
        rows = _index_rows(fixture, entry.id)
        print(json.dumps({
            "repeated_cancel": repeated,
            "request_completed_before_worker": request_completed_before_worker,
            "delete_completed_before_worker": deletion_completed_before_worker,
            "index_rows_before_release": before_release_rows,
            "index_rows_after_worker": len(rows),
            "real_worker_finished": finished.is_set(),
        }))
        assert rows == []
        assert not request_completed_before_worker
        assert cancellations == [("synthetic registration cancelled",)]
        assert not fixture.registry.contains(fixture.other)
        assert fixture.other.exists()  # Unregister never deletes user files.
        assert not fixture.manager._states
    finally:
        release.set()
        await asyncio.gather(request, *([deletion] if deletion else []), return_exceptions=True)
        await _until(finished.is_set)


@pytest.mark.asyncio
async def test_workspace_rename_order_and_delete_keep_saved_index_consistent(workspace_fixture):
    fixture = workspace_fixture
    entry = await fixture.api.register_workspace(
        fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic original"),
    )
    renamed = await fixture.api.register_workspace(
        fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic renamed"),
    )
    assert renamed["id"] == entry["id"]
    assert _index_rows(fixture, entry["id"])[0]["name"] == "synthetic renamed"
    primary = str(fixture.registry.primary)
    order = [str(fixture.other.resolve()), primary]
    reordered = await asyncio.to_thread(
        fixture.api.reorder_workspaces, fixture.api.WorkspaceOrderRequest(paths=order),
    )
    assert [row["path"] for row in reordered["workspaces"]] == order
    assert fixture.registry.resolve(None) == Path(primary)
    reloaded = fixture.api.WorkspaceRegistry(fixture.registry.primary)
    assert reloaded.list() == fixture.registry.list()
    assert [row.primary for row in reloaded.list()] == [False, True]
    assert await fixture.api.remove_workspace(path=str(fixture.other)) == {"ok": True}
    assert _index_rows(fixture, entry["id"]) == []
    assert [row.path for row in fixture.api.WorkspaceRegistry(fixture.registry.primary).list()] == [primary]
    assert (fixture.other / "synthetic.txt").read_text(encoding="utf-8") == "synthetic data"


@pytest.mark.asyncio
async def test_failed_workspace_rename_preserves_saved_and_live_names(workspace_fixture, monkeypatch):
    fixture = workspace_fixture
    entry = await fixture.api.register_workspace(
        fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic original"),
    )
    before = fixture.registry._path.read_bytes()
    real_write = fixture.api.atomic_write_text

    def fail_write(path, *args, **kwargs):
        if path == fixture.registry._path:
            raise OSError(errno.EIO, "synthetic registry write failure")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(fixture.api, "atomic_write_text", fail_write)
    with pytest.raises(OSError, match="synthetic registry write failure"):
        await fixture.api.register_workspace(
            fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic rejected"),
        )
    assert fixture.registry._path.read_bytes() == before
    assert fixture.registry.entry_for(fixture.other).name == "synthetic original"
    assert _index_rows(fixture, entry["id"])[0]["name"] == "synthetic original"
    assert fixture.api.WorkspaceRegistry(fixture.registry.primary).list() == fixture.registry.list()
    monkeypatch.setattr(fixture.api, "atomic_write_text", real_write)
    retried = await fixture.api.register_workspace(
        fixture.api.WorkspaceRequest(path=str(fixture.other), name="synthetic retried"),
    )
    assert retried["id"] == entry["id"]
    assert _index_rows(fixture, entry["id"])[0]["name"] == "synthetic retried"
