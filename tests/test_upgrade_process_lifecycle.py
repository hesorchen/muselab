"""Upgrade subprocess ownership, using only disposable synthetic executables."""

import asyncio
import json
import os
import signal
import sys

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.skipif(os.name != "posix", reason="synthetic executables use POSIX shebangs")


@pytest.fixture
def upgrade_processes(app_module, tmp_path, monkeypatch):
    from backend import api_settings

    control = tmp_path / "control.json"
    ready = tmp_path / "ready.json"
    log = tmp_path / "invocations.jsonl"
    executable = tmp_path / "synthetic-command"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\nfrom pathlib import Path\n"
        f"control=json.loads(Path({str(control)!r}).read_text(encoding='utf-8'))\n"
        f"with Path({str(log)!r}).open('a',encoding='utf-8') as stream:\n"
        " stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "step=sys.argv[1]\nprint('synthetic '+step,flush=True)\n"
        "if step==control.get('hold'):\n"
        f" marker=Path({str(ready.with_suffix('.tmp'))!r})\n"
        " marker.write_text(json.dumps({'pid':os.getpid(),'step':step}),encoding='utf-8')\n"
        f" marker.replace({str(ready)!r})\n"
        " time.sleep(5)\n"
        "sys.exit(control.get('failure',{}).get(step,0))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    control.write_text("{}", encoding="utf-8")
    context = {
        "api": api_settings, "control": control, "ready": ready, "log": log,
        "processes": [], "hold_reap": False, "cancel_requested": False,
    }
    monkeypatch.setattr(api_settings, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(api_settings, "_locate_executable", lambda name: str(executable))
    monkeypatch.setattr(api_settings, "_current_versions", lambda: {"sdk": "1.0.0", "cli": "1.0.0"})
    monkeypatch.setattr(api_settings, "_restart_hint", lambda: "synthetic restart hint")
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert args[0] == str(executable)
        assert args[1:] in (
            ("lock", "--upgrade-package", "claude-agent-sdk"),
            ("sync", "--frozen"),
            ("install", "-g", "@anthropic-ai/claude-code@latest"),
        )
        assert kwargs.get("cwd") == (str(tmp_path) if args[1] != "install" else None)
        process = await real_spawn(*args, **kwargs)
        context["processes"].append(process)
        if context["hold_reap"]:
            real_wait = process.wait

            async def observed_wait():
                if context["cancel_requested"]:
                    context["reap_entered"].set()
                    await context["release_reap"].wait()
                return await real_wait()

            process.wait = observed_wait
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return context


async def _ready_process(context):
    async with asyncio.timeout(2):
        while not context["ready"].exists():
            await asyncio.sleep(0.001)
    record = json.loads(context["ready"].read_text(encoding="utf-8"))
    process = context["processes"][-1]
    assert record["pid"] == process.pid
    os.kill(process.pid, 0)
    return process


async def _cleanup(context, task=None):
    if context.get("release_reap"):
        context["release_reap"].set()
    for process in context["processes"]:
        if process.returncode is None:
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()  # Baseline shares our group; never signal that group.
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.communicate(), 2)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("step", ["lock", "sync", "install"])
def test_upgrade_cancel_reaps_each_command(upgrade_processes, step):
    context = upgrade_processes
    context["control"].write_text(json.dumps({"hold": step}), encoding="utf-8")

    async def scenario():
        api = context["api"]
        task = asyncio.create_task(api.trigger_upgrade(api.UpgradeReq(targets=["cli" if step == "install" else "sdk"])))
        try:
            process = await _ready_process(context)
            task.cancel("upgrade request cancelled")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, 2)
            assert cancelled.value.args == ("upgrade request cancelled",)
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
            assert not api._UPGRADE_LOCK.locked()
        finally:
            await _cleanup(context, task)

    asyncio.run(scenario())


def test_upgrade_repeated_cancel_waits_for_reaping_and_retains_lock(upgrade_processes):
    context = upgrade_processes
    context["hold_reap"] = True
    context["control"].write_text(json.dumps({"hold": "lock"}), encoding="utf-8")

    async def scenario():
        api = context["api"]
        context["reap_entered"] = asyncio.Event()
        context["release_reap"] = asyncio.Event()
        task = asyncio.create_task(api.trigger_upgrade(api.UpgradeReq(targets=["sdk"])))
        try:
            process = await _ready_process(context)
            context["cancel_requested"] = True
            task.cancel("first upgrade cancellation")
            await asyncio.wait_for(context["reap_entered"].wait(), 2)
            assert not task.done()
            assert api._UPGRADE_LOCK.locked()
            with pytest.raises(HTTPException) as busy:
                await api.trigger_upgrade(api.UpgradeReq(targets=[]))
            assert busy.value.status_code == 409
            task.cancel("second upgrade cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert api._UPGRADE_LOCK.locked()
            context["release_reap"].set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, 2)
            assert cancelled.value.args == ("first upgrade cancellation",)
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
            assert not api._UPGRADE_LOCK.locked()
        finally:
            await _cleanup(context, task)

    asyncio.run(scenario())


def test_upgrade_success_preserves_all_command_output(upgrade_processes, monkeypatch):
    context = upgrade_processes
    versions = iter([{"sdk": "1.0.0", "cli": "1.0.0"}, {"sdk": "2.0.0", "cli": "2.0.0"}])
    monkeypatch.setattr(context["api"], "_current_versions", lambda: next(versions))

    async def scenario():
        api = context["api"]
        try:
            result = await api.trigger_upgrade(api.UpgradeReq())
            assert result["ok"] and result["sdk_changed"] and result["cli_changed"]
            assert result["needs_restart"] and result["restart_hint"] == "synthetic restart hint"
            commands = [step for step in result["steps"] if "rc" in step]
            assert [step["output"] for step in commands] == ["synthetic lock\n", "synthetic sync\n", "synthetic install\n"]
            assert all(step["rc"] == 0 for step in commands)
            assert len(context["processes"]) == 3
            assert all(process.returncode == 0 for process in context["processes"])
            assert api._LAST_UPGRADE == result
        finally:
            await _cleanup(context)

    asyncio.run(scenario())


@pytest.mark.parametrize("step", ["lock", "sync", "install"])
def test_upgrade_nonzero_exit_preserves_response(upgrade_processes, step):
    context = upgrade_processes
    context["control"].write_text(json.dumps({"failure": {step: 7}}), encoding="utf-8")

    async def scenario():
        api = context["api"]
        try:
            result = await api.trigger_upgrade(api.UpgradeReq(targets=["cli" if step == "install" else "sdk"]))
            assert not result["ok"] and not result["needs_restart"]
            failed = next(item for item in result["steps"] if item.get("rc") == 7)
            assert failed["output"] == f"synthetic {step}\n"
            assert all(process.returncode is not None for process in context["processes"])
            assert len(context["processes"]) == (2 if step == "sync" else 1)
            assert api._LAST_UPGRADE == result
            if step != "install":
                assert result["steps"][-2]["step"] == "sdk upgrade aborted"
        finally:
            await _cleanup(context)

    asyncio.run(scenario())
