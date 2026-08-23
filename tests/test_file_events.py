"""Shared filesystem watcher and SSE endpoint regressions."""

import asyncio
import contextlib
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


def test_unavailable_sse_crosses_gzip_middleware_as_clean_event(
    client,
    auth,
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    class UnavailableManager:
        @contextlib.asynccontextmanager
        async def subscribe(self, _root):
            yield asyncio.Queue()

        async def ready_state(self, _root):
            raise file_events.HTTPException(
                status_code=503,
                detail=f"private scan failure: {temp_root}/secret",
            )

    minted = client.post("/api/files/events-ticket", headers=auth)
    ticket = minted.json()["ticket"]
    monkeypatch.setattr(file_events, "manager", UnavailableManager())
    response = client.get(
        "/api/files/events",
        params={"ticket": ticket},
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("content-encoding") != "gzip"
    assert response.text.count("event: unavailable") == 1
    assert '{"available":false,"retryable":true}' in response.text
    assert str(temp_root) not in response.text
    assert "private scan failure" not in response.text


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
    for opaque in (
        ".cache", ".local", ".codex", ".claude", ".npm", "venv",
        ".jumbo", ".jumbo.bak",
    ):
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
async def test_bootstrap_and_delta_perf_events_are_bounded_and_selective(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    class FakeStore:
        delta_payload = {
            "cursor": 1,
            "changes": [{"path": "private-delta.txt"}],
            "resync": False,
            "has_more": False,
        }

        def bootstrap(self, *_args, **_kwargs):
            return {
                "entries": [
                    {"path": "private-one.txt"},
                    {"path": "private-two.txt"},
                ],
                "partial": True,
            }

        def delta(self, *_args, **_kwargs):
            return dict(self.delta_payload)

    store = FakeStore()
    manager = file_events.FileWatchManager(store)
    state = file_events._WatchState(
        root=temp_root,
        workspace_id="workspace-sensitive-identifier",
        initialized=True,
    )

    async def ensure_workspace(_root):
        return state

    async def await_baseline(_state):
        return None

    events = []
    monkeypatch.setattr(manager, "ensure_workspace", ensure_workspace)
    monkeypatch.setattr(manager, "_await_baseline", await_baseline)
    monkeypatch.setattr(file_events, "is_slow", lambda _duration: False)
    monkeypatch.setattr(
        file_events,
        "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )

    payload = await manager.bootstrap(temp_root, parents=[])
    assert len(payload["entries"]) == 2
    assert events[0][0] == "files.bootstrap"
    assert events[0][1] == {
        "workspace": "workspac",
        "status": "ok",
        "total_ms": events[0][1]["total_ms"],
        "entries": 2,
        "partial": True,
    }

    # A normal fast delta is intentionally silent.
    await manager.delta(temp_root, 0)
    assert [event for event, _fields in events] == ["files.bootstrap"]

    store.delta_payload = {
        **store.delta_payload,
        "resync": True,
        "changes": [],
    }
    await manager.delta(temp_root, 0)
    store.delta_payload = {
        **store.delta_payload,
        "resync": False,
        "has_more": True,
    }
    await manager.delta(temp_root, 0)
    monkeypatch.setattr(file_events, "is_slow", lambda _duration: True)
    store.delta_payload = {
        **store.delta_payload,
        "has_more": False,
    }
    await manager.delta(temp_root, 0)

    assert [event for event, _fields in events] == [
        "files.bootstrap",
        "files.delta",
        "files.delta",
        "files.delta",
    ]
    assert [fields["resync"] for _, fields in events[1:]] == [
        True,
        False,
        False,
    ]
    assert [fields["has_more"] for _, fields in events[1:]] == [
        False,
        True,
        False,
    ]
    captured = repr(events)
    assert str(temp_root) not in captured
    assert "private-one.txt" not in captured
    assert "private-delta.txt" not in captured


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
        if len(calls) <= 2:
            raise RuntimeError(
                "No space left on device (os error 28)"
            )
        return
        yield set()

    monkeypatch.setattr(file_events, "awatch", fake_awatch)
    monkeypatch.setattr(file_events, "_WATCH_RETRY_S", 0)
    perf_events = []
    monkeypatch.setattr(
        file_events,
        "perf_event",
        lambda event, **fields: perf_events.append((event, fields)),
    )
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
    assert perf_events == [
        (
            "files.watcher_mode",
            {
                "workspace": file_events.short_id(workspace_id),
                "mode": "polling",
                "reason": "resource_exhaustion",
                "error_type": "RuntimeError",
            },
        ),
    ]
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


def test_reconcile_backoff_is_scoped_to_each_workspace(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    monkeypatch.setattr(file_events, "monotonic", lambda: 100.0)
    monkeypatch.setattr(file_events, "_RECONCILE_RETRY_BASE_S", 1.0)
    monkeypatch.setattr(file_events, "_RECONCILE_RETRY_MAX_S", 10.0)
    first = file_events._WatchState(
        root=temp_root / "first",
        workspace_id="first",
    )
    second = file_events._WatchState(
        root=temp_root / "second",
        workspace_id="second",
    )

    file_events.FileWatchManager._record_reconcile_retry(first)
    file_events.FileWatchManager._record_reconcile_retry(first)
    file_events.FileWatchManager._record_reconcile_retry(second)

    assert first.reconcile_failures == 2
    assert first.reconcile_retry_at == 102.0
    assert second.reconcile_failures == 1
    assert second.reconcile_retry_at == 101.0
    file_events.FileWatchManager._reset_reconcile_retry(second)
    assert second.reconcile_retry_at == 0.0
    assert first.reconcile_retry_at == 102.0


@pytest.mark.asyncio
async def test_reconcile_perf_event_splits_wait_scan_and_replay(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    class FakeStore:
        def current_cursor(self, _workspace_id):
            return 9

        def reconcile(self, *_args, **_kwargs):
            return 9

        def delta(self, *_args, **_kwargs):
            return {
                "cursor": 9,
                "changes": [{"path": "private-replay.txt"}],
                "resync": False,
                "has_more": True,
            }

        def close(self):
            return None

    events = []
    monkeypatch.setattr(
        file_events,
        "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    manager = file_events.FileWatchManager(FakeStore())
    state = file_events._WatchState(
        root=temp_root,
        workspace_id="workspace-sensitive-identifier",
        initialized=True,
    )

    # The per-workspace mutation queue is observable without a process-wide
    # scan gate that would couple unrelated roots.
    await state.mutation_lock.acquire()
    task = asyncio.create_task(manager._reconcile_and_broadcast(state))
    await asyncio.sleep(0.02)
    state.mutation_lock.release()
    await asyncio.wait_for(task, timeout=1)

    assert len(events) == 1
    event, fields = events[0]
    assert event == "files.reconcile"
    assert fields["workspace"] == "workspac"
    assert fields["status"] == "ok"
    assert fields["error_type"] is None
    assert "scan_slot_wait_ms" not in fields
    assert fields["mutation_lock_wait_ms"] >= 10
    assert fields["scan_ms"] >= 0
    assert fields["replay_ms"] >= 0
    assert fields["scanned_files"] == 0
    assert fields["snapshot_files"] == 0
    assert fields["partial"] is False
    assert fields["partial_reason"] is None
    assert fields["changes"] == 1
    assert fields["resync"] is True
    assert fields["total_ms"] >= 10
    captured = repr(events)
    assert str(temp_root) not in captured
    assert "private-replay.txt" not in captured
    await manager.shutdown()


@pytest.mark.asyncio
async def test_workspace_reconciles_run_independently_off_event_loop(
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
    for _ in range(20):
        if store.calls == 2:
            break
        await asyncio.sleep(0.01)
    assert store.calls == 2
    assert store.max_active == 2

    # Both blocking scans are worker-thread work: the event loop must continue
    # servicing unrelated coroutines while neither filesystem pass can finish.
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
    store.release.set()
    await asyncio.gather(first_task, second_task)

    # A watcher can restart while its own mutation lock is queued. The pass must
    # follow the replacement generation and wait for it to arm instead of
    # scanning inside the new watch gap.
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
    await queued.mutation_lock.acquire()
    queued_scan = asyncio.create_task(
        manager._reconcile_and_broadcast(queued)
    )
    await asyncio.sleep(0.02)
    queued.watch_ready.clear()
    queued.mutation_lock.release()
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
async def test_reconcile_failures_back_off_coalesce_and_reset(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    store = file_events.WorkspaceStore(temp_root)
    real_reconcile = store.reconcile
    calls = 0
    events = []

    def fail_twice(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise file_events.WorkspaceScanIncomplete(
                f"private failure under {temp_root}/secret-{calls}"
            )
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(store, "reconcile", fail_twice)
    monkeypatch.setattr(file_events, "_RECONCILE_BACKOFF_START_S", 0.02)
    monkeypatch.setattr(file_events, "_RECONCILE_BACKOFF_CAP_S", 0.04)
    monkeypatch.setattr(
        file_events,
        "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    manager = file_events.FileWatchManager(store)

    with pytest.raises(file_events.HTTPException) as first:
        await manager.bootstrap(temp_root)
    assert first.value.detail == "workspace index is temporarily unavailable"
    assert str(temp_root) not in first.value.detail

    started = file_events.monotonic()
    failed_pair = await asyncio.gather(
        manager.bootstrap(temp_root),
        manager.bootstrap(temp_root),
        return_exceptions=True,
    )
    assert all(isinstance(item, file_events.HTTPException) for item in failed_pair)
    assert calls == 2
    assert file_events.monotonic() - started >= 0.015

    started = file_events.monotonic()
    recovered = await asyncio.gather(
        manager.bootstrap(temp_root),
        manager.bootstrap(temp_root),
    )
    assert calls == 3
    assert file_events.monotonic() - started >= 0.03
    assert recovered[0] == recovered[1]

    state = manager._states[temp_root.resolve()]
    assert state.reconcile_attempts == 3
    assert state.reconcile_failures == 0
    assert state.reconcile_retry_at == 0.0
    reconcile_events = [fields for event, fields in events
                        if event == "files.reconcile"]
    assert [fields["attempt"] for fields in reconcile_events] == [1, 2, 3]
    assert [fields["failures"] for fields in reconcile_events] == [1, 2, 0]
    assert [fields["backoff_ms"] for fields in reconcile_events] == [20, 40, 0]
    assert {fields["phase"] for fields in reconcile_events} == {"initial"}
    captured = repr(reconcile_events)
    assert str(temp_root) not in captured
    assert "private failure" not in captured
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_interrupts_reconcile_backoff(
    app_module,
    temp_root,
):
    import backend.file_events as file_events

    manager = file_events.FileWatchManager(file_events.WorkspaceStore(temp_root))
    state = file_events._WatchState(
        root=temp_root.resolve(),
        workspace_id="workspace-id",
        reconcile_retry_at=file_events.monotonic() + 30,
    )
    manager._states[state.root] = state
    async with manager._lock:
        manager._queue_reconcile_locked(state)
    await asyncio.sleep(0)

    await asyncio.wait_for(manager.shutdown(), timeout=0.5)
    assert state.reconcile_task is None


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
async def test_sse_initial_unavailable_is_private_and_closes_cleanly(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    exited = False

    class UnavailableManager:
        @contextlib.asynccontextmanager
        async def subscribe(self, _root):
            nonlocal exited
            try:
                yield asyncio.Queue()
            finally:
                exited = True

        async def ready_state(self, _root):
            raise file_events.HTTPException(
                status_code=503,
                detail=f"failed to scan {temp_root}/private: permission denied",
            )

    monkeypatch.setattr(file_events, "manager", UnavailableManager())
    events = [event async for event in file_events._event_stream(temp_root, None)]

    assert len(events) == 1
    assert events[0].event == "unavailable"
    assert events[0].data == '{"available":false,"retryable":true}'
    assert str(temp_root) not in events[0].data
    assert "permission denied" not in events[0].data
    assert exited is True


@pytest.mark.asyncio
async def test_http_exception_after_sse_ready_becomes_clean_eof(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.file_events as file_events

    class ClosingManager:
        @contextlib.asynccontextmanager
        async def subscribe(self, _root):
            yield asyncio.Queue()

        async def ready_state(self, _root):
            return {"workspace_id": "safe-id", "cursor": 1}

        async def delta(self, _root, _cursor):
            raise file_events.HTTPException(
                status_code=503,
                detail=f"late failure at {temp_root}/private",
            )

    monkeypatch.setattr(file_events, "manager", ClosingManager())
    events = [event async for event in file_events._event_stream(temp_root, 0)]

    assert [event.event for event in events] == ["ready"]
    assert str(temp_root) not in events[0].data


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

    def counted_scan(root, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(root, **kwargs)

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


def test_reconcile_budget_reports_partial_without_inventing_deletes(
    app_module,
    temp_root,
):
    from backend.workspace_store import WorkspaceStore
    from backend.workspaces import registry

    workspace_id = registry.id_for(temp_root)
    store = WorkspaceStore(temp_root)
    store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
        max_files=None,
        max_seconds=None,
    )
    stale = temp_root / "README.md"
    stale.unlink()
    (temp_root / "00-observed.txt").write_text("new", encoding="utf-8")

    report = {}
    store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
        max_files=1,
        max_seconds=None,
        report=report,
    )

    assert report == {
        "partial": True,
        "partial_reason": "file_limit",
        "scanned_files": 1,
        "snapshot_files": 1,
        "resumed": False,
        "scan_ms": report["scan_ms"],
    }
    partial_paths = {
        row["path"] for row in store.bootstrap(workspace_id)["entries"]
    }
    assert "README.md" in partial_paths

    complete_report = {}
    store.reconcile(
        workspace_id,
        temp_root,
        "root",
        primary=True,
        max_files=None,
        max_seconds=None,
        report=complete_report,
    )
    assert complete_report["partial"] is False
    complete_paths = {
        row["path"] for row in store.bootstrap(workspace_id)["entries"]
    }
    assert "README.md" not in complete_paths
    assert "00-observed.txt" in complete_paths


def test_bounded_scan_resumes_until_snapshot_is_complete(
    app_module,
    tmp_path,
):
    from backend.workspace_store import WorkspaceStore

    root = tmp_path / "resumable"
    root.mkdir()
    for index in range(7):
        (root / f"file-{index}.txt").write_text("x", encoding="utf-8")

    store = WorkspaceStore(root)
    progress = {}
    reports = []
    for _ in range(10):
        report = {}
        store.reconcile(
            "bounded-workspace",
            root,
            "bounded",
            max_files=2,
            max_seconds=None,
            report=report,
            scan_progress=progress,
        )
        reports.append(report)
        if not report["partial"]:
            break

    assert len(reports) == 4
    assert [report["scanned_files"] for report in reports] == [2, 2, 2, 1]
    assert reports[-1]["snapshot_files"] == 7
    assert reports[-1]["resumed"] is True
    assert progress == {}
    assert {
        row["path"]
        for row in store.bootstrap("bounded-workspace")["entries"]
    } == {f"file-{index}.txt" for index in range(7)}


def test_bounded_scan_survives_directory_enumeration_order_change(
    app_module,
    tmp_path,
    monkeypatch,
):
    from backend import workspace_store
    from backend.workspace_store import WorkspaceScanIncomplete, WorkspaceStore

    root = tmp_path / "reordered-resume"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text(name, encoding="utf-8")

    store = WorkspaceStore(root)
    workspace_id = "reordered-workspace"
    store.reconcile(
        workspace_id,
        root,
        "reordered",
        max_files=None,
        max_seconds=None,
    )

    real_scandir = workspace_store.os.scandir
    root_calls = 0

    class ReorderedScandir:
        def __init__(self, directory):
            nonlocal root_calls
            self._iterator = real_scandir(directory)
            entries = list(self._iterator)
            if Path(directory).resolve() == root.resolve():
                root_calls += 1
                preferred = (
                    ["a.txt", "b.txt", "c.txt"]
                    if root_calls == 1
                    else ["c.txt", "a.txt", "b.txt"]
                )
                rank = {name: index for index, name in enumerate(preferred)}
                entries.sort(key=lambda child: rank.get(child.name, len(rank)))
            self._entries = entries

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *_exc_info):
            self._iterator.close()

    monkeypatch.setattr(workspace_store.os, "scandir", ReorderedScandir)
    progress = {}
    first_report = {}
    store.reconcile(
        workspace_id,
        root,
        "reordered",
        max_files=2,
        max_seconds=None,
        report=first_report,
        scan_progress=progress,
    )
    assert first_report["partial"] is True

    with pytest.raises(WorkspaceScanIncomplete):
        store.reconcile(
            workspace_id,
            root,
            "reordered",
            max_files=2,
            max_seconds=None,
            scan_progress=progress,
        )
    assert progress == {}

    final_report = {}
    store.reconcile(
        workspace_id,
        root,
        "reordered",
        max_files=None,
        max_seconds=None,
        report=final_report,
        scan_progress=progress,
    )

    assert root_calls == 3
    assert final_report["partial"] is False
    assert progress == {}
    assert {
        row["path"] for row in store.bootstrap(workspace_id)["entries"]
    } == {"a.txt", "b.txt", "c.txt"}


def test_explicit_large_tree_pruning_keeps_only_directory_nodes(
    app_module,
    temp_root,
):
    from backend.workspace_store import scan_workspace

    for name in (
        "node_modules",
        ".git",
        "dist",
        "build",
        "target",
        "output",
        "pdc_space",
        "share_space",
        "content_agent_freshdoc",
    ):
        nested = temp_root / name / "deep"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "large.bin").write_bytes(b"x")

    paths = {
        row["path"]
        for row in scan_workspace(
            temp_root,
            max_files=None,
            max_seconds=None,
        )
    }
    for name in (
        "node_modules",
        ".git",
        "dist",
        "build",
        "target",
        "output",
        "pdc_space",
        "share_space",
        "content_agent_freshdoc",
    ):
        assert name in paths
        assert f"{name}/deep" not in paths
        assert f"{name}/deep/large.bin" not in paths


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


def test_slow_subscriber_is_collapsed_to_resync(
    app_module,
    temp_root,
    monkeypatch,
):
    from backend.file_events import FileWatchManager, _WatchState
    import backend.file_events as file_events

    events = []
    monkeypatch.setattr(
        file_events,
        "perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    state = _WatchState(
        root=temp_root,
        workspace_id="workspace-sensitive-identifier",
        subscribers={queue},
    )
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "first"}]},
    )
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "second"}]},
    )
    FileWatchManager._broadcast(
        state,
        {"changes": [{"type": "added", "path": "third"}]},
    )
    assert queue.get_nowait() == {"resync": True, "changes": []}
    assert events == [
        (
            "files.watcher_queue_overflow",
            {
                "workspace": "workspac",
                "subscribers": 1,
                "overflowed": 1,
            },
        ),
    ]
    assert str(temp_root) not in repr(events)
