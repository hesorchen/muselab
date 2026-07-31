"""Workspace index snapshot and concurrency regressions."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_compact_bootstrap_reads_only_root_and_expanded_children(
    app_module,
    temp_root: Path,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    (temp_root / "root.txt").write_text("root", encoding="utf-8")
    (temp_root / "docs" / "nested").mkdir(parents=True)
    (temp_root / "docs" / "direct.txt").write_text("direct", encoding="utf-8")
    (temp_root / "docs" / "nested" / "deep.txt").write_text(
        "deep",
        encoding="utf-8",
    )
    (temp_root / "other").mkdir()
    (temp_root / "other" / "omitted.txt").write_text(
        "omitted",
        encoding="utf-8",
    )
    (temp_root / ".hidden-dir").mkdir()
    (temp_root / ".hidden-dir" / "hidden.txt").write_text(
        "hidden",
        encoding="utf-8",
    )

    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)

    root_only = store.bootstrap(workspace_id, parents=[])
    assert root_only["partial"] is True
    assert root_only["parents"] == []
    assert {row["path"] for row in root_only["entries"]} == {
        ".env",
        ".hidden-dir",
        ".secret",
        "README.md",
        "docs",
        "notes",
        "other",
        "root.txt",
    }

    expanded = store.bootstrap(
        workspace_id,
        parents=["docs/nested"],
        show_hidden=False,
    )
    assert expanded["parents"] == ["docs", "docs/nested"]
    assert {row["path"] for row in expanded["entries"]} == {
        "docs",
        "docs/direct.txt",
        "docs/nested",
        "docs/nested/deep.txt",
        "README.md",
        "notes",
        "other",
        "root.txt",
    }
    assert "other/omitted.txt" not in {
        row["path"] for row in expanded["entries"]
    }

    complete = store.bootstrap(workspace_id)
    assert "partial" not in complete
    assert "other/omitted.txt" in {
        row["path"] for row in complete["entries"]
    }


def test_bootstrap_cursor_and_rows_share_one_read_snapshot(
    app_module,
    temp_root: Path,
    monkeypatch,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    target = temp_root / "tracked.txt"
    target.write_text("before", encoding="utf-8")
    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    baseline_cursor = store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
    )
    baseline_mtime = next(
        row["mtime_ns"]
        for row in store.bootstrap(workspace_id)["entries"]
        if row["path"] == "tracked.txt"
    )

    read_started = threading.Event()
    allow_read = threading.Event()
    original = store._file_rows_for_parents

    def paused_rows(db, selected_workspace_id, parents, **kwargs):
        read_started.set()
        assert allow_read.wait(timeout=2)
        return original(db, selected_workspace_id, parents, **kwargs)

    monkeypatch.setattr(store, "_file_rows_for_parents", paused_rows)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            store.bootstrap,
            workspace_id,
            parents=[],
        )
        assert read_started.wait(timeout=2)

        target.write_text("after", encoding="utf-8")
        newer_ns = baseline_mtime + 2_000_000_000
        os.utime(target, ns=(newer_ns, newer_ns))
        update = store.apply_changes(
            workspace_id,
            temp_root,
            [{"type": "modified", "path": "tracked.txt"}],
        )
        assert update["cursor"] == baseline_cursor + 1
        allow_read.set()
        snapshot = pending.result(timeout=2)

    assert snapshot["cursor"] == baseline_cursor
    assert next(
        row["mtime_ns"]
        for row in snapshot["entries"]
        if row["path"] == "tracked.txt"
    ) == baseline_mtime
    current = store.bootstrap(workspace_id, parents=[])
    assert current["cursor"] == baseline_cursor + 1
    assert next(
        row["mtime_ns"]
        for row in current["entries"]
        if row["path"] == "tracked.txt"
    ) == newer_ns


def test_reconcile_write_phase_does_not_block_other_workspace_reads(
    app_module,
    temp_root: Path,
    monkeypatch,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    other_root = temp_root.parent / "other-workspace"
    other_root.mkdir()
    (other_root / "ready.txt").write_text("ready", encoding="utf-8")
    first_id = registry.id_for(temp_root)
    other_id = registry.register(other_root, "other").id
    store = WorkspaceStore(temp_root)
    store.reconcile(first_id, temp_root, "first", primary=True)
    store.reconcile(other_id, other_root, "other")

    transaction_started = threading.Event()
    finish_transaction = threading.Event()
    original = store._file_rows

    def paused_first_workspace(db, selected_workspace_id, **kwargs):
        if selected_workspace_id == first_id:
            transaction_started.set()
            assert finish_transaction.wait(timeout=2)
        return original(db, selected_workspace_id, **kwargs)

    monkeypatch.setattr(store, "_file_rows", paused_first_workspace)
    (temp_root / "new.txt").write_text("new", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            store.reconcile,
            first_id,
            temp_root,
            "first",
            primary=True,
        )
        assert transaction_started.wait(timeout=2)
        other = store.bootstrap(other_id, parents=[])
        assert {row["path"] for row in other["entries"]} == {"ready.txt"}
        finish_transaction.set()
        pending.result(timeout=2)
