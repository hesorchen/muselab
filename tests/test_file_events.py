"""Shared filesystem watcher and SSE endpoint regressions."""

import asyncio
import os
import stat
import threading
from pathlib import Path

import pytest
from watchfiles import Change


def test_file_events_require_short_lived_ticket(client):
    response = client.get("/api/files/events")
    assert response.status_code == 401


def test_file_event_ticket_is_workspace_bound_and_single_use(
    client, auth, app_module, temp_root,
):
    from backend.capability_tickets import tickets

    minted = client.post("/api/files/events-ticket", headers=auth)
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    scope = (str(temp_root.resolve()),)
    assert tickets.validate(ticket, "files", scope) is True
    assert tickets.validate(ticket, "files", scope) is False


def test_normalise_changes_keeps_paths_relative_and_deduplicated(app_module, temp_root):
    from backend.file_events import _normalise_changes

    changes = {
        (Change.added, str(temp_root / "notes" / "new.md")),
        (Change.added, str(temp_root / "notes" / "new.md")),
        (Change.deleted, str(temp_root / "old.txt")),
        (Change.modified, str(temp_root.parent / "outside.txt")),
    }
    assert _normalise_changes(temp_root, changes) == [
        {"type": "added", "path": "notes/new.md"},
        {"type": "deleted", "path": "old.txt"},
    ]


def test_watch_filter_checks_workspace_relative_paths(app_module, tmp_path):
    from backend.file_events import _watch_filter_for

    root = tmp_path / ".muselab" / "projects" / "demo"
    root.mkdir(parents=True)
    include = _watch_filter_for(root)
    assert include(Change.added, str(root / "a.txt")) is True
    assert include(
        Change.added,
        str(root / ".muselab" / "state.db"),
    ) is False
    assert include(
        Change.modified,
        str(root / "node_modules" / "pkg" / "x.js"),
    ) is False
    for opaque in (".cache", ".local", ".codex", ".claude", ".npm", "venv"):
        assert include(Change.modified, str(root / opaque)) is True
        assert include(
            Change.modified,
            str(root / opaque / "deep" / "state.json"),
        ) is False


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("OS file watch limit reached"),
        RuntimeError("No space left on device (os error 28)"),
        RuntimeError("Too many open files (os error 24)"),
        OSError(28, "No space left on device"),
    ],
)
def test_watch_resource_errors_switch_to_bounded_polling(
    app_module,
    error,
):
    from backend.file_events import _is_watch_resource_error

    assert _is_watch_resource_error(error) is True
    assert _is_watch_resource_error(
        RuntimeError("synthetic unrelated watcher failure")
    ) is False


