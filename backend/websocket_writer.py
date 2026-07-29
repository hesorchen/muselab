"""Serialized, close-aware writes for Starlette WebSockets."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class WebSocketWriter:
    """Guarantee that no coroutine writes after this socket starts closing."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.closed = False
        self._lock = asyncio.Lock()

    def mark_closed(self) -> None:
        self.closed = True

    async def send_json(self, payload: dict[str, Any]) -> bool:
        return await self._send(self.websocket.send_json, payload)

    async def send_bytes(self, payload: bytes) -> bool:
        return await self._send(self.websocket.send_bytes, payload)

    async def _send(self, operation, payload) -> bool:
        async with self._lock:
            if self.closed:
                return False
            try:
                await operation(payload)
                return True
            except (RuntimeError, WebSocketDisconnect):
                self.closed = True
                return False

    async def close(self, code: int, reason: str = "") -> None:
        async with self._lock:
            if self.closed:
                return
            self.closed = True
            try:
                await self.websocket.close(code=code, reason=reason)
            except (RuntimeError, WebSocketDisconnect):
                pass
