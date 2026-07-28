"""WebSocket single-writer close-race regressions."""

import asyncio

from backend.websocket_writer import WebSocketWriter


def test_writer_never_sends_after_close_begins():
    events: list[tuple] = []

    class Socket:
        async def send_json(self, payload):
            events.append(("json", payload))

        async def send_bytes(self, payload):
            events.append(("bytes", payload))

        async def close(self, *, code, reason):
            events.append(("close", code, reason))

    async def scenario():
        writer = WebSocketWriter(Socket())
        assert await writer.send_json({"type": "ready"}) is True
        await writer.close(1000)
        assert await writer.send_json({"type": "pong"}) is False
        assert await writer.send_bytes(b"late") is False

    asyncio.run(scenario())
    assert events == [
        ("json", {"type": "ready"}),
        ("close", 1000, ""),
    ]


def test_writer_turns_runtime_send_failure_into_closed_state():
    class Socket:
        async def send_json(self, _payload):
            raise RuntimeError("websocket already closed")

    async def scenario():
        writer = WebSocketWriter(Socket())
        assert await writer.send_json({"type": "pong"}) is False
        assert writer.closed is True

    asyncio.run(scenario())