@pytest.mark.asyncio
async def test_watch_limit_fallback_polls_only_indexed_shallow_directories(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    opaque = temp_root / ".local" / "deep"
    opaque.mkdir(parents=True)
    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    calls = []

    async def fake_awatch(*paths, **options):
        calls.append((tuple(Path(path) for path in paths), options))
        if len(calls) == 1:
            raise RuntimeError(
                "No space left on device (os error 28)"
            )
        return
        yield set()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    monkeypatch.setattr(file_events, "_WATCH_RETRY_S", 0)
    manager = file_events.FileWatchManager(store)
    state = file_events._WatchState(
        root=temp_root,
        workspace_id=workspace_id,
        name="root",
        primary=True,
        initialized=True,
    )
    await asyncio.wait_for(manager._watch(state), timeout=2)

    polling_paths, polling_options = calls[1]
    assert state.force_polling is True
    assert polling_options["force_polling"] is True
    assert polling_options["recursive"] is False
    assert temp_root in polling_paths
    assert temp_root / "notes" in polling_paths
    assert opaque not in polling_paths
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_shares_one_native_watcher_per_workspace(
    app_module,
    temp_root: Path,
    monkeypatch,
):
    import backend.file_events as file_events

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_awatch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        yield {(Change.added, str(temp_root / "fresh.txt"))}
        await asyncio.Future()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    store = file_events.WorkspaceStore(temp_root)
    manager = file_events.FileWatchManager(store)
    async with manager.subscribe(temp_root) as first:
        await asyncio.wait_for(started.wait(), timeout=1)
        # Establish the baseline before the native event so the test exercises
        # one durable incremental mutation rather than first-run bootstrap.
        assert await manager.current_cursor(temp_root) == 0
        async with manager.subscribe(temp_root) as second:
            assert calls == 1
            (temp_root / "fresh.txt").write_text(
                "fresh\n",
                encoding="utf-8",
            )
            release.set()
            first_payload, second_payload = await asyncio.gather(
                asyncio.wait_for(first.get(), timeout=1),
                asyncio.wait_for(second.get(), timeout=1),
            )
            assert first_payload["cursor"] == 1
            assert first_payload["changes"] == [{
                "type": "added",
                "path": "fresh.txt",
                "seq": 1,
                "name": "fresh.txt",
                "is_dir": False,
                "size": 6,
                "mtime": (temp_root / "fresh.txt").stat().st_mtime,
                "mtime_ns": (
                    temp_root / "fresh.txt"
                ).stat().st_mtime_ns,
            }]
            assert second_payload == first_payload
    assert len(manager._states) == 1
    assert manager._states[temp_root.resolve()].task is None
    await manager.shutdown()
    assert manager._states == {}


@pytest.mark.asyncio
async def test_watcher_uses_shallow_indexed_directories_and_closes_refresh_gap(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    opaque = temp_root / ".local" / "deep"
    opaque.mkdir(parents=True)
    (opaque / "ignored.txt").write_text("ignored", encoding="utf-8")
    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)

    incoming = temp_root / "incoming"
    nested = incoming / "nested"
    nested.mkdir(parents=True)
    (nested / "before-refresh.txt").write_text(
        "before",
        encoding="utf-8",
    )
    calls = []
    gap_file = nested / "during-refresh.txt"
    handoff_file = nested / "after-stop-before-refresh.txt"
    second_started = asyncio.Event()

    async def fake_awatch(*paths, **options):
        calls.append((tuple(Path(path) for path in paths), options))
        if len(calls) == 1:
            try:
                yield {(Change.added, str(incoming))}
            finally:
                # The old shallow generation has been stopped, while the new
                # directory set is not armed yet. No watcher reports this.
                handoff_file.write_text("handoff", encoding="utf-8")
            return
        if len(calls) == 2:
            # No native event is delivered for this write. The watch-first
            # closing reconciliation must still make it durable.
            gap_file.write_text("gap", encoding="utf-8")
            second_started.set()
            await options["stop_event"].wait()
            return
        await asyncio.Future()
        yield set()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    manager = file_events.FileWatchManager(store)
    state = file_events._WatchState(
        root=temp_root,
        workspace_id=workspace_id,
        name="root",
        primary=True,
        initialized=True,
    )
    watch_task = asyncio.create_task(manager._watch(state))
    state.task = watch_task
    await asyncio.wait_for(second_started.wait(), timeout=2)
    assert state.reconcile_task is not None
    await asyncio.wait_for(state.reconcile_task, timeout=2)

    first_paths, first_options = calls[0]
    second_paths, second_options = calls[1]
    assert temp_root in first_paths
    assert temp_root / "notes" in first_paths
    assert opaque not in first_paths
    assert incoming not in first_paths
    assert incoming in second_paths
    assert nested in second_paths
    assert first_options["recursive"] is False
    assert second_options["recursive"] is False
    assert first_options["force_polling"] is file_events._FORCE_POLLING

    indexed = {
        row["path"]
        for row in store.bootstrap(workspace_id)["entries"]
    }
    assert "incoming/nested/during-refresh.txt" in indexed
    assert "incoming/nested/after-stop-before-refresh.txt" in indexed
    replay_paths = {
        row["path"]
        for row in store.delta(workspace_id, baseline)["changes"]
    }
    assert "incoming/nested/during-refresh.txt" in replay_paths
    assert "incoming/nested/after-stop-before-refresh.txt" in replay_paths
    watch_task.cancel()
    await asyncio.gather(watch_task, return_exceptions=True)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_start_is_nonblocking_and_reconciles_on_first_use(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    (temp_root / "offline.txt").write_text("offline", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    real_reconcile = store.reconcile

    def blocking_reconcile(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return real_reconcile(*args, **kwargs)

    async def fake_awatch(*_args, **_kwargs):
        await asyncio.Future()
        yield set()

    monkeypatch.setattr(store, "reconcile", blocking_reconcile)
    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    manager = file_events.FileWatchManager(store)

    await asyncio.wait_for(manager.start(), timeout=1)
    assert manager._started is True
    assert not entered.is_set()
    assert manager._states == {}

    bootstrap_task = asyncio.create_task(manager.bootstrap(temp_root))
    assert await asyncio.to_thread(entered.wait, 1)
    # An initialized index is a last-good snapshot: first paint must not wait
    # for an offline reconciliation of a large workspace.
    bootstrap = await asyncio.wait_for(bootstrap_task, timeout=1)
    assert "offline.txt" not in {
        row["path"]
        for row in bootstrap["entries"]
    }

    release.set()
    state = manager._states[temp_root.resolve()]
    await asyncio.wait_for(state.reconcile_task, timeout=2)
    replay = await manager.delta(temp_root, 0)
    assert [(row["type"], row["path"]) for row in replay["changes"]] == [
        ("added", "offline.txt"),
    ]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_initial_reconcile_retries_on_next_ensure(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    store = file_events.WorkspaceStore(temp_root)
    real_reconcile = store.reconcile
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise file_events.WorkspaceScanIncomplete(
                "synthetic transient scan failure"
            )
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(store, "reconcile", fail_once)
    manager = file_events.FileWatchManager(store)
    with pytest.raises(file_events.HTTPException) as first:
        await manager.bootstrap(temp_root)
    assert first.value.status_code == 503

    recovered = await asyncio.wait_for(
        manager.bootstrap(temp_root),
        timeout=2,
    )
    assert calls == 2
    assert "notes/a.md" in {
        row["path"]
        for row in recovered["entries"]
    }
    await manager.shutdown()


@pytest.mark.asyncio
async def test_sse_ready_uses_lightweight_cursor_not_bootstrap(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    local_manager = file_events.FileWatchManager(store)
    monkeypatch.setattr(file_events, "manager", local_manager)

    async def fake_awatch(*_args, **_kwargs):
        await asyncio.Future()
        yield set()

    async def forbidden_bootstrap(*_args, **_kwargs):
        raise AssertionError("SSE ready must not materialize the snapshot")

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    monkeypatch.setattr(local_manager, "bootstrap", forbidden_bootstrap)
    stream = file_events._event_stream(temp_root, cursor=0)
    ready = await asyncio.wait_for(anext(stream), timeout=1)
    assert ready.event == "ready"
    assert '"cursor":0' in ready.data
    await stream.aclose()
    await local_manager.shutdown()


@pytest.mark.asyncio
async def test_large_reconcile_reloads_cursor_without_bad_arguments(
    app_module,
    temp_root,
):
    from backend.file_events import FileWatchManager, _WatchState

    class FakeStore:
        def __init__(self):
            self.cursor_calls = []

        def current_cursor(self, workspace_id):
            self.cursor_calls.append(workspace_id)
            return 9001

        def reconcile(self, *_args, **_kwargs):
            return 9001

        def delta(self, *_args, **_kwargs):
            return {
                "cursor": 5000,
                "changes": [],
                "resync": False,
                "has_more": True,
            }

    store = FakeStore()
    manager = FileWatchManager(store)
    queue = asyncio.Queue()
    state = _WatchState(
        root=temp_root,
        workspace_id="workspace-id",
        name="root",
        primary=True,
        initialized=True,
        subscribers={queue},
    )
    await manager._reconcile_and_broadcast(state)
    assert store.cursor_calls == ["workspace-id", "workspace-id"]
    assert queue.get_nowait() == {
        "resync": True,
        "changes": [],
        "cursor": 9001,
    }


@pytest.mark.asyncio
async def test_watcher_broadcasts_large_batch_resync(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    class FakeStore:
        def apply_changes(self, *_args, **_kwargs):
            return {"cursor": 42, "changes": [], "resync": True}

    async def fake_awatch(*_args, **_kwargs):
        yield {(Change.added, str(temp_root / "bulk"))}

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    manager = file_events.FileWatchManager(FakeStore())
    queue = asyncio.Queue()
    state = file_events._WatchState(
        root=temp_root,
        workspace_id="workspace-id",
        initialized=True,
        subscribers={queue},
    )
    await manager._watch(state)
    assert queue.get_nowait() == {
        "cursor": 42,
        "changes": [],
        "resync": True,
    }


def test_directory_modified_events_are_bounded_cursor_noops(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.workspace_store as workspace_store
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = workspace_store.WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)
    scans = 0
    real_scan = workspace_store.scan_workspace

    def counted_scan(root):
        nonlocal scans
        scans += 1
        return real_scan(root)

    monkeypatch.setattr(workspace_store, "scan_workspace", counted_scan)
    duplicate = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "modified", "path": "notes"}],
    )
    assert duplicate == {"cursor": baseline, "changes": []}
    assert scans == 0

    target = temp_root / "notes" / "a.md"
    target.write_text("# changed\n", encoding="utf-8")
    changed = store.apply_changes(
        workspace_id,
        temp_root,
        [
            {"type": "modified", "path": "notes"},
            {"type": "modified", "path": "notes/a.md"},
        ],
    )
    assert scans == 0
    assert [
        (row["type"], row["path"])
        for row in changed["changes"]
    ] == [("modified", "notes/a.md")]
    assert changed["cursor"] == baseline + 1

    repeated = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "modified", "path": "notes/a.md"}],
    )
    unknown_delete = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "deleted", "path": "never-existed"}],
    )
    assert repeated == {"cursor": baseline + 1, "changes": []}
    assert unknown_delete == {
        "cursor": baseline + 1,
        "changes": [],
    }
    assert store.delta(workspace_id, baseline)["changes"] == (
        changed["changes"]
    )


