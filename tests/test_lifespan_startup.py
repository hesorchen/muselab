"""Lifespan cleanup uses real file/terminal managers without SDK or service I/O."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def lifespan_runtime(app_module, temp_root, monkeypatch):
    from backend import chat, file_events, files, memory_client, runtime_lifecycle
    from backend import push, scheduler, sessions, terminal
    from backend.activity import activity
    from backend.todos import todos
    from backend.workspace_store import WorkspaceStore

    runtime = SimpleNamespace(
        main=app_module,
        calls=[],
        scheduler_task=None,
        startup_tasks=[],
        cleanup_entered=asyncio.Event(),
        initialize_entered=threading.Event(),
        release_initialize=threading.Event(),
        initialize_finished=threading.Event(),
        store=WorkspaceStore(temp_root),
        terminal=terminal.TerminalManager(),
    )
    runtime.watcher = file_events.FileWatchManager(runtime.store)
    runtime.original_initialize = runtime.store.initialize
    runtime.original_shutdown = runtime_lifecycle.shutdown_runtime
    original_store_close = runtime.store.close
    original_watch_start = runtime.watcher.start
    original_watch_shutdown = runtime.watcher.shutdown
    original_terminal_start = runtime.terminal.start
    original_terminal_shutdown = runtime.terminal.shutdown
    original_upload_cleanup = files.cleanup_pending_uploads

    def noop(*_args, **_kwargs):
        return 0

    async def async_noop(*_args, **_kwargs):
        return 0

    async def start_scheduler():
        runtime.calls.append("scheduler.start")
        runtime.scheduler_task = asyncio.create_task(asyncio.Event().wait())

    async def stop_scheduler():
        runtime.calls.append("scheduler.stop")
        if runtime.scheduler_task is not None:
            runtime.scheduler_task.cancel()
            await asyncio.gather(runtime.scheduler_task, return_exceptions=True)

    async def close_memory():
        runtime.calls.append("memory.close")

    async def close_chat():
        runtime.calls.append("chat.close")

    async def start_watcher():
        runtime.calls.append("file_watcher.start")
        await original_watch_start()

    async def stop_watcher():
        runtime.calls.append("file_watcher.shutdown")
        await original_watch_shutdown()

    async def start_terminal():
        runtime.calls.append("terminal.start")
        await original_terminal_start()

    async def stop_terminal():
        runtime.calls.append("terminal.shutdown")
        await original_terminal_shutdown()

    def close_store():
        runtime.calls.append("store.close")
        original_store_close()

    async def shutdown_runtime(*args, **kwargs):
        runtime.calls.append("shutdown_runtime")
        runtime.cleanup_entered.set()
        await runtime.original_shutdown(*args, **kwargs)

    async def cleanup_uploads():
        runtime.calls.append("uploads.cleanup")
        await original_upload_cleanup()

    def launch_background(coroutines):
        # Never run SDK history migrations or version-detection subprocesses.
        for coroutine in coroutines:
            coroutine.close()
        runtime.calls.append("background.start")
        task = asyncio.create_task(asyncio.Event().wait())
        app_module._BG_TASKS.add(task)
        task.add_done_callback(app_module._BG_TASKS.discard)

    for owner, method in (
        (sessions, "ensure_private_session_storage"),
        (chat, "ensure_private_attachment_storage"),
        (files, "ensure_private_trash_storage"),
        (activity, "initialize_runtime_state"),
        (todos, "initialize_runtime_state"),
        (activity, "reconcile_fork_sessions"),
        (sessions, "reconcile_runtime_task_overlay_chains"),
        (sessions, "stop_stale_runtime_task_overlays"),
        (chat, "recover_durable_queue_attachments_at_startup"),
    ):
        monkeypatch.setattr(owner, method, noop)
    monkeypatch.setattr(app_module, "_recover_message_queues_at_startup", async_noop)
    monkeypatch.setattr(chat, "recover_runtime_continuation_outboxes_at_startup", async_noop)
    monkeypatch.setattr(chat, "recover_native_cron_at_startup", async_noop)
    monkeypatch.setattr(sessions, "list_queue_session_ids", lambda: [])
    monkeypatch.setattr(chat, "shutdown_runtime", close_chat)
    monkeypatch.setattr(scheduler, "start_scheduler", start_scheduler)
    monkeypatch.setattr(scheduler, "stop_scheduler", stop_scheduler)
    monkeypatch.setattr(memory_client, "start", lambda: runtime.calls.append("memory.start"))
    monkeypatch.setattr(memory_client, "aclose", close_memory)
    monkeypatch.setattr(push, "init", noop)
    monkeypatch.setattr(app_module, "_launch_background_tasks", launch_background)
    monkeypatch.setattr(app_module, "start_diagnostics", lambda: runtime.calls.append("diagnostics.start"))
    monkeypatch.setattr(app_module, "stop_diagnostics", lambda: runtime.calls.append("diagnostics.stop"))
    monkeypatch.setattr(runtime_lifecycle, "shutdown_runtime", shutdown_runtime)
    monkeypatch.setattr(files, "cleanup_pending_uploads", cleanup_uploads)
    monkeypatch.setattr(file_events, "manager", runtime.watcher)
    monkeypatch.setattr(terminal, "manager", runtime.terminal)
    monkeypatch.setattr(runtime.watcher, "start", start_watcher)
    monkeypatch.setattr(runtime.watcher, "shutdown", stop_watcher)
    monkeypatch.setattr(runtime.store, "close", close_store)
    monkeypatch.setattr(runtime.terminal, "start", start_terminal)
    monkeypatch.setattr(runtime.terminal, "shutdown", stop_terminal)
    try:
        yield runtime
    finally:
        runtime.release_initialize.set()
        for task in runtime.startup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*runtime.startup_tasks, return_exceptions=True)
        await runtime.original_shutdown(
            app_module._BG_TASKS,
            scheduler=scheduler,
            memory=memory_client,
            terminal=runtime.terminal,
            file_watcher=runtime.watcher,
        )
        await original_upload_cleanup()


def _fail_upload_cleanup(runtime, monkeypatch):
    from backend import files

    failure = OSError("synthetic upload cleanup failure")

    async def fail_cleanup():
        runtime.calls.append("uploads.cleanup")
        raise failure

    monkeypatch.setattr(files, "cleanup_pending_uploads", fail_cleanup)
    return failure


def _assert_closed(runtime):
    assert runtime.calls.count("shutdown_runtime") == 1
    assert {"scheduler.stop", "file_watcher.shutdown", "terminal.shutdown", "chat.close", "memory.close", "uploads.cleanup", "diagnostics.stop"}.issubset(runtime.calls)
    assert not runtime.main._BG_TASKS
    assert runtime.scheduler_task is None or runtime.scheduler_task.done()
    assert runtime.watcher._startup_task is None
    assert runtime.watcher._maintenance_task is None
    assert runtime.store._ready is False
    assert runtime.terminal.reaper_task is None
    assert runtime.calls.index("scheduler.stop") < runtime.calls.index("file_watcher.shutdown")
    assert runtime.calls.index("uploads.cleanup") < runtime.calls.index("diagnostics.stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_cancelled_workspace_start_joins_store_owner_before_cleanup_finishes(
    lifespan_runtime, monkeypatch, cleanup_failure,
):
    runtime = lifespan_runtime

    def blocked_initialize():
        with runtime.store._lock:
            runtime.initialize_entered.set()
            assert runtime.release_initialize.wait(10)
            try:
                runtime.original_initialize()
            finally:
                runtime.initialize_finished.set()

    monkeypatch.setattr(runtime.store, "initialize", blocked_initialize)
    if cleanup_failure:
        _fail_upload_cleanup(runtime, monkeypatch)
    context = runtime.main._lifespan(runtime.main.app)
    startup = asyncio.create_task(context.__aenter__())
    runtime.startup_tasks.append(startup)
    assert await asyncio.to_thread(runtime.initialize_entered.wait, 3)
    startup.cancel()
    await asyncio.wait_for(runtime.cleanup_entered.wait(), 3)
    await asyncio.sleep(0)
    assert not startup.done()
    assert not runtime.initialize_finished.is_set()
    assert runtime.watcher._startup_task is not None
    assert not runtime.watcher._startup_task.done()
    assert "store.close" not in runtime.calls
    # The actual initializer holds its RLock; cleanup must still let the loop run.
    await asyncio.wait_for(asyncio.sleep(0), 3)
    runtime.release_initialize.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(startup, 3)
    assert runtime.initialize_finished.is_set()
    _assert_closed(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_terminal_start_failure_keeps_original_error_and_cleans(
    lifespan_runtime, monkeypatch, cleanup_failure,
):
    runtime = lifespan_runtime
    failure = RuntimeError("synthetic terminal startup failure")

    async def fail_start():
        runtime.calls.append("terminal.start")
        raise failure

    monkeypatch.setattr(runtime.terminal, "start", fail_start)
    if cleanup_failure:
        _fail_upload_cleanup(runtime, monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        async with runtime.main._lifespan(runtime.main.app):
            pytest.fail("startup failure must not yield a running application")
    assert raised.value is failure
    _assert_closed(runtime)


@pytest.mark.asyncio
async def test_normal_lifespan_preserves_startup_and_shutdown_order(lifespan_runtime):
    runtime = lifespan_runtime
    async with runtime.main._lifespan(runtime.main.app):
        assert runtime.calls == [
            "memory.start", "scheduler.start", "background.start",
            "file_watcher.start", "terminal.start", "diagnostics.start",
        ]
        assert runtime.watcher._started
        assert runtime.store._ready
        assert runtime.terminal.reaper_task is not None
        assert not runtime.terminal.reaper_task.done()
    _assert_closed(runtime)


@pytest.mark.asyncio
async def test_failure_before_services_start_cleans_unstarted_real_managers(
    lifespan_runtime, monkeypatch,
):
    from backend import sessions

    runtime = lifespan_runtime
    failure = OSError("synthetic preflight failure")

    def fail_preflight():
        raise failure

    monkeypatch.setattr(sessions, "ensure_private_session_storage", fail_preflight)
    with pytest.raises(OSError) as raised:
        async with runtime.main._lifespan(runtime.main.app):
            pytest.fail("failed preflight must not yield")
    assert raised.value is failure
    assert "scheduler.start" not in runtime.calls
    assert "file_watcher.start" not in runtime.calls
    assert "terminal.start" not in runtime.calls
    _assert_closed(runtime)
    # No start call is needed for repeated close of either actual manager.
    await runtime.watcher.shutdown()
    await runtime.terminal.shutdown()
    assert runtime.store._ready is False
    assert runtime.terminal.reaper_task is None


@pytest.mark.asyncio
async def test_normal_shutdown_reports_cleanup_error_after_stopping_diagnostics(
    lifespan_runtime, monkeypatch,
):
    runtime = lifespan_runtime
    failure = _fail_upload_cleanup(runtime, monkeypatch)
    with pytest.raises(OSError) as raised:
        async with runtime.main._lifespan(runtime.main.app):
            pass
    assert raised.value is failure
    _assert_closed(runtime)
