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


def test_selected_bootstrap_post_is_bounded_and_validates_parents(
    client,
    auth,
    temp_root,
):
    headers = {
        **auth,
        "X-Muselab-Workspace": str(temp_root),
    }
    response = client.post(
        "/api/files/bootstrap",
        headers=headers,
        json={
            "show_hidden": False,
            "parents": ["./notes", "notes//deep", "notes"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["partial"] is True
    assert payload["parents"] == ["notes", "notes/deep"]
    assert payload["truncated_parents"] == []
    assert payload["children_per_parent_limit"] == 500
    assert {row["path"] for row in payload["entries"]} == {
        "README.md",
        "notes",
        "notes/a.md",
        "notes/b.txt",
        "notes/deep",
        "notes/deep/c.py",
    }

    root_only = client.post(
        "/api/files/bootstrap",
        headers=headers,
        json={},
    )
    assert root_only.status_code == 200
    root_payload = root_only.json()
    assert root_payload["partial"] is True
    assert root_payload["parents"] == []
    assert {row["path"] for row in root_payload["entries"]} == {
        "README.md",
        "notes",
    }

    # GET remains the backward-compatible full snapshot. The workspace root is
    # still selected by the trusted header rather than accepted in the body.
    legacy = client.get("/api/files/bootstrap", headers=headers)
    assert legacy.status_code == 200
    assert "partial" not in legacy.json()

    for parent in ("../secret", "/etc", r"C:\\Windows", "notes/../../x"):
        invalid = client.post(
            "/api/files/bootstrap",
            headers=headers,
            json={"parents": [parent]},
        )
        assert invalid.status_code == 422
    too_many = client.post(
        "/api/files/bootstrap",
        headers=headers,
        json={"parents": [f"folder-{index}" for index in range(101)]},
    )
    assert too_many.status_code == 422


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
    idle_state = manager._states[temp_root.resolve()]
    assert idle_state.task is not None
    assert idle_state.stop_task is not None
    await manager.shutdown()
    assert manager._states == {}


@pytest.mark.asyncio
async def test_idle_watcher_lingers_and_reconnect_reuses_it(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    monkeypatch.setattr(file_events, "_WATCH_LINGER_S", 0.05)
    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    calls = 0
    started = asyncio.Event()

    async def fake_awatch(*_paths, **options):
        nonlocal calls
        calls += 1
        started.set()
        await options["stop_event"].wait()
        return
        yield set()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    manager = file_events.FileWatchManager(store)
    async with manager.subscribe(temp_root):
        await asyncio.wait_for(started.wait(), timeout=1)
    state = manager._states[temp_root.resolve()]
    first_watcher = state.task
    assert first_watcher is not None
    assert state.stop_task is not None

    # A refresh-style reconnect inside the grace period cancels delayed stop
    # and atomically retains the already-armed native watcher.
    async with manager.subscribe(temp_root):
        assert state.task is first_watcher
        assert state.stop_task is None
        assert calls == 1

    await asyncio.sleep(0.08)
    assert state.task is None
    assert state.stop_task is None
    assert state.watch_ready.is_set() is False
    assert state.watch_paths == ()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_idle_watcher_lru_and_stale_unsubscribe_are_bounded(
    app_module,
    temp_root,
):
    import backend.file_events as file_events

    manager = file_events.FileWatchManager(
        file_events.WorkspaceStore(temp_root)
    )

    async def parked():
        await asyncio.Future()

    roots = [temp_root / f"workspace-{index}" for index in range(5)]
    queues = [asyncio.Queue() for _ in roots]
    states = []
    for index, root in enumerate(roots):
        root = root.resolve()
        state = file_events._WatchState(
            root=root,
            workspace_id=f"workspace-{index}",
            subscribers={queues[index]},
            task=asyncio.create_task(parked()),
            initialized=True,
        )
        states.append(state)
        manager._states[root] = state

    # Keep one active subscriber out of the idle LRU. Idling four more states
    # immediately evicts only the least-recently-idled watcher.
    for index in range(1, 5):
        await manager._unsubscribe(roots[index], queues[index])
    assert states[0].task is not None
    assert states[0].subscribers == {queues[0]}
    assert states[1].task is None
    assert list(manager._idle_watchers) == [
        root.resolve()
        for root in roots[2:5]
    ]
    assert sum(
        state.task is not None and not state.subscribers
        for state in states
    ) == 3

    # Cleanup from a queue belonging to an old/replaced state must not schedule
    # a stop against the replacement state at the same path.
    replacement_queue = asyncio.Queue()
    replacement = file_events._WatchState(
        root=roots[1].resolve(),
        workspace_id="replacement",
        subscribers={replacement_queue},
        task=asyncio.create_task(parked()),
        initialized=True,
    )
    manager._states[replacement.root] = replacement
    await manager._unsubscribe(replacement.root, queues[1])
    assert replacement.subscribers == {replacement_queue}
    assert replacement.stop_task is None
    assert replacement.task is not None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_watcher_restart_queues_an_armed_closing_reconcile(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    manager = file_events.FileWatchManager(
        file_events.WorkspaceStore(temp_root)
    )
    state = file_events._WatchState(
        root=temp_root.resolve(),
        workspace_id="workspace-id",
        initialized=True,
    )
    manager._states[state.root] = state
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    arm_restarted_watcher = asyncio.Event()
    second_finished = asyncio.Event()
    calls = 0

    async def controlled_reconcile(_state):
        nonlocal calls
        calls += 1
        if calls == 1:
            _state.reconcile_running = True
            first_entered.set()
            await release_first.wait()
            _state.reconcile_running = False
        else:
            second_finished.set()

    async def controlled_watch(watch_state):
        await arm_restarted_watcher.wait()
        watch_state.watch_paths = (watch_state.root,)
        watch_state.watch_ready.set()
        await asyncio.Future()

    monkeypatch.setattr(
        manager,
        "_reconcile_and_broadcast",
        controlled_reconcile,
    )
    monkeypatch.setattr(manager, "_watch", controlled_watch)
    async with manager._lock:
        manager._queue_reconcile_locked(state)
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    assert state.reconcile_running is True

    async with manager._lock:
        assert manager._start_watcher_locked(state) is True
        manager._queue_reconcile_locked(state)
        assert state.reconcile_pending is True
    release_first.set()
    await asyncio.sleep(0)
    assert calls == 1
    assert second_finished.is_set() is False

    # The queued closing pass cannot start until the replacement watcher has
    # actually reached its armed point.
    arm_restarted_watcher.set()
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    assert calls == 2
    await asyncio.wait_for(state.reconcile_task, timeout=1)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_generation_reconciles_only_after_retry_is_armed(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events
    from backend.workspaces import registry

    store = file_events.WorkspaceStore(temp_root)
    workspace_id = registry.id_for(temp_root)
    store.reconcile(workspace_id, temp_root, "root", primary=True)
    baseline = store.current_cursor(workspace_id)
    gap_file = temp_root / "created-in-watch-gap.txt"
    second_armed = asyncio.Event()
    generations = 0
    reconciles = 0
    real_reconcile = store.reconcile

    def guarded_reconcile(*args, **kwargs):
        nonlocal reconciles
        reconciles += 1
        # The synchronously failed first `anext` must not set watch_ready or
        # permit a closing scan before the polling/native retry is installed.
        assert second_armed.is_set()
        return real_reconcile(*args, **kwargs)

    async def flaky_awatch(*_paths, **options):
        nonlocal generations
        generations += 1
        if generations == 1:
            gap_file.write_text("gap", encoding="utf-8")
            raise RuntimeError("synthetic synchronous watch setup failure")
            yield set()
        second_armed.set()
        await options["stop_event"].wait()
        return
        yield set()

    monkeypatch.setattr(store, "reconcile", guarded_reconcile)
    monkeypatch.setattr(file_events, "awatch", flaky_awatch)
    monkeypatch.setattr(file_events, "_WATCH_RETRY_S", 0)
    manager = file_events.FileWatchManager(store)
    async with manager.subscribe(temp_root):
        await asyncio.wait_for(second_armed.wait(), timeout=1)
        state = manager._states[temp_root.resolve()]
        await asyncio.wait_for(state.reconcile_task, timeout=2)
        assert generations >= 2
        assert reconciles == 1
        assert state.needs_closing_reconcile is False
        assert "created-in-watch-gap.txt" in {
            row["path"]
            for row in store.bootstrap(workspace_id)["entries"]
        }
        assert "created-in-watch-gap.txt" in {
            row["path"]
            for row in store.delta(workspace_id, baseline)["changes"]
        }
    await manager.shutdown()


@pytest.mark.asyncio
async def test_full_reconciles_are_globally_serialized(
    app_module,
    temp_root,
):
    import backend.file_events as file_events

    class ConcurrentStore:
        def __init__(self):
            self.lock = threading.Lock()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.active = 0
            self.max_active = 0
            self.calls = 0
            self.block_delta = False
            self.delta_entered = threading.Event()
            self.delta_release = threading.Event()
            self.delta_release.set()

        def current_cursor(self, _workspace_id):
            return 0

        def reconcile(self, *_args, **_kwargs):
            with self.lock:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.entered.set()
            assert self.release.wait(timeout=3)
            with self.lock:
                self.active -= 1

        def delta(self, *_args, **_kwargs):
            if self.block_delta:
                self.delta_entered.set()
                assert self.delta_release.wait(timeout=3)
            return {
                "cursor": 0,
                "changes": [],
                "resync": False,
                "has_more": False,
            }

        def close(self):
            return None

    store = ConcurrentStore()
    manager = file_events.FileWatchManager(store)
    first = file_events._WatchState(
        root=temp_root / "first",
        workspace_id="first",
        name="first",
        initialized=True,
    )
    second = file_events._WatchState(
        root=temp_root / "second",
        workspace_id="second",
        name="second",
        initialized=True,
    )
    first_task = asyncio.create_task(
        manager._reconcile_and_broadcast(first)
    )
    assert await asyncio.to_thread(store.entered.wait, 1)
    second_task = asyncio.create_task(
        manager._reconcile_and_broadcast(second)
    )
    await asyncio.sleep(0.05)
    assert store.calls == 1
    assert store.max_active == 1
    queued_workspace_mutated = asyncio.Event()

    async def mutate_while_scan_is_queued():
        async with second.mutation_lock:
            queued_workspace_mutated.set()

    queued_mutation = asyncio.create_task(mutate_while_scan_is_queued())
    await asyncio.wait_for(queued_workspace_mutated.wait(), timeout=0.5)
    await queued_mutation
    store.release.set()
    await asyncio.gather(first_task, second_task)
    assert store.calls == 2
    assert store.max_active == 1

    # A watcher can restart while its workspace is queued behind the global
    # scan slot. The queued pass must follow the replacement generation and
    # wait for it to arm instead of scanning inside the new watch gap.
    async def parked_watcher():
        await asyncio.Future()

    queued = file_events._WatchState(
        root=temp_root / "queued",
        workspace_id="queued",
        name="queued",
        initialized=True,
        task=asyncio.create_task(parked_watcher()),
    )
    queued.watch_ready.set()
    queued.watch_paths = (queued.root,)
    await manager._reconcile_semaphore.acquire()
    queued_scan = asyncio.create_task(
        manager._reconcile_and_broadcast(queued)
    )
    await asyncio.sleep(0.02)
    queued.watch_ready.clear()
    manager._reconcile_semaphore.release()
    await asyncio.sleep(0.05)
    assert store.calls == 2
    queued.watch_ready.set()
    await asyncio.wait_for(queued_scan, timeout=1)
    assert store.calls == 3
    queued.task.cancel()
    await asyncio.gather(queued.task, return_exceptions=True)
    queued.task = None

    # The watcher mutation lock covers cursor-before through replay-after, not
    # just the scan itself, so a native batch cannot slip into the replay window
    # and get broadcast twice.
    store.block_delta = True
    store.delta_release.clear()
    window_task = asyncio.create_task(
        manager._reconcile_and_broadcast(first)
    )
    assert await asyncio.to_thread(store.delta_entered.wait, 1)
    mutation_acquired = asyncio.Event()

    async def watcher_mutation():
        async with first.mutation_lock:
            mutation_acquired.set()

    mutation_task = asyncio.create_task(watcher_mutation())
    await asyncio.sleep(0.05)
    assert mutation_acquired.is_set() is False
    store.delta_release.set()
    await asyncio.gather(window_task, mutation_task)
    assert mutation_acquired.is_set() is True
    await manager.shutdown()


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
    # This test drives the private watcher loop directly, so install the state
    # just as ``ensure_workspace`` would. The scheduler intentionally ignores
    # detached generations to prevent a removed workspace from being revived.
    manager._states[state.root] = state
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
async def test_ensure_workspace_hot_path_skips_registry_sqlite_roundtrips(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    store = file_events.WorkspaceStore(temp_root)
    manager = file_events.FileWatchManager(store)
    state = await manager.ensure_workspace(temp_root)
    await asyncio.wait_for(state.reconcile_task, timeout=2)

    register_calls = 0
    state_calls = 0
    real_register = store.register_workspace
    real_state = store.state

    def counted_register(*args, **kwargs):
        nonlocal register_calls
        register_calls += 1
        return real_register(*args, **kwargs)

    def counted_state(*args, **kwargs):
        nonlocal state_calls
        state_calls += 1
        return real_state(*args, **kwargs)

    monkeypatch.setattr(store, "register_workspace", counted_register)
    monkeypatch.setattr(store, "state", counted_state)
    assert await manager.ensure_workspace(temp_root) is state
    assert await manager.ensure_workspace(temp_root) is state
    partial = await manager.bootstrap(temp_root, parents=["notes"])
    assert partial["partial"] is True
    assert register_calls == 0
    assert state_calls == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_remove_serializes_with_inflight_first_ensure(
    app_module,
    temp_root,
    monkeypatch,
):
    """A slow registration cannot reinstall state after registry deletion."""
    import backend.file_events as file_events
    from backend.workspaces import registry

    extra_root = temp_root.parent / "workspace-remove-race"
    extra_root.mkdir(exist_ok=True)
    entry = registry.register(extra_root, "race")
    store = file_events.WorkspaceStore(temp_root)
    manager = file_events.FileWatchManager(store)
    register_started = threading.Event()
    allow_register = threading.Event()
    real_register = store.register_workspace

    def paused_register(*args, **kwargs):
        register_started.set()
        assert allow_register.wait(timeout=2)
        return real_register(*args, **kwargs)

    monkeypatch.setattr(store, "register_workspace", paused_register)
    ensure_task = asyncio.create_task(manager.ensure_workspace(extra_root))
    assert await asyncio.to_thread(register_started.wait, 2)

    registry.remove(extra_root)
    remove_task = asyncio.create_task(
        manager.remove_workspace(entry.id, extra_root),
    )
    await asyncio.sleep(0)
    assert not remove_task.done()
    allow_register.set()

    with pytest.raises(ValueError, match="workspace is not registered"):
        await ensure_task
    await asyncio.wait_for(remove_task, timeout=2)

    assert extra_root.resolve() not in manager._states
    with pytest.raises(KeyError, match="unknown workspace"):
        store.state(entry.id)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_remove_waits_for_inflight_reconcile_thread(
    app_module,
    temp_root,
    monkeypatch,
):
    """A cancelled outer task must not let its worker resurrect SQLite state."""
    import backend.file_events as file_events
    from backend.workspaces import registry

    extra_root = temp_root.parent / "workspace-reconcile-remove-race"
    extra_root.mkdir(exist_ok=True)
    (extra_root / "tracked.txt").write_text("data", encoding="utf-8")
    entry = registry.register(extra_root, "race")
    store = file_events.WorkspaceStore(temp_root)
    manager = file_events.FileWatchManager(store)
    reconcile_started = threading.Event()
    allow_reconcile = threading.Event()
    real_reconcile = store.reconcile

    def paused_reconcile(*args, **kwargs):
        reconcile_started.set()
        assert allow_reconcile.wait(timeout=2)
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(store, "reconcile", paused_reconcile)
    state = await manager.ensure_workspace(extra_root)
    assert await asyncio.to_thread(reconcile_started.wait, 2)

    registry.remove(extra_root)
    remove_task = asyncio.create_task(
        manager.remove_workspace(entry.id, extra_root),
    )
    await asyncio.sleep(0.05)
    assert not remove_task.done()
    allow_reconcile.set()
    await asyncio.wait_for(remove_task, timeout=2)

    assert state.reconcile_task is None
    assert extra_root.resolve() not in manager._states
    with pytest.raises(KeyError, match="unknown workspace"):
        store.state(entry.id)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_delete_and_same_path_readd_are_one_lifecycle(
    app_module,
    temp_root,
    monkeypatch,
):
    """POST cannot publish a new generation before DELETE finishes cleanup."""
    import backend.file_events as file_events
    from backend.workspaces import registry

    extra_root = temp_root.parent / "workspace-delete-readd-race"
    extra_root.mkdir(exist_ok=True)
    store = file_events.WorkspaceStore(temp_root)
    manager = file_events.FileWatchManager(store)
    first = await manager.register_workspace(extra_root, "first")
    first_state = manager._states[extra_root.resolve()]
    if first_state.reconcile_task is not None:
        await asyncio.wait_for(first_state.reconcile_task, timeout=2)

    remove_started = threading.Event()
    allow_remove = threading.Event()
    real_remove = store.remove_workspace

    def paused_remove(*args, **kwargs):
        remove_started.set()
        assert allow_remove.wait(timeout=2)
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(store, "remove_workspace", paused_remove)
    delete_task = asyncio.create_task(
        manager.unregister_workspace(extra_root),
    )
    assert await asyncio.to_thread(remove_started.wait, 2)
    readd_task = asyncio.create_task(
        manager.register_workspace(extra_root, "second"),
    )
    await asyncio.sleep(0.05)
    assert not readd_task.done()

    allow_remove.set()
    await asyncio.wait_for(delete_task, timeout=2)
    second = await asyncio.wait_for(readd_task, timeout=2)

    assert second.id != first.id
    assert registry.id_for(extra_root) == second.id
    assert store.state(second.id)["cursor"] >= 0
    with pytest.raises(KeyError, match="unknown workspace"):
        store.state(first.id)

    second_state = manager._states[extra_root.resolve()]
    if second_state.reconcile_task is not None:
        await asyncio.wait_for(second_state.reconcile_task, timeout=2)
    await manager.unregister_workspace(extra_root)
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
    assert f'"workspace_id":"{workspace_id}"' in ready.data
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