def test_same_size_and_mtime_file_replacement_is_not_dropped(
    app_module,
    temp_root,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    target = temp_root / "same-signature.txt"
    target.write_text("aa", encoding="utf-8")
    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)
    old = target.stat()

    target.write_text("bb", encoding="utf-8")
    os.utime(
        target,
        ns=(old.st_atime_ns, old.st_mtime_ns),
    )
    assert target.stat().st_size == old.st_size
    assert target.stat().st_mtime_ns == old.st_mtime_ns

    changed = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "modified", "path": "same-signature.txt"}],
    )
    assert changed["cursor"] == baseline + 1
    assert changed["changes"][0]["path"] == "same-signature.txt"
    assert changed["changes"][0]["mtime_ns"] == old.st_mtime_ns
    replay = store.delta(workspace_id, baseline)
    assert replay["changes"][0]["mtime_ns"] == old.st_mtime_ns


def test_incomplete_added_directory_scan_preserves_last_good_descendants(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.workspace_store as workspace_store
    from backend.workspaces import registry

    child = temp_root / "incoming" / "old.txt"
    child.parent.mkdir()
    child.write_text("old", encoding="utf-8")
    workspace_id = registry.id_for(temp_root)
    store = workspace_store.WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)
    real_scan = workspace_store.scan_workspace

    def incomplete_scan(root):
        if Path(root).resolve() == child.parent.resolve():
            raise workspace_store.WorkspaceScanIncomplete(
                "synthetic transient read failure"
            )
        return real_scan(root)

    monkeypatch.setattr(
        workspace_store,
        "scan_workspace",
        incomplete_scan,
    )
    payload = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "added", "path": "incoming"}],
    )
    assert payload == {
        "cursor": baseline,
        "changes": [],
        "_reconcile": True,
    }
    assert child.exists()
    assert "incoming/old.txt" in {
        row["path"]
        for row in store.bootstrap(workspace_id)["entries"]
    }


