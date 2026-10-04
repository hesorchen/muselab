"""Native subtree updates retain unrelated and unobserved indexed files."""

from pathlib import Path
import shutil

import pytest


@pytest.mark.parametrize("mutation", ["delete", "replace_directory", "replace_file"])
@pytest.mark.parametrize("name", ["Foo", "Foo_%[x]\\part"])
def test_native_subtree_change_preserves_case_distinct_sibling(
    app_module, temp_root, mutation, name,
):
    from backend.workspace_store import WorkspaceStore

    changed = temp_root / name
    sibling = temp_root / name.lower()
    changed.mkdir()
    sibling.mkdir()
    (changed / "old.txt").write_text("old", encoding="utf-8")
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    store = WorkspaceStore(temp_root)
    store.reconcile("test", temp_root, "test", primary=True)
    cursor = store.current_cursor("test")

    shutil.rmtree(changed)
    if mutation == "replace_directory":
        changed.mkdir()
        (changed / "new.txt").write_text("new", encoding="utf-8")
    elif mutation == "replace_file":
        changed.write_text("replacement", encoding="utf-8")
    payload = store.apply_changes("test", temp_root, [{
        "type": "deleted" if mutation == "delete" else "added",
        "path": name,
    }])

    expected = f"{sibling.name}/keep.txt"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert expected in {row["path"] for row in store.bootstrap("test")["entries"]}
    assert expected not in {row["path"] for row in payload["changes"]}
    assert expected not in {row["path"] for row in store.delta("test", cursor)["changes"]}
    assert f"{name}/old.txt" not in {
        row["path"] for row in store.bootstrap("test")["entries"]
    }


@pytest.mark.parametrize("existing", [False, True])
def test_budgeted_native_subtree_scan_preserves_unobserved_files_and_reconciles(
    app_module, temp_root, monkeypatch, existing,
):
    from backend import workspace_store

    incoming = temp_root / "incoming"
    if existing:
        incoming.mkdir()
        for index in range(3):
            (incoming / f"item-{index}.txt").write_text("old", encoding="utf-8")
    store = workspace_store.WorkspaceStore(temp_root)
    store.reconcile("test", temp_root, "test", primary=True)
    if not existing:
        incoming.mkdir()
    for index in range(3):
        (incoming / f"item-{index}.txt").write_text("new", encoding="utf-8")

    real_scan = workspace_store.scan_workspace

    def budgeted_scan(root: Path, **kwargs):
        return real_scan(root, max_files=1, max_seconds=None, **kwargs)

    monkeypatch.setattr(workspace_store, "scan_workspace", budgeted_scan)
    payload = store.apply_changes("test", temp_root, [{"type": "added", "path": "incoming"}])

    assert payload.get("_reconcile") is True
    assert not any(row["type"] == "deleted" for row in payload["changes"])
    paths = {row["path"] for row in store.bootstrap("test")["entries"]}
    if existing:
        assert {f"incoming/item-{index}.txt" for index in range(3)} <= paths
    else:
        assert any(path.startswith("incoming/") for path in paths)

    # The scheduled full reconciliation can establish all files afterwards.
    monkeypatch.setattr(workspace_store, "scan_workspace", real_scan)
    store.reconcile("test", temp_root, "test", primary=True)
    assert {f"incoming/item-{index}.txt" for index in range(3)} <= {
        row["path"] for row in store.bootstrap("test")["entries"]
    }


def test_native_subtree_delete_avoids_scanning_unrelated_index_entries(
    app_module, temp_root, monkeypatch,
):
    from backend.workspace_store import WorkspaceStore, _entry

    store = WorkspaceStore(temp_root)
    info = (temp_root / "README.md").stat()
    rows = [_entry(f"unrelated-{index:05d}/item.txt", False, info) for index in range(10_000)]
    rows.extend([_entry("removed", True, info), _entry("removed/child.txt", False, info)])
    store.reconcile("test", temp_root, "test", primary=True, snapshot=rows)
    original_connect = store._connect
    operations = 0

    def progress():
        nonlocal operations
        operations += 100
        return 0

    def counted_connect():
        connection = original_connect()
        connection.set_progress_handler(progress, 100)
        return connection

    monkeypatch.setattr(store, "_connect", counted_connect)
    payload = store.apply_changes("test", temp_root, [{"type": "deleted", "path": "removed"}])
    assert {row["path"] for row in payload["changes"]} == {"removed", "removed/child.txt"}
    # A two-entry delete must stay bounded as unrelated index rows grow.
    # The former LIKE/OR queries scan every unrelated entry.
    assert operations < 5_000


def test_native_fifo_notification_does_not_block_index_worker(
    app_module, temp_root,
):
    import os
    import threading

    from backend.workspace_store import WorkspaceStore

    fifo = temp_root / "notification.txt"
    os.mkfifo(fifo)
    store = WorkspaceStore(temp_root)
    store.reconcile("test", temp_root, "test", primary=True)
    errors = []

    def apply_notification():
        try:
            store.apply_changes("test", temp_root, [{"type": "modified", "path": fifo.name}])
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=apply_notification, daemon=True)
    worker.start()
    worker.join(timeout=0.5)
    finished = not worker.is_alive()
    if not finished:
        # Unblock the old implementation without leaving a daemon/thread lock.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(timeout=2)
    assert finished, "native watcher blocked on a named pipe"
    assert not errors
