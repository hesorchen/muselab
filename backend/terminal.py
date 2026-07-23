"""Authenticated multi-terminal manager backed by real Unix PTYs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import struct
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from .auth import require_token
from .settings import ROOT, atomic_write_text
from .workspaces import resolve_workspace_root


ENABLED = os.environ.get("MUSELAB_TERMINAL_ENABLED", "1").strip().lower() in {
    "1", "true", "yes", "on",
}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(value, high))


MAX_SESSIONS = _bounded_int("MUSELAB_TERMINAL_MAX_SESSIONS", 8, 1, 32)
BUFFER_BYTES = _bounded_int(
    "MUSELAB_TERMINAL_BUFFER_BYTES", 2 * 1024 * 1024, 64 * 1024, 16 * 1024 * 1024)
DETACHED_TTL = _bounded_int("MUSELAB_TERMINAL_DETACHED_TTL", 1800, 60, 86400)
EXITED_TTL = _bounded_int("MUSELAB_TERMINAL_EXITED_TTL", 3600, 60, 86400)
MAX_INPUT_BYTES = 64 * 1024
MAX_PROFILE_COMMAND_BYTES = 16 * 1024
TICKET_TTL = 30.0
PROTOCOL = "muselab-terminal-v1"

_HEADER = struct.Struct("!BI")
_SIZE = struct.Struct("!HH")
_EXIT = struct.Struct("!i")
_INPUT = 1
_RESIZE = 2
_TERMINATE = 3
_OUTPUT = 1
_EXITED = 2
_ERROR = 3

_SAFE_ENV_EXACT = {
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "TMP", "TEMP",
    "LANG", "LANGUAGE", "SSH_AUTH_SOCK",
}


def _terminal_env(shell: str, cwd: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in _SAFE_ENV_EXACT or key.startswith("LC_")
    }
    env.update({
        "SHELL": shell,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "PWD": str(cwd),
    })
    return env


def _shell_path() -> str:
    configured = os.environ.get("MUSELAB_TERMINAL_SHELL", "").strip()
    candidates = [configured, os.environ.get("SHELL", ""), "/bin/bash", "/bin/zsh", "/bin/sh"]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return str(Path(resolved).resolve())
    raise RuntimeError("no executable shell found")


class TerminalCreate(BaseModel):
    name: str = Field(default="", max_length=80)
    # None means "use the server default"; an explicit empty string means a
    # plain shell even when a default profile exists.
    profile_id: str | None = Field(default=None, max_length=64)
    rows: int = Field(default=24, ge=2, le=500)
    cols: int = Field(default=80, ge=2, le=1000)


class TerminalRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TerminalProfileWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=8192)
    is_default: bool = False


class TerminalProfileRegistry:
    """Small, process-local registry persisted under the primary archive root."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".muselab" / "terminal_profiles.json"
        self.lock = threading.RLock()
        self.profiles: list[dict[str, Any]] = []
        self.default_profile_id = ""
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = payload.get("profiles") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("id") or "").strip()
            name = str(row.get("name") or "").strip()
            command = str(row.get("command") or "").strip()
            if (not profile_id or profile_id in seen or len(profile_id) > 64
                    or not name or len(name) > 80 or not command
                    or len(command) > 8192):
                continue
            seen.add(profile_id)
            profiles.append({"id": profile_id, "name": name, "command": command})
        default_id = str(payload.get("default_profile_id") or "").strip()
        self.profiles = profiles
        self.default_profile_id = default_id if default_id in seen else ""

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps({
            "version": 1,
            "default_profile_id": self.default_profile_id,
            "profiles": self.profiles,
        }, ensure_ascii=False, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {**row, "is_default": row["id"] == self.default_profile_id}
                for row in self.profiles
            ]

    def get(self, profile_id: str | None, *, use_default: bool = False) -> dict[str, Any] | None:
        with self.lock:
            wanted = self.default_profile_id if use_default and profile_id is None else profile_id
            if not wanted:
                return None
            row = next((item for item in self.profiles if item["id"] == wanted), None)
            if row is None:
                raise HTTPException(404, "terminal profile not found")
            return {**row, "is_default": row["id"] == self.default_profile_id}

    def create(self, request: TerminalProfileWrite) -> dict[str, Any]:
        name = request.name.strip()
        command = request.command.strip()
        if not name or not command:
            raise HTTPException(422, "profile name and command are required")
        if len(command.encode("utf-8")) > MAX_PROFILE_COMMAND_BYTES:
            raise HTTPException(422, "profile command is too large")
        with self.lock:
            profile = {"id": str(uuid.uuid4()), "name": name, "command": command}
            self.profiles.append(profile)
            if request.is_default or len(self.profiles) == 1:
                self.default_profile_id = profile["id"]
            self._save()
            return {**profile, "is_default": profile["id"] == self.default_profile_id}

    def update(self, profile_id: str, request: TerminalProfileWrite) -> dict[str, Any]:
        name = request.name.strip()
        command = request.command.strip()
        if not name or not command:
            raise HTTPException(422, "profile name and command are required")
        if len(command.encode("utf-8")) > MAX_PROFILE_COMMAND_BYTES:
            raise HTTPException(422, "profile command is too large")
        with self.lock:
            index = next((i for i, row in enumerate(self.profiles)
                          if row["id"] == profile_id), -1)
            if index < 0:
                raise HTTPException(404, "terminal profile not found")
            profile = {"id": profile_id, "name": name, "command": command}
            self.profiles[index] = profile
            if request.is_default:
                self.default_profile_id = profile_id
            elif self.default_profile_id == profile_id:
                self.default_profile_id = ""
            self._save()
            return {**profile, "is_default": profile_id == self.default_profile_id}

    def delete(self, profile_id: str) -> None:
        with self.lock:
            if not any(row["id"] == profile_id for row in self.profiles):
                raise HTTPException(404, "terminal profile not found")
            self.profiles = [row for row in self.profiles if row["id"] != profile_id]
            if self.default_profile_id == profile_id:
                self.default_profile_id = self.profiles[0]["id"] if self.profiles else ""
            self._save()


