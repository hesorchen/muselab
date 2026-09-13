"""Detached scans merge complete watcher history without reverting it."""

import pytest


def test_snapshot_keeps_newer_modifications_deletions_and_directory_children(app_module, temp_root):
    from backend.workspace_store import WorkspaceStore, scan_workspace, compact_scan_rows
    from backend.workspaces import registry
    entry = registry.entry_for(temp_root)
    store = WorkspaceStore(temp_root)
    target = temp_root / "fixture.txt"
    target.write_text("before")
    deleted = temp_root / "removed.txt"
    deleted.write_text("old")
    store.reconcile(entry.id, temp_root, entry.name)
    before = store.current_cursor(entry.id)
    # Also discover an offline file, which must survive the rebase.
    offline = temp_root / "offline.txt"
    offline.write_text("offline")
    report = {}
    snapshot = compact_scan_rows(scan_workspace(temp_root, report=report))
    target.write_text("much newer metadata")
    deleted.unlink()
    directory = temp_root / "new-tree"
    directory.mkdir()
    (directory / "child.txt").write_text("child")
    live = store.apply_changes(entry.id, temp_root, [
        {"type": "modified", "path": target.name},
        {"type": "deleted", "path": deleted.name},
        {"type": "added", "path": directory.name},
    ])
    result = store.apply_reconcile_snapshot(entry.id, temp_root, entry.name,
        snapshot, report, expected_cursor=before)
    assert not result["resync"] and not result.get("_stale")
    assert result["cursor"] == live["cursor"] + 1
    assert result["changes"][0]["path"] == offline.name
    with store._connect() as db:
        rows = {row["path"]: dict(row) for row in store._file_rows(db, entry.id)}
    assert rows[target.name]["size"] == len("much newer metadata")
    assert deleted.name not in rows
    assert "new-tree/child.txt" in rows
    store.close()


def test_snapshot_rejects_a_missing_durable_event_interval(app_module, temp_root):
    from backend.workspace_store import WorkspaceStore, scan_workspace, compact_scan_rows
    from backend.workspaces import registry
    entry = registry.entry_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(entry.id, temp_root, entry.name)
    before = store.current_cursor(entry.id)
    report = {}
    snapshot = compact_scan_rows(scan_workspace(temp_root, report=report))
    target = temp_root / "fixture-new.txt"
    target.write_text("new")
    latest = store.apply_changes(entry.id, temp_root, [{"type": "added", "path": target.name}])
    with store._connect() as db:
        db.execute("DELETE FROM events WHERE workspace_id=?", (entry.id,))
        db.commit()
    result = store.apply_reconcile_snapshot(entry.id, temp_root, entry.name,
        snapshot, report, expected_cursor=before)
    assert result["_stale"] and result["cursor"] == latest["cursor"]
    with store._connect() as db:
        assert target.name in {row["path"] for row in store._file_rows(db, entry.id)}
    store.close()


@pytest.mark.asyncio
async def test_busy_workspace_rebases_once_including_cursor_noops(app_module, temp_root, monkeypatch):
    from backend import file_events as module
    from backend.workspace_store import WorkspaceStore, scan_workspace, compact_scan_rows
    from backend.workspaces import registry
    entry = registry.entry_for(temp_root)
    store = WorkspaceStore(temp_root)
    target = temp_root / "busy.txt"
    target.write_text("old")
    store.reconcile(entry.id, temp_root, entry.name)
    manager = module.FileWatchManager(store)
    state = module._WatchState(root=temp_root, workspace_id=entry.id,
        name=entry.name, initialized=True)
    scans = 0
    async def scan(current):
        nonlocal scans
        scans += 1
        report = {}
        snapshot = compact_scan_rows(scan_workspace(temp_root, report=report))
        target.write_text("newest")
        rows = [{"type": "modified", "path": target.name}]
        for _ in range(100):
            manager._record_native_mutation(current, rows)
            store.apply_changes(entry.id, temp_root, rows)
        return snapshot, report
    monkeypatch.setattr(manager, "_scan_workspace", scan)
    try:
        await manager._reconcile_and_broadcast(state)
        assert scans == 1 and state.reconcile_failures == 0
        with store._connect() as db:
            row = db.execute("SELECT size FROM files WHERE workspace_id=? AND path=?",
                (entry.id, target.name)).fetchone()
            assert row["size"] == len("newest")
    finally:
        await manager.shutdown()


def test_directory_modified_noop_does_not_hide_new_scan_descendants(app_module, temp_root):
    from backend.workspace_store import WorkspaceStore, scan_workspace, compact_scan_rows
    from backend.workspaces import registry
    entry = registry.entry_for(temp_root)
    store = WorkspaceStore(temp_root)
    directory = temp_root / "fixture-dir"
    directory.mkdir()
    store.reconcile(entry.id, temp_root, entry.name)
    before = store.current_cursor(entry.id)
    (directory / "offline-child.txt").write_text("discovered by scan")
    report = {}
    snapshot = compact_scan_rows(scan_workspace(temp_root, report=report))
    store.apply_changes(entry.id, temp_root, [{"type": "modified", "path": directory.name}])
    result = store.apply_reconcile_snapshot(entry.id, temp_root, entry.name,
        snapshot, report, expected_cursor=before, dirty_paths={directory.name: False})
    assert not result.get("_stale")
    with store._connect() as db:
        assert "fixture-dir/offline-child.txt" in {row["path"] for row in store._file_rows(db, entry.id)}
    store.close()
