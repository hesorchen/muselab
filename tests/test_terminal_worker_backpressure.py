"""Real PTY input remains lossless while its foreground process is busy."""
from __future__ import annotations

import asyncio
import hashlib
import struct
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_count", [1, 6])
async def test_large_terminal_paste_waits_for_pty_reader(tmp_path, frame_count):
    from backend import terminal_worker

    payload = bytes(range(256)) * 256
    expected = hashlib.sha256(payload * frame_count).hexdigest().encode()
    child = tmp_path / "synthetic-shell"
    child.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, os, time, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'__READY__')\n"
        "time.sleep(0.25)\n"
        f"remaining = {len(payload) * frame_count}\n"
        "digest = hashlib.sha256()\n"
        "while remaining:\n"
        "    block = os.read(0, min(65536, remaining))\n"
        "    if not block:\n"
        "        raise RuntimeError('input ended early')\n"
        "    digest.update(block)\n"
        "    remaining -= len(block)\n"
        "os.write(1, b'__DIGEST__' + digest.hexdigest().encode())\n"
        # Let the broker drain the output before the login process exits.
        "time.sleep(0.05)\n",
    )
    child.chmod(0o700)
    worker = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(terminal_worker.__file__)),
        str(child), str(tmp_path), "24", "80",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    header = struct.Struct("!BI")
    output = bytearray()
    errors = []

    async def read_frame():
        kind, size = header.unpack(await worker.stdout.readexactly(header.size))
        return kind, await worker.stdout.readexactly(size)

    try:
        async with asyncio.timeout(5):
            while b"__READY__" not in output:
                kind, data = await read_frame()
                assert kind == terminal_worker.OUTPUT, data
                output.extend(data)
            for _ in range(frame_count):
                worker.stdin.write(header.pack(terminal_worker.INPUT, len(payload)) + payload)
                await worker.stdin.drain()
            while True:
                try:
                    kind, data = await read_frame()
                except asyncio.IncompleteReadError:
                    break
                if kind == terminal_worker.ERROR:
                    errors.append(data)
                elif kind == terminal_worker.OUTPUT:
                    output.extend(data)
                elif kind == terminal_worker.EXITED:
                    assert terminal_worker.EXIT.unpack(data)[0] == 0
                    break
            await worker.wait()
        assert errors == [], f"PTY rejected a valid paste: {errors!r}"
        assert b"__DIGEST__" + expected in output
        assert worker.returncode == 0
    finally:
        if worker.returncode is None:
            worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), 3)
            except asyncio.TimeoutError:
                worker.kill()
                await worker.wait()


@pytest.mark.asyncio
async def test_terminal_close_remains_bounded_with_saturated_input_pipe(
    app_module, monkeypatch, tmp_path,
):
    from backend import terminal

    child = tmp_path / "busy-synthetic-shell"
    child.write_text(
        f"#!{sys.executable}\n"
        "import os, time, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'__BUSY__')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child.chmod(0o700)
    monkeypatch.setattr(terminal, "_shell_path", lambda: str(child))
    manager = terminal.TerminalManager()
    session = await manager.create(tmp_path, terminal.TerminalCreate(profile_id=""))
    inputs = []
    try:
        async with asyncio.timeout(3):
            while b"__BUSY__" not in b"".join(session.buffer):
                await asyncio.sleep(0.005)
        inputs = [asyncio.create_task(manager.input(session, b"x" * 65536))
                  for _ in range(12)]
        async with asyncio.timeout(3):
            while session.process.stdin.transport.get_write_buffer_size() < 65536:
                await asyncio.sleep(0.005)
        assert any(not task.done() for task in inputs)
        await asyncio.wait_for(manager.close(session.id, tmp_path), 6)
        assert session.process.returncode is not None
        assert session.id not in manager.sessions
    finally:
        if session.process.returncode is None:
            session.process.terminate()
            await asyncio.wait_for(session.process.wait(), 3)
        await asyncio.gather(*inputs, return_exceptions=True)
        await manager.shutdown()
