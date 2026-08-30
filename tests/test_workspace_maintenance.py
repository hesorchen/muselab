import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_file_watch_start_does_not_await_page_moving_maintenance(
    app_module,
):
    import backend.file_events as file_events

    class FakeStore:
        def __init__(self):
            self.initialized = False
            self.registered = []
            self.closed = False

        def initialize(self):
            self.initialized = True

        def maintain_database(self):
            raise AssertionError("startup must not run VACUUM maintenance")

        def register_workspace(self, *args, **kwargs):
            self.registered.append((args, kwargs))

        def close(self):
            self.closed = True

    store = FakeStore()
    manager = file_events.FileWatchManager(store)

    await manager.start()

    assert store.initialized is True
    assert store.registered
    assert manager._started is True
    await manager.shutdown()
    assert store.closed is True


def _legacy_database_with_freelist(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA auto_vacuum = NONE")
        db.execute("CREATE TABLE legacy_payloads (payload BLOB NOT NULL)")
        db.executemany(
            "INSERT INTO legacy_payloads(payload) VALUES (zeroblob(131072))",
            [()] * 32,
        )
        db.commit()
        db.execute("DELETE FROM legacy_payloads")
        db.commit()


def test_legacy_workspace_database_reports_offline_full_vacuum(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.workspace_store as workspace_store

    path = workspace_store.database_path(temp_root)
    _legacy_database_with_freelist(path)
    store = workspace_store.WorkspaceStore(temp_root)
    store.initialize()
    monkeypatch.setattr(workspace_store, "_VACUUM_MIN_RECLAIM_BYTES", 1)
    monkeypatch.setattr(workspace_store, "_VACUUM_MIN_FREE_RATIO", 0.0)
    size_before = path.stat().st_size

    result = store.maintain_database()

    assert result["action"] == "full-required"
    assert result["full_vacuum_required"] is True
    assert result["required_headroom_bytes"] == 0
    assert result["before"]["auto_vacuum"] == 0
    assert result["before"]["freelist_count"] > 0
    assert result["after"]["page_count"] == result["before"]["page_count"]
    assert path.stat().st_size == size_before


def test_offline_full_vacuum_checks_disk_headroom(
    app_module,
    temp_root,
    monkeypatch,
):
    import backend.workspace_store as workspace_store

    path = workspace_store.database_path(temp_root)
    _legacy_database_with_freelist(path)
    store = workspace_store.WorkspaceStore(temp_root)
    store.initialize()
    monkeypatch.setattr(workspace_store, "_VACUUM_MIN_RECLAIM_BYTES", 1)
    monkeypatch.setattr(workspace_store, "_VACUUM_MIN_FREE_RATIO", 0.0)
    monkeypatch.setattr(
        workspace_store.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    result = store.maintain_database(allow_full_vacuum=True)

    assert result["action"] == "full-skipped-no-space"
    assert result["full_vacuum_required"] is True
    assert result["required_headroom_bytes"] >= 64 * 1024 * 1024
    assert result["after"]["auto_vacuum"] == 0


@pytest.mark.asyncio
async def test_shutdown_keeps_ownership_of_started_maintenance(
    app_module,
    monkeypatch,
):
    import backend.file_events as file_events

    class FakeStore:
        def __init__(self):
            self.maintenance_started = threading.Event()
            self.release_maintenance = threading.Event()
            self.closed = False

        def initialize(self):
            return None

        def register_workspace(self, *_args, **_kwargs):
            return None

        def maintain_database(self):
            self.maintenance_started.set()
            assert self.release_maintenance.wait(timeout=2)
            return {"action": "none"}

        def close(self):
            assert self.release_maintenance.is_set()
            self.closed = True

    monkeypatch.setattr(file_events, "_DATABASE_MAINTENANCE_DELAY_S", 0)
    store = FakeStore()
    manager = file_events.FileWatchManager(store)
    await manager.start()
    assert await asyncio.to_thread(store.maintenance_started.wait, 1)

    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0.02)
    assert shutdown.done() is False
    assert store.closed is False

    store.release_maintenance.set()
    await asyncio.wait_for(shutdown, timeout=1)
    assert store.closed is True