profiles = TerminalProfileRegistry(ROOT)


@dataclass(eq=False)
class TerminalSubscriber:
    queue: asyncio.Queue[bytes | dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=128))


@dataclass
class TerminalSession:
    id: str
    name: str
    workspace: Path
    shell: str
    profile_id: str
    profile_name: str
    process: asyncio.subprocess.Process
    created_at: float
    last_activity: float
    status: str = "running"
    exit_code: int | None = None
    buffer: deque[bytes] = field(default_factory=deque)
    buffer_size: int = 0
    buffer_truncated: bool = False
    subscribers: set[TerminalSubscriber] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "workspace": str(self.workspace),
            "cwd": str(self.workspace),
            "shell": self.shell,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "pid": self.process.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "attached": len(self.subscribers),
            "buffer_truncated": self.buffer_truncated,
        }


class TerminalManager:
    def __init__(self) -> None:
        self.sessions: dict[str, TerminalSession] = {}
        self.tickets: dict[str, tuple[str, float]] = {}
        self.lock = asyncio.Lock()
        self.reaper_task: asyncio.Task | None = None

    def ensure_enabled(self) -> None:
        if not ENABLED:
            raise HTTPException(403, "terminal is disabled; set MUSELAB_TERMINAL_ENABLED=1")

    async def start(self) -> None:
        if ENABLED and (self.reaper_task is None or self.reaper_task.done()):
            self.reaper_task = asyncio.create_task(self._reaper())

    async def shutdown(self) -> None:
        if self.reaper_task:
            self.reaper_task.cancel()
            await asyncio.gather(self.reaper_task, return_exceptions=True)
            self.reaper_task = None
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.tickets.clear()
        await asyncio.gather(*(self._terminate_process(session) for session in sessions),
                             return_exceptions=True)

    async def create(self, workspace: Path, request: TerminalCreate) -> TerminalSession:
        self.ensure_enabled()
        await self.start()
        async with self.lock:
            live = sum(1 for session in self.sessions.values()
                       if session.status == "running")
            if live >= MAX_SESSIONS:
                raise HTTPException(409, f"terminal limit reached ({MAX_SESSIONS})")
        profile = profiles.get(request.profile_id, use_default=True)
        shell = _shell_path()
        worker = Path(__file__).with_name("terminal_worker.py")
        env = _terminal_env(shell, workspace)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(worker), shell, str(workspace),
            str(request.rows), str(request.cols),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        now = time.time()
        terminal_id = str(uuid.uuid4())
        default_name = f"Terminal {sum(1 for s in self.sessions.values() if s.workspace == workspace) + 1}"
        session = TerminalSession(
            id=terminal_id,
            name=request.name.strip() or (profile["name"] if profile else default_name),
            workspace=workspace,
            shell=shell,
            profile_id=profile["id"] if profile else "",
            profile_name=profile["name"] if profile else "",
            process=process,
            created_at=now,
            last_activity=now,
        )
        async with self.lock:
            self.sessions[terminal_id] = session
        session.reader_task = asyncio.create_task(self._read_worker(session))
        session.stderr_task = asyncio.create_task(self._read_stderr(session))
        if profile:
            command = profile["command"].encode("utf-8")
            if not command.endswith((b"\n", b"\r")):
                command += b"\n"
            await self._write_worker(session, _INPUT, command)
        return session

    async def list(self, workspace: Path) -> list[dict[str, Any]]:
        self.ensure_enabled()
        async with self.lock:
            rows = [session.public() for session in self.sessions.values()
                    if session.workspace == workspace]
        return sorted(rows, key=lambda row: row["created_at"])

    async def get(self, terminal_id: str, workspace: Path | None = None) -> TerminalSession:
        self.ensure_enabled()
        async with self.lock:
            session = self.sessions.get(terminal_id)
        if session is None or (workspace is not None and session.workspace != workspace):
            raise HTTPException(404, "terminal not found")
        return session

    async def rename(self, terminal_id: str, workspace: Path, name: str) -> TerminalSession:
        session = await self.get(terminal_id, workspace)
        session.name = name.strip()[:80]
        return session

    async def close(self, terminal_id: str, workspace: Path) -> None:
        session = await self.get(terminal_id, workspace)
        async with self.lock:
            self.sessions.pop(terminal_id, None)
            self.tickets = {
                digest: row for digest, row in self.tickets.items()
                if row[0] != terminal_id
            }
        await self._terminate_process(session)

    async def close_all(self, workspace: Path) -> int:
        self.ensure_enabled()
        async with self.lock:
            sessions = [session for session in self.sessions.values()
                        if session.workspace == workspace]
            ids = {session.id for session in sessions}
            for terminal_id in ids:
                self.sessions.pop(terminal_id, None)
            self.tickets = {
                digest: row for digest, row in self.tickets.items()
                if row[0] not in ids
            }
        await asyncio.gather(*(self._terminate_process(session) for session in sessions),
                             return_exceptions=True)
        return len(sessions)

    async def mint_ticket(self, terminal_id: str, workspace: Path) -> str:
        await self.get(terminal_id, workspace)
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = time.monotonic()
        async with self.lock:
            self._prune_tickets(now)
            self.tickets[digest] = (terminal_id, now + TICKET_TTL)
        return "ticket." + raw

    async def consume_ticket(self, terminal_id: str, offered: list[str]) -> TerminalSession | None:
        value = next((item for item in offered if item.startswith("ticket.")), "")
        if not value:
            return None
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        now = time.monotonic()
        async with self.lock:
            self._prune_tickets(now)
            row = self.tickets.pop(digest, None)
            session = self.sessions.get(terminal_id)
        if row is None or row[0] != terminal_id or row[1] < now:
            return None
        if session is None:
            return None
        return session

    async def attach(self, session: TerminalSession) -> tuple[TerminalSubscriber, bytes]:
        subscriber = TerminalSubscriber()
        async with session.lock:
            replay = b"".join(session.buffer)
            session.subscribers.add(subscriber)
        session.last_activity = time.time()
        return subscriber, replay

    async def detach(self, session: TerminalSession, subscriber: TerminalSubscriber) -> None:
        async with session.lock:
            session.subscribers.discard(subscriber)
        session.last_activity = time.time()

    async def input(self, session: TerminalSession, payload: bytes) -> None:
        if not payload or len(payload) > MAX_INPUT_BYTES or session.status != "running":
            return
        await self._write_worker(session, _INPUT, payload)
        session.last_activity = time.time()

    async def resize(self, session: TerminalSession, rows: int, cols: int) -> None:
        rows = max(2, min(int(rows), 500))
        cols = max(2, min(int(cols), 1000))
        await self._write_worker(session, _RESIZE, _SIZE.pack(rows, cols))
        session.last_activity = time.time()

    async def _write_worker(self, session: TerminalSession, kind: int,
                            payload: bytes = b"") -> None:
        writer = session.process.stdin
        if writer is None or writer.is_closing():
            return
        try:
            writer.write(_HEADER.pack(kind, len(payload)) + payload)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            return

    async def _read_worker(self, session: TerminalSession) -> None:
        reader = session.process.stdout
        if reader is None:
            return
        try:
            while True:
                header = await reader.readexactly(_HEADER.size)
                kind, length = _HEADER.unpack(header)
                if length > 256 * 1024:
                    raise RuntimeError("terminal worker sent oversized frame")
                payload = await reader.readexactly(length)
                if kind == _OUTPUT:
                    await self._publish_output(session, payload)
                elif kind == _EXITED:
                    code = _EXIT.unpack(payload)[0] if len(payload) == _EXIT.size else 1
                    await self._mark_exited(session, code)
                    return
                elif kind == _ERROR:
                    await self._publish_output(
                        session, b"\r\n\x1b[31mmuselab terminal error: "
                        + payload + b"\x1b[0m\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            if session.status == "running":
                code = await session.process.wait()
                await self._mark_exited(session, code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._publish_output(
                session, f"\r\n\x1b[31mterminal transport failed: {exc}\x1b[0m\r\n".encode())
            await self._mark_exited(session, 1)

    async def _read_stderr(self, session: TerminalSession) -> None:
        reader = session.process.stderr
        if reader is None:
            return
        try:
            data = await reader.read(16 * 1024)
            if data:
                await self._publish_output(
                    session, b"\r\n\x1b[31mterminal worker: "
                    + data[:4096] + b"\x1b[0m\r\n")
        except asyncio.CancelledError:
            raise

    async def _publish_output(self, session: TerminalSession, payload: bytes) -> None:
        if not payload:
            return
        session.last_activity = time.time()
        async with session.lock:
            session.buffer.append(payload)
            session.buffer_size += len(payload)
            while session.buffer_size > BUFFER_BYTES and session.buffer:
                dropped = session.buffer.popleft()
                session.buffer_size -= len(dropped)
                session.buffer_truncated = True
            subscribers = list(session.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.queue.put_nowait(payload)
            except asyncio.QueueFull:
                while not subscriber.queue.empty():
                    try:
                        subscriber.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    subscriber.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _mark_exited(self, session: TerminalSession, code: int) -> None:
        if session.status != "running":
            return
        session.status = "exited"
        session.exit_code = int(code)
        session.last_activity = time.time()
        message = {"type": "exit", "exit_code": session.exit_code}
        async with session.lock:
            subscribers = list(session.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def _terminate_process(self, session: TerminalSession) -> None:
        if session.status == "running":
            try:
                await self._write_worker(session, _TERMINATE)
            except Exception:
                pass
            try:
                await asyncio.wait_for(session.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                session.process.terminate()
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    session.process.kill()
                    await session.process.wait()
            session.status = "exited"
            session.exit_code = session.process.returncode
        for task in (session.reader_task, session.stderr_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.gather(*(
            task for task in (session.reader_task, session.stderr_task)
            if task and task is not asyncio.current_task()
        ), return_exceptions=True)

    def _prune_tickets(self, now: float) -> None:
        self.tickets = {
            digest: row for digest, row in self.tickets.items()
            if row[1] >= now
        }

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            async with self.lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                age = now - session.last_activity
                if session.status == "running" and not session.subscribers and age > DETACHED_TTL:
                    try:
                        await self.close(session.id, session.workspace)
                    except HTTPException:
                        pass
                elif session.status != "running" and age > EXITED_TTL:
                    async with self.lock:
                        self.sessions.pop(session.id, None)


manager = TerminalManager()
router = APIRouter(prefix="/api/terminals", tags=["terminals"])


@router.get("", dependencies=[Depends(require_token)])
async def list_terminals(workspace: Path = Depends(resolve_workspace_root)) -> dict[str, Any]:
    return {"terminals": await manager.list(workspace), "limits": {
        "max_sessions": MAX_SESSIONS,
        "buffer_bytes": BUFFER_BYTES,
        "detached_ttl": DETACHED_TTL,
    }, "profiles": profiles.list(), "default_profile_id": profiles.default_profile_id}


@router.post("", dependencies=[Depends(require_token)])
async def create_terminal(
    request: TerminalCreate,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    session = await manager.create(workspace, request)
    return session.public()


@router.get("/profiles", dependencies=[Depends(require_token)])
async def list_terminal_profiles() -> dict[str, Any]:
    return {
        "profiles": profiles.list(),
        "default_profile_id": profiles.default_profile_id,
    }


@router.post("/profiles", dependencies=[Depends(require_token)])
async def create_terminal_profile(request: TerminalProfileWrite) -> dict[str, Any]:
    return profiles.create(request)


@router.patch("/profiles/{profile_id}", dependencies=[Depends(require_token)])
async def update_terminal_profile(
    profile_id: str,
    request: TerminalProfileWrite,
) -> dict[str, Any]:
    return profiles.update(profile_id, request)


@router.delete("/profiles/{profile_id}", dependencies=[Depends(require_token)])
async def delete_terminal_profile(profile_id: str) -> dict[str, bool]:
    profiles.delete(profile_id)
    return {"ok": True}


@router.patch("/{terminal_id}", dependencies=[Depends(require_token)])
async def rename_terminal(
    terminal_id: str,
    request: TerminalRename,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    session = await manager.rename(terminal_id, workspace, request.name)
    return session.public()


@router.delete("/{terminal_id}", dependencies=[Depends(require_token)])
async def close_terminal(
    terminal_id: str,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict[str, bool]:
    await manager.close(terminal_id, workspace)
    return {"ok": True}


@router.post("/terminate-all", dependencies=[Depends(require_token)])
async def close_all_terminals(
    workspace: Path = Depends(resolve_workspace_root),
) -> dict[str, int]:
    return {"closed": await manager.close_all(workspace)}


@router.post("/{terminal_id}/ticket", dependencies=[Depends(require_token)])
async def terminal_ticket(
    terminal_id: str,
    workspace: Path = Depends(resolve_workspace_root),
) -> dict[str, Any]:
    ticket = await manager.mint_ticket(terminal_id, workspace)
    return {"ticket": ticket, "protocol": PROTOCOL, "expires_in": int(TICKET_TTL)}


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin", "")
    if not origin:
        return True
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    request_host = websocket.headers.get("host", "").lower()
    return bool(origin_host and request_host and origin_host == request_host)


@router.websocket("/{terminal_id}/ws")
async def terminal_websocket(websocket: WebSocket, terminal_id: str) -> None:
    if not ENABLED or not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    offered = list(websocket.scope.get("subprotocols") or [])
    session = await manager.consume_ticket(terminal_id, offered)
    if session is None or PROTOCOL not in offered:
        await websocket.close(code=1008)
        return

    await websocket.accept(subprotocol=PROTOCOL)
    subscriber, replay = await manager.attach(session)
    await websocket.send_json({
        "type": "ready",
        "terminal": session.public(),
        "replay_bytes": len(replay),
    })
    if session.buffer_truncated:
        await websocket.send_bytes(
            b"\x1b[33m[muselab: earlier terminal output was truncated]\x1b[0m\r\n")
    if replay:
        # Historical terminal output may contain device queries (DA/DSR/OSC).
        # Delimit replay explicitly so the browser can render it without
        # forwarding newly-generated xterm replies into today's foreground
        # process.
        await websocket.send_json({"type": "replay_start"})
        await websocket.send_bytes(replay)
        await websocket.send_json({"type": "replay_end"})
    if session.status != "running":
        await websocket.send_json({"type": "exit", "exit_code": session.exit_code})
        await websocket.close(code=1000)
        await manager.detach(session, subscriber)
        return

    async def send_loop() -> None:
        while True:
            item = await subscriber.queue.get()
            if item is None:
                await websocket.close(code=1013, reason="terminal client too slow")
                return
            if isinstance(item, bytes):
                await websocket.send_bytes(item)
            else:
                await websocket.send_json(item)
                if item.get("type") == "exit":
                    await websocket.close(code=1000)
                    return

    async def receive_loop() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("bytes")
            if payload is not None:
                await manager.input(session, payload)
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue
            if control.get("type") == "resize":
                try:
                    await manager.resize(
                        session, int(control.get("rows", 24)), int(control.get("cols", 80)))
                except (TypeError, ValueError):
                    continue
            elif control.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    sender = asyncio.create_task(send_loop())
    receiver = asyncio.create_task(receive_loop())
    try:
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await manager.detach(session, subscriber)