def test_metadata_error_makes_reconcile_incomplete_without_false_deletes(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.workspace_store as workspace_store
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = workspace_store.WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = {
        row["path"]
        for row in store.bootstrap(workspace_id)["entries"]
    }
    real_scandir = workspace_store.os.scandir

    class BrokenEntry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_symlink(self):
            return self._entry.is_symlink()

        def is_dir(self):
            raise PermissionError("synthetic metadata race")

    class RootScan:
        def __enter__(self):
            with real_scandir(temp_root) as iterator:
                entries = list(iterator)
            return iter([
                BrokenEntry(entry)
                if entry.name == "notes"
                else entry
                for entry in entries
            ])

        def __exit__(self, *_args):
            return False

    def flaky_scandir(path):
        if Path(path).resolve() == temp_root.resolve():
            return RootScan()
        return real_scandir(path)

    monkeypatch.setattr(
        workspace_store.os,
        "scandir",
        flaky_scandir,
    )
    with pytest.raises(workspace_store.WorkspaceScanIncomplete):
        store.reconcile(
            workspace_id,
            temp_root,
            "root",
            primary=True,
        )
    assert {
        row["path"]
        for row in store.bootstrap(workspace_id)["entries"]
    } == baseline


def test_workspace_database_files_are_private(
    app_module,
    temp_root,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    store = WorkspaceStore(temp_root)
    store.reconcile(
        registry.id_for(temp_root),
        temp_root,
        "root",
        primary=True,
    )
    assert all(
        "ctime_ns" not in row and "inode" not in row
        for row in store.bootstrap(
            registry.id_for(temp_root)
        )["entries"]
    )
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    with store._connect() as db:
        db.execute(
            "UPDATE workspaces SET scanned_at = scanned_at"
        )
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{store.path}{suffix}")
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_added_directory_keeps_descendant_and_replace_semantics(
    app_module,
    temp_root,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)

    folder = temp_root / "incoming"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("b", encoding="utf-8")
    added = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "added", "path": "incoming"}],
    )
    assert {row["path"] for row in added["changes"]} == {
        "incoming",
        "incoming/a.txt",
        "incoming/nested",
        "incoming/nested/b.txt",
    }
    cursor = added["cursor"]
    assert store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "added", "path": "incoming"}],
    ) == {"cursor": cursor, "changes": []}

    (folder / "a.txt").unlink()
    (folder / "c.txt").write_text("c", encoding="utf-8")
    replaced = store.apply_changes(
        workspace_id,
        temp_root,
        [
            {"type": "deleted", "path": "incoming"},
            {"type": "added", "path": "incoming"},
        ],
    )
    by_path = {
        row["path"]: row["type"]
        for row in replaced["changes"]
    }
    assert by_path["incoming/a.txt"] == "deleted"
    assert by_path["incoming/c.txt"] == "added"
    indexed = {
        row["path"]
        for row in store.bootstrap(workspace_id)["entries"]
    }
    assert "incoming/a.txt" not in indexed
    assert "incoming/c.txt" in indexed


