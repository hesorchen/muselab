"""Controlled thread gates for the file watch manager's lifecycle owners."""

import asyncio
import threading

import pytest


class _GatedStore:
    def __init__(self, blocked_phase):
        self.blocked_phase = blocked_phase
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.lock = threading.RLock()
        self.initialize_calls = 0
        self.register_calls = 0
        self.close_calls = 0
        self.close_threads = []
        self.closed = False
        self.close_before_startup_finished = False

    def _wait(self, phase):
        if phase == self.blocked_phase:
            self.entered.set()
            assert self.release.wait(10), "test did not release its thread gate"

    def initialize(self):
        with self.lock:
            self.initialize_calls += 1
            self._wait("initialize")
            self.closed = False
        if self.blocked_phase == "initialize":
            self.finished.set()

    def register_workspace(self, *_args, **_kwargs):
        with self.lock:
            self.register_calls += 1
            self._wait("register")
        if self.blocked_phase == "register":
            self.finished.set()

    def close(self):
        if self.blocked_phase in {"initialize", "register"}:
            self.close_before_startup_finished = not self.finished.is_set()
        with self.lock:
            self.close_calls += 1
            self.close_threads.append(threading.get_ident())
            self._wait("close")
            self.closed = True
        if self.blocked_phase == "close":
            self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initialize", "register"])
async def test_cancelled_start_retry_joins_the_live_initializer(app_module, phase):
    from backend.file_events import FileWatchManager

    store = _GatedStore(phase)
    manager = FileWatchManager(store)
    startup = asyncio.create_task(manager.start())
    retry = None
    try:
        assert await asyncio.to_thread(store.entered.wait, 3)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        retry = asyncio.create_task(manager.start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not retry.done(), "retry claimed readiness while startup I/O was live"
        assert manager._started is False
        assert manager._maintenance_task is None
        store.release.set()
        await asyncio.wait_for(retry, 3)
        assert store.finished.is_set()
        assert store.initialize_calls == 1
        assert store.register_calls > 0
        assert manager._started is True
        assert manager._maintenance_task is not None
    finally:
        store.release.set()
        await asyncio.gather(startup, *([retry] if retry else []), return_exceptions=True)
        assert await asyncio.to_thread(store.finished.wait, 3)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initialize", "register"])
async def test_shutdown_joins_cancelled_startup_without_blocking_the_loop(app_module, phase):
    from backend.file_events import FileWatchManager

    store = _GatedStore(phase)
    manager = FileWatchManager(store)
    startup = asyncio.create_task(manager.start())
    shutdown = duplicate = None
    # This releases the gate even when the buggy synchronous close blocks the
    # event loop. Assertions use gate order, not a machine-speed threshold.
    guard = threading.Timer(5, store.release.set)
    try:
        assert await asyncio.to_thread(store.entered.wait, 3)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        guard.start()
        shutdown = asyncio.create_task(manager.shutdown())
        pulse = asyncio.create_task(asyncio.sleep(0))
        await asyncio.wait_for(pulse, 3)
        assert not store.finished.is_set(), "shutdown blocked the loop until startup exited"
        assert store.close_calls == 0
        assert not shutdown.done()
        assert manager._started is False
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        duplicate = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert not duplicate.done()
        assert store.close_calls == 0
        store.release.set()
        await asyncio.wait_for(duplicate, 3)
        assert store.finished.is_set()
        assert store.close_before_startup_finished is False
        assert store.closed is True
        assert store.close_calls == 1
        assert manager._maintenance_task is None
        await manager.shutdown()
        assert store.close_calls == 1
    finally:
        guard.cancel()
        store.release.set()
        await asyncio.gather(
            startup,
            *([shutdown] if shutdown else []),
            *([duplicate] if duplicate else []),
            return_exceptions=True,
        )
        assert await asyncio.to_thread(store.finished.wait, 3)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_close_owned_and_restart_waits(app_module):
    from backend.file_events import FileWatchManager

    store = _GatedStore("close")
    manager = FileWatchManager(store)
    await manager.start()
    shutdown = duplicate = restart = None
    guard = threading.Timer(5, store.release.set)
    try:
        guard.start()
        shutdown = asyncio.create_task(manager.shutdown())
        assert await asyncio.to_thread(store.entered.wait, 3)
        assert not store.finished.is_set(), "close ran synchronously on the event loop"
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        duplicate = asyncio.create_task(manager.shutdown())
        restart = asyncio.create_task(manager.start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not duplicate.done()
        assert not restart.done()
        assert store.close_calls == 1
        assert store.initialize_calls == 1
        assert not store.closed
        store.release.set()
        await asyncio.wait_for(asyncio.gather(duplicate, restart), 3)
        assert store.finished.is_set()
        assert store.close_calls == 1
        assert store.close_threads != [threading.get_ident()]
        assert store.initialize_calls == 2
        assert manager._started is True
        assert not store.closed
    finally:
        guard.cancel()
        store.release.set()
        await asyncio.gather(
            *([shutdown] if shutdown else []),
            *([duplicate] if duplicate else []),
            *([restart] if restart else []),
            return_exceptions=True,
        )
        await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_start_can_retry_and_finish_registration(app_module, monkeypatch):
    from backend.file_events import FileWatchManager

    store = _GatedStore(None)
    original = store.initialize
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic initialization failure")
        original()

    monkeypatch.setattr(store, "initialize", fail_once)
    manager = FileWatchManager(store)
    try:
        with pytest.raises(OSError, match="synthetic initialization failure"):
            await manager.start()
        assert manager._started is False
        assert manager._maintenance_task is None
        await manager.start()
        assert attempts == 2
        assert store.register_calls > 0
        assert manager._started is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_concurrent_start_waiters_share_failure_then_retry(app_module, monkeypatch):
    from backend.file_events import FileWatchManager

    store = _GatedStore("initialize")
    original = store.initialize
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        original()
        if attempts == 1:
            raise OSError("synthetic shared initialization failure")

    monkeypatch.setattr(store, "initialize", fail_once)
    manager = FileWatchManager(store)
    first = asyncio.create_task(manager.start())
    second = None
    try:
        assert await asyncio.to_thread(store.entered.wait, 3)
        second = asyncio.create_task(manager.start())
        await asyncio.sleep(0)
        assert not second.done()
        store.release.set()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 3,
        )
        assert all(isinstance(result, OSError) for result in results)
        assert attempts == 1
        assert manager._started is False
        await manager.start()
        assert attempts == 2
        assert store.register_calls > 0
        assert manager._started is True
    finally:
        store.release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await manager.shutdown()
