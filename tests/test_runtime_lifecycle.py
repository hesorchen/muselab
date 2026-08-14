"""Process lifecycle and deployment command regressions."""

import asyncio
import logging
import platform


def test_restart_command_uses_explicit_systemd_unit(app_module, monkeypatch):
    from backend import api_settings

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("MUSELAB_SERVICE_NAME", "muselab-main.service")
    assert api_settings._restart_command() == [
        "systemctl", "--user", "--no-block", "restart", "muselab-main.service"]
    assert api_settings._restart_hint() == \
        "systemctl --user --no-block restart muselab-main.service"


def test_restart_command_rejects_shell_metacharacters(app_module, monkeypatch):
    from backend import api_settings

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv(
        "MUSELAB_SERVICE_NAME",
        "muselab-main.service; touch /tmp/should-not-exist",
    )
    assert api_settings._restart_command() == []


def test_restart_dispatch_is_non_blocking(app_module, monkeypatch):
    from backend import api_settings

    calls = []

    async def no_delay(_seconds):
        return None

    def popen(command, **options):
        calls.append((command, options))
        return object()

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(
        api_settings,
        "_restart_command",
        lambda: ["systemctl", "--user", "--no-block", "restart", "muselab.service"],
    )
    monkeypatch.setattr(api_settings.subprocess, "Popen", popen)

    asyncio.run(api_settings._do_restart())

    assert calls[0][0][-2:] == ["restart", "muselab.service"]
    assert calls[0][1]["start_new_session"] is True


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", path, "1.1", status),
        None,
    )


def test_access_log_filter_redacts_every_query_value_and_uuid(app_module):
    session_id = "123e4567-e89b-42d3-a456-426614174000"
    record = _access_record(
        f"/api/chat/sessions/{session_id}?ticket=once-secret&token=reusable-secret"
        "&path=private%2Fnotes.md&q=secret+search&bad%20name=also-secret"
    )

    assert app_module._TokenFilter().filter(record) is False
    target = record.args[2]
    assert target == (
        "/api/chat/sessions/:id?ticket=***&token=***&path=***&q=***&param=***"
    )
    assert "once-secret" not in target
    assert "reusable-secret" not in target
    assert session_id not in target
    assert "private" not in target


def test_access_log_filter_drops_success_error_and_poll_records(app_module):
    successful_poll = _access_record(
        "/api/chat/sessions?limit=15&ids=private", status=304)
    failed_poll = _access_record(
        "/api/chat/sessions?limit=15&ids=private", status=503)
    ordinary = _access_record("/api/chat/sessions/not-a-uuid?full=true", status=200)

    assert app_module._TokenFilter().filter(successful_poll) is False
    assert successful_poll.args[2] == "/api/chat/sessions?limit=***&ids=***"
    assert app_module._TokenFilter().filter(failed_poll) is False
    assert failed_poll.args[2] == "/api/chat/sessions?limit=***&ids=***"
    assert app_module._TokenFilter().filter(ordinary) is False


def test_access_log_filter_fails_closed_on_unknown_record_shape(app_module):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "GET /legacy?prompt=private-content",
        (),
        None,
    )

    assert app_module._TokenFilter().filter(record) is False
    assert record.getMessage() == "access event suppressed: unsupported log shape"
    assert "private-content" not in record.getMessage()


def test_startup_config_banner_does_not_log_root_path(
    app_module, monkeypatch, capsys,
):
    private_root = "/private/workspace/customer-project"
    monkeypatch.setenv("MUSELAB_ROOT", private_root)
    capsys.readouterr()

    app_module._startup_config_banner()

    stderr = capsys.readouterr().err
    assert "root_configured=true" in stderr
    assert private_root not in stderr


def test_scheduler_shutdown_cancels_loop_and_runs(app_module):
    from backend import scheduler

    async def scenario():
        blocker = asyncio.Event()
        scheduler._scheduler_task = asyncio.create_task(blocker.wait())
        run = scheduler._track_task(asyncio.create_task(blocker.wait()))
        await scheduler.stop_scheduler()
        assert scheduler._scheduler_task is None
        assert scheduler._RUN_TASKS == set()
        assert run.cancelled()

    asyncio.run(scenario())


def test_chat_shutdown_disconnects_each_client_once(app_module):
    from backend import chat

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def disconnect(self):
            self.calls += 1

    async def scenario():
        client = FakeClient()
        chat._clients[("session", "model-a", "")] = client
        chat._clients[("session", "model-b", "")] = client
        await chat.shutdown_runtime()
        assert client.calls == 1
        assert chat._clients == {}
        assert chat._session_streams == {}

    asyncio.run(scenario())


def test_workspace_index_start_is_fail_soft(app_module, capsys):
    class BrokenIndex:
        async def start(self):
            raise RuntimeError("synthetic index failure")

    assert asyncio.run(
        app_module._start_workspace_index(BrokenIndex())
    ) is False
    assert "continuing with chat/terminal" in capsys.readouterr().err


def test_runtime_shutdown_cancels_background_and_drains_services(
    app_module,
    monkeypatch,
):
    from backend import chat, runtime_lifecycle

    calls: list[str] = []

    class Service:
        def __init__(self, name: str, method: str):
            setattr(self, method, self.stop)
            self.name = name

        async def stop(self):
            calls.append(self.name)

    async def scenario():
        blocker = asyncio.Event()
        background = asyncio.create_task(blocker.wait())
        tasks = {background}

        async def stop_chat():
            calls.append("chat")

        monkeypatch.setattr(chat, "shutdown_runtime", stop_chat)
        await runtime_lifecycle.shutdown_runtime(
            tasks,
            scheduler=Service("scheduler", "stop_scheduler"),
            memory=Service("memory", "aclose"),
            terminal=Service("terminal", "shutdown"),
            file_watcher=Service("file watcher", "shutdown"),
        )
        assert background.cancelled()
        assert tasks == set()
        assert set(calls) == {
            "scheduler", "memory", "terminal", "file watcher", "chat",
        }

    asyncio.run(scenario())


def test_project_version_matches_pyproject(app_module):
    from backend.version import project_version

    assert project_version() == "1.2.0"
    assert app_module.app.version == project_version()
    assert app_module.GRACEFUL_SHUTDOWN_TIMEOUT == 3
