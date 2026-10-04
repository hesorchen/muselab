"""Exit delivery must survive a full, bounded terminal output queue."""

import asyncio
from types import SimpleNamespace

import pytest


class TerminalSocket:
    headers = {}

    def __init__(self, protocol, *, hold_first=False):
        self.scope = {"subprotocols": [protocol, "ticket.synthetic"]}
        self.frames = []
        self.closes = []
        self.receiving = asyncio.Event()
        self.first_write = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_first = hold_first

    async def accept(self, *, subprotocol):
        assert subprotocol == self.scope["subprotocols"][0]

    async def send_json(self, payload):
        self.frames.append(payload)

    async def send_bytes(self, payload):
        if not self.first_write.is_set():
            self.first_write.set()
            if self.hold_first:
                await self.release.wait()
        self.frames.append(payload)

    async def receive(self):
        self.receiving.set()
        await asyncio.Event().wait()

    async def close(self, *, code, reason=""):
        self.closes.append((code, reason))


@pytest.fixture
def terminal_runtime(app_module, monkeypatch, tmp_path):
    from backend import terminal

    manager = terminal.TerminalManager()
    session = terminal.TerminalSession(
        id="synthetic-exit", name="Exit fixture", workspace=tmp_path,
        shell="synthetic", profile_id="", profile_name="",
        process=SimpleNamespace(pid=0), created_at=0, last_activity=0,
    )

    async def ticket(terminal_id, _offered):
        assert terminal_id == session.id
        return session

    monkeypatch.setattr(manager, "consume_ticket", ticket)
    monkeypatch.setattr(terminal, "manager", manager)
    return terminal, manager, session


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [127, 128, 129])
async def test_exit_at_output_queue_capacity(terminal_runtime, queued):
    terminal, manager, session = terminal_runtime
    socket = TerminalSocket(terminal.PROTOCOL, hold_first=True)
    task = asyncio.create_task(terminal.terminal_websocket(socket, session.id))
    expected = [b"first\n", *[f"line-{i}\n".encode() for i in range(queued)]]
    try:
        await asyncio.wait_for(socket.receiving.wait(), 1)
        await manager._publish_output(session, expected[0])
        await asyncio.wait_for(socket.first_write.wait(), 1)
        for payload in expected[1:]:
            await manager._publish_output(session, payload)
        subscriber = next(iter(session.subscribers))
        if queued == 128:
            assert subscriber.queue.full()
        await manager._mark_exited(session, 7)
        socket.release.set()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert done, "terminal exit was lost after queued output drained"
        await task
        if queued <= 128:
            assert [item for item in socket.frames if isinstance(item, bytes)] == expected
            assert [item for item in socket.frames if isinstance(item, dict)
                    and item.get("type") == "exit"] == [{"type": "exit", "exit_code": 7}]
            assert socket.closes == [(1000, "")]
        else:
            assert socket.closes == [(1013, "terminal client too slow")]
        assert not session.subscribers
    finally:
        socket.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_full_slow_viewer_does_not_hold_healthy_viewer_exit(terminal_runtime):
    terminal, manager, session = terminal_runtime
    slow = TerminalSocket(terminal.PROTOCOL, hold_first=True)
    fast = TerminalSocket(terminal.PROTOCOL)
    slow_task = asyncio.create_task(terminal.terminal_websocket(slow, session.id))
    fast_task = asyncio.create_task(terminal.terminal_websocket(fast, session.id))
    expected = [b"first\n", *[f"line-{i}\n".encode() for i in range(128)]]
    try:
        await asyncio.wait_for(asyncio.gather(slow.receiving.wait(), fast.receiving.wait()), 1)
        await manager._publish_output(session, expected[0])
        await asyncio.wait_for(asyncio.gather(slow.first_write.wait(), fast.first_write.wait()), 1)
        for payload in expected[1:]:
            await manager._publish_output(session, payload)
        await asyncio.wait_for(manager._mark_exited(session, 23), .5)
        done, _ = await asyncio.wait({fast_task}, timeout=.5)
        assert done, "healthy viewer waited for another subscriber's queue"
        await fast_task
        assert fast.closes == [(1000, "")]
        assert [item for item in fast.frames if isinstance(item, bytes)] == expected
        assert fast.frames[-1] == {"type": "exit", "exit_code": 23}
        slow.release.set()
        done, _ = await asyncio.wait({slow_task}, timeout=1)
        assert done, "slow viewer lost the exit after its output was delivered"
        await slow_task
        assert [item for item in slow.frames if isinstance(item, bytes)] == expected
        assert slow.frames[-1] == {"type": "exit", "exit_code": 23}
        assert not session.subscribers
    finally:
        slow.release.set()
        for task in (slow_task, fast_task):
            task.cancel()
        await asyncio.gather(slow_task, fast_task, return_exceptions=True)