def test_large_reconcile_and_watcher_batch_collapse_to_resync(
    app_module,
    temp_root,
    monkeypatch,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root, event_limit=1_000)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)

    for index in range(501):
        (temp_root / f"offline-{index:03}.txt").write_text(
            "offline",
            encoding="utf-8",
        )
    original_upsert = store._upsert_file
    incremental_upserts = 0

    def counted_upsert(*args):
        nonlocal incremental_upserts
        incremental_upserts += 1
        return original_upsert(*args)

    monkeypatch.setattr(store, "_upsert_file", counted_upsert)
    reset_cursor = store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
    )
    assert incremental_upserts == 0
    monkeypatch.setattr(store, "_upsert_file", original_upsert)
    assert reset_cursor == baseline + 1
    assert store.delta(workspace_id, baseline) == {
        "workspace_id": workspace_id,
        "cursor": reset_cursor,
        "changes": [],
        "resync": True,
        "has_more": False,
    }
    assert store.delta(workspace_id, reset_cursor)["resync"] is False

    bulk = temp_root / "bulk"
    bulk.mkdir()
    for index in range(501):
        (bulk / f"new-{index:03}.txt").write_text(
            "new",
            encoding="utf-8",
        )
    payload = store.apply_changes(
        workspace_id,
        temp_root,
        [{"type": "added", "path": "bulk"}],
    )
    assert payload == {
        "cursor": reset_cursor + 1,
        "changes": [],
        "resync": True,
        "_watch_refresh": True,
    }
    assert store.delta(workspace_id, reset_cursor)["resync"] is True
    fresh = store.bootstrap(workspace_id)
    assert fresh["cursor"] == payload["cursor"]
    assert "bulk/new-500.txt" in {
        row["path"]
        for row in fresh["entries"]
    }


def test_filtered_bootstrap_keeps_hidden_delta_semantics(
    app_module,
    temp_root,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    hidden = temp_root / ".hidden-data" / "share"
    hidden.mkdir(parents=True)
    target = hidden / "state.txt"
    target.write_text("before", encoding="utf-8")
    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)

    filtered = store.bootstrap(workspace_id, show_hidden=False)
    complete = store.bootstrap(workspace_id, show_hidden=True)
    assert filtered["cursor"] == complete["cursor"]
    assert all(
        not any(part.startswith(".") for part in Path(row["path"]).parts)
        for row in filtered["entries"]
    )
    assert ".hidden-data/share/state.txt" in {
        row["path"]
        for row in complete["entries"]
    }

    cursor = filtered["cursor"]
    target.write_text("after", encoding="utf-8")
    store.apply_changes(
        workspace_id,
        temp_root,
        [{
            "type": "modified",
            "path": ".hidden-data/share/state.txt",
        }],
    )
    delta = store.delta(workspace_id, cursor)
    assert [
        (row["type"], row["path"])
        for row in delta["changes"]
    ] == [("modified", ".hidden-data/share/state.txt")]


def test_heavy_hidden_subtrees_keep_node_but_skip_descendants(
    app_module,
    temp_root,
):
    from backend.workspace_store import scan_workspace

    for name in (".cache", ".local", ".codex", ".claude", ".npm", "venv"):
        nested = temp_root / name / "deep"
        nested.mkdir(parents=True)
        (nested / "state.json").write_text(
            "{}",
            encoding="utf-8",
        )
    paths = {row["path"] for row in scan_workspace(temp_root)}
    for name in (".cache", ".local", ".codex", ".claude", ".npm", "venv"):
        assert name in paths
        assert f"{name}/deep" not in paths
        assert f"{name}/deep/state.json" not in paths


def test_unchanged_reconcile_does_not_bulk_reinsert(
    app_module,
    temp_root,
    monkeypatch,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    cursor = store.current_cursor(workspace_id)

    def forbidden_insert(*_args, **_kwargs):
        raise AssertionError("unchanged reconciliation must not rewrite all rows")

    monkeypatch.setattr(store, "_insert_files", forbidden_insert)
    assert store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
    ) == cursor
    assert store.delta(workspace_id, cursor) == {
        "workspace_id": workspace_id,
        "cursor": cursor,
        "changes": [],
        "resync": False,
        "has_more": False,
    }


def test_slow_subscriber_is_collapsed_to_resync(app_module, temp_root):
    from backend.file_events import FileWatchManager, _WatchState

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    state = _WatchState(root=temp_root, subscribers={queue})
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "first"}]},
    )
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "second"}]},
    )
    assert queue.get_nowait() == {"resync": True, "changes": []}
