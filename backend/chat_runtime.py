"""Claude SDK client pool, disconnect, and sole-stream lifecycle.

Canonical Claude CLI JSONL remains the transcript authority. This module owns
only in-process SDK runtime state: pooled clients, their one message-stream pump,
and bounded disconnect fences. It imports neither ``backend.chat`` nor turn
orchestration; application policy is supplied by callbacks configured by the
chat composition root.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import sys
from typing import Any, Callable, Collection, Iterable

from claude_agent_sdk import ClaudeSDKClient, ClaudeSDKError


ClientKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class RuntimeHooks:
    sessions: Any
    normalize_effort: Callable[[str], str]
    valid_efforts: Collection[str]
    valid_service_tiers: Collection[str]
    normalize_plan_return_permission: Callable[[str, str | None], str]
    build_and_connect_client: Callable[..., Any]
    has_enabled_external_mcp: Callable[[], bool]
    await_mcp_ready: Callable[..., Any]
    active_turns: dict[str, Any]
    sessions_with_inflight_tasks: dict[str, set[str]]
    session_has_live_watcher: Callable[[str], bool]
    pending_runtime_rebuilds: set[str]
    client_pool_cap: Callable[[], int]
    disconnect_unpooled_client: Callable[..., Any]
    disconnect_client: Callable[[str], Any]
    get_client: Callable[..., Any]
    ensure_session_stream: Callable[[ClientKey, ClaudeSDKClient], Any]
    join_session_disconnects: Callable[..., Any]
    evict_failed_session_stream: Callable[[Any], Any]
    retain_detached_cleanup: Callable[[asyncio.Task], None]


_hooks: RuntimeHooks | None = None


def configure_hooks(hooks: RuntimeHooks) -> None:
    global _hooks
    _hooks = hooks


def _require_hooks() -> RuntimeHooks:
    if _hooks is None:
        raise RuntimeError("chat runtime hooks are not configured")
    return _hooks


CLIENTS: dict[ClientKey, ClaudeSDKClient] = {}
CLIENT_PERMISSION: dict[ClientKey, str] = {}
CLIENT_PLAN_RETURN: dict[ClientKey, str] = {}
CLIENT_LRU: list[ClientKey] = []
CLIENT_LOCK = asyncio.Lock()
CREATION_LOCKS: dict[ClientKey, asyncio.Lock] = {}
SESSION_STREAMS: dict[ClientKey, "SessionStream"] = {}
SESSION_DISCONNECT_TASKS: dict[str, set[asyncio.Task]] = {}
SESSION_DISCONNECT_FAILED: set[str] = set()
CLIENT_DISCONNECT_DEADLINE_S = 22.0
STREAM_EOF = object()


def creation_lock_for(key: ClientKey) -> asyncio.Lock:
    return CREATION_LOCKS.setdefault(key, asyncio.Lock())


async def disconnect_unpooled_client(
    client: ClaudeSDKClient,
    session_id: str,
) -> None:
    """Boundedly close a connected client that never entered the pool."""

    async def _sdk_disconnect() -> None:
        try:
            # The SDK close path owns graceful -> TERM -> KILL escalation. A
            # shorter wait_for can cancel before that escalation completes.
            await client.disconnect()
        except Exception as exc:
            sys.stderr.write(
                f"[client-pool] unpooled {session_id[:8]} disconnect err: "
                f"{type(exc).__name__}\n"
            )

    cleanup = asyncio.create_task(_sdk_disconnect())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def get_client(
    session_id: str,
    model: str,
    permission: str = "bypassPermissions",
    effort: str = "",
    service_tier: str = "",
    plan_return_permission: str = "",
) -> ClaudeSDKClient:
    """Create or fetch one client for a session runtime key."""
    hooks = _require_hooks()
    sess = hooks.sessions
    if not await hooks.join_session_disconnects(session_id):
        raise RuntimeCleanupTimeout(
            "session runtime cleanup is still in progress"
        )
    if sess.session_is_deleting(session_id):
        raise RuntimeError("session is being deleted")
    if session_id in hooks.pending_runtime_rebuilds:
        await hooks.disconnect_client(session_id)

    effort = hooks.normalize_effort(effort)
    service_tier = (service_tier or "").strip()
    if effort not in hooks.valid_efforts:
        raise ValueError(f"invalid effort: {effort}")
    if service_tier not in hooks.valid_service_tiers:
        raise ValueError(f"invalid service tier: {service_tier}")
    key = (session_id, model, effort, service_tier)
    plan_return_permission = hooks.normalize_plan_return_permission(
        permission, plan_return_permission
    )

    async with CLIENT_LOCK:
        cached = CLIENTS.get(key)
        if cached is not None:
            if key in CLIENT_LRU:
                CLIENT_LRU.remove(key)
            CLIENT_LRU.append(key)
        cached_perm = CLIENT_PERMISSION.get(key) if cached is not None else None
        cached_plan_return = (
            CLIENT_PLAN_RETURN.get(key, "") if cached is not None else ""
        )

    if cached is not None:
        if sess.session_is_deleting(session_id):
            raise RuntimeError("session is being deleted")
        if (
            cached_perm != permission
            or (
                permission == "plan"
                and cached_plan_return != plan_return_permission
            )
        ):
            await hooks.disconnect_client(session_id)
            return await hooks.get_client(
                session_id,
                model,
                permission,
                effort=effort,
                service_tier=service_tier,
                plan_return_permission=plan_return_permission,
            )
        return cached

    async with creation_lock_for(key):
        async with CLIENT_LOCK:
            cached = CLIENTS.get(key)
            if cached is not None:
                if key in CLIENT_LRU:
                    CLIENT_LRU.remove(key)
                CLIENT_LRU.append(key)
            cached_perm = (
                CLIENT_PERMISSION.get(key) if cached is not None else None
            )
            cached_plan_return = (
                CLIENT_PLAN_RETURN.get(key, "") if cached is not None else ""
            )

        if cached is not None:
            if sess.session_is_deleting(session_id):
                raise RuntimeError("session is being deleted")
            if (
                cached_perm == permission
                and (
                    permission != "plan"
                    or cached_plan_return == plan_return_permission
                )
            ):
                return cached
            await hooks.disconnect_client(session_id)

        if permission == "plan":
            client = await hooks.build_and_connect_client(
                session_id,
                model,
                permission,
                effort,
                service_tier,
                plan_return_permission=plan_return_permission,
            )
        else:
            client = await hooks.build_and_connect_client(
                session_id, model, permission, effort, service_tier
            )

        try:
            if hooks.has_enabled_external_mcp():
                await hooks.await_mcp_ready(client)
        except BaseException:
            await hooks.disconnect_unpooled_client(client, session_id)
            raise

        to_disconnect: list[
            tuple[ClientKey, ClaudeSDKClient, SessionStream | None]
        ] = []
        reject_deleting = False
        async with CLIENT_LOCK:
            with sess.session_lifecycle_lock(session_id):
                reject_deleting = sess.session_is_deleting(session_id)
                if not reject_deleting:
                    CLIENTS[key] = client
                    CLIENT_PERMISSION[key] = permission
                    if permission == "plan":
                        CLIENT_PLAN_RETURN[key] = plan_return_permission
                    else:
                        CLIENT_PLAN_RETURN.pop(key, None)
                    hooks.ensure_session_stream(key, client)
                    CLIENT_LRU.append(key)
            while (
                not reject_deleting
                and len(CLIENT_LRU) > hooks.client_pool_cap()
            ):
                candidate_idx = None
                for index, candidate_key in enumerate(CLIENT_LRU):
                    if candidate_key == key:
                        continue
                    active = hooks.active_turns.get(candidate_key[0])
                    if active is not None and not active.done:
                        continue
                    if candidate_key[0] in hooks.sessions_with_inflight_tasks:
                        continue
                    if hooks.session_has_live_watcher(candidate_key[0]):
                        continue
                    candidate_idx = index
                    break
                if candidate_idx is None:
                    break
                old_key = CLIENT_LRU.pop(candidate_idx)
                old_client = CLIENTS.pop(old_key, None)
                CLIENT_PERMISSION.pop(old_key, None)
                CLIENT_PLAN_RETURN.pop(old_key, None)
                CREATION_LOCKS.pop(old_key, None)
                if old_client is not None:
                    old_stream = SESSION_STREAMS.pop(old_key, None)
                    to_disconnect.append((old_key, old_client, old_stream))

        if reject_deleting:
            await hooks.disconnect_unpooled_client(client, session_id)
            raise RuntimeError("session is being deleted")

        for old_key, old_client, old_stream in to_disconnect:
            if old_stream is not None:
                await old_stream.aclose()
            try:
                await old_client.disconnect()
            except Exception as exc:
                sys.stderr.write(
                    "[client-pool] evict disconnect failed "
                    f"sid={old_key[0][:8]} exc={type(exc).__name__}\n"
                )
                sys.stderr.flush()

        return client


class SessionStream:
    """The sole reader and router for one SDK client's message stream."""

    _ORPHAN_MAX = 512

    def __init__(self, key: ClientKey, client: ClaudeSDKClient):
        self.key = key
        self.client = client
        self._turn: asyncio.Queue | None = None
        self._background: asyncio.Queue | None = None
        self._orphans: deque = deque(maxlen=self._ORPHAN_MAX)
        self._closed = False
        self._failure: Exception | None = None
        self.task: asyncio.Task = asyncio.create_task(self._pump())

    def _adopt_orphans(self, queue: asyncio.Queue) -> None:
        while self._orphans:
            queue.put_nowait(self._orphans.popleft())

    def attach_turn(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._turn = queue
        return queue

    def detach_turn(self, queue: asyncio.Queue) -> None:
        if self._turn is queue:
            self._turn = None

    def park_unconsumed(self, queue: asyncio.Queue) -> None:
        while True:
            try:
                message = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if message is STREAM_EOF:
                continue
            self._orphans.append(message)

    def attach_background(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._background = queue
        self._adopt_orphans(queue)
        return queue

    def detach_background(self, queue: asyncio.Queue) -> None:
        if self._background is queue:
            self._background = None

    async def _pump(self) -> None:
        try:
            async for message in self.client.receive_messages():
                if self._closed:
                    break
                queue = self._turn or self._background
                if queue is not None:
                    queue.put_nowait(message)
                else:
                    self._orphans.append(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc
            sys.stderr.write(
                f"[chat] session stream ended sid={self.key[0][:8]} "
                f"exc={type(exc).__name__}\n"
            )
            sys.stderr.flush()
        finally:
            if not self._closed and self._failure is None:
                self._failure = ClaudeSDKError(
                    "SDK message stream ended before the session was closed"
                )
            self._closed = True
            for queue in (self._turn, self._background):
                if queue is not None:
                    queue.put_nowait(STREAM_EOF)
            if self._failure is not None:
                await _require_hooks().evict_failed_session_stream(self)

    async def aclose(self) -> None:
        self._closed = True
        if not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.task


def ensure_session_stream(
    key: ClientKey,
    client: ClaudeSDKClient,
) -> SessionStream:
    stream = SESSION_STREAMS.get(key)
    if stream is not None and not stream._closed and stream.client is client:
        return stream
    if stream is not None:
        stream._closed = True
    stream = SessionStream(key, client)
    SESSION_STREAMS[key] = stream
    return stream


def stream_for(client: ClaudeSDKClient) -> SessionStream | None:
    for stream in SESSION_STREAMS.values():
        if stream.client is client and not stream._closed:
            return stream
    return None


async def evict_failed_session_stream(stream: SessionStream) -> None:
    key = stream.key
    client = stream.client
    async with CLIENT_LOCK:
        if CLIENTS.get(key) is client:
            CLIENTS.pop(key, None)
            CLIENT_PERMISSION.pop(key, None)
            CLIENT_PLAN_RETURN.pop(key, None)
            if key in CLIENT_LRU:
                CLIENT_LRU.remove(key)
        if SESSION_STREAMS.get(key) is stream:
            SESSION_STREAMS.pop(key, None)
    try:
        if not await _require_hooks().join_session_disconnects(key[0], (client,)):
            raise RuntimeCleanupTimeout(
                "failed session stream cleanup is still in progress"
            )
    except Exception as exc:
        sys.stderr.write(
            f"[chat] failed-stream disconnect sid={key[0][:8]} "
            f"exc={type(exc).__name__}\n"
        )
        sys.stderr.flush()


async def drop_session_streams(session_id: str) -> None:
    streams = []
    for key in [key for key in SESSION_STREAMS if key[0] == session_id]:
        stream = SESSION_STREAMS.pop(key, None)
        if stream is not None:
            streams.append(stream)
    if not streams:
        return
    tasks = {asyncio.create_task(stream.aclose()) for stream in streams}
    done, pending = await asyncio.wait(tasks, timeout=1.0)
    if done:
        await asyncio.gather(*done, return_exceptions=True)
    for task in pending:
        task.cancel()
        _require_hooks().retain_detached_cleanup(task)


class RuntimeCleanupTimeout(RuntimeError):
    """A CLI cleanup did not finish before the public operation's deadline."""


def track_session_disconnect(session_id: str, task: asyncio.Task) -> None:
    owners = SESSION_DISCONNECT_TASKS.setdefault(session_id, set())
    owners.add(task)

    def _done(done: asyncio.Task) -> None:
        current = SESSION_DISCONNECT_TASKS.get(session_id)
        if current is not None:
            current.discard(done)
            if not current:
                SESSION_DISCONNECT_TASKS.pop(session_id, None)
        if done.cancelled():
            SESSION_DISCONNECT_FAILED.add(session_id)
            return
        try:
            error = done.exception()
        except Exception as exc:
            error = exc
        if error is not None:
            SESSION_DISCONNECT_FAILED.add(session_id)

    task.add_done_callback(_done)


async def join_session_disconnects(
    session_id: str,
    clients: Iterable[ClaudeSDKClient] = (),
    *,
    timeout: float = CLIENT_DISCONNECT_DEADLINE_S,
) -> bool:
    if session_id in SESSION_DISCONNECT_FAILED:
        return False
    tasks = {
        task
        for task in SESSION_DISCONNECT_TASKS.get(session_id, set())
        if not task.done()
    }
    for client in {id(item): item for item in clients}.values():
        task = asyncio.create_task(client.disconnect())
        track_session_disconnect(session_id, task)
        tasks.add(task)
    if not tasks:
        return True
    done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
    if done:
        await asyncio.gather(*done, return_exceptions=True)
    return not pending and session_id not in SESSION_DISCONNECT_FAILED


async def disconnect_client(session_id: str) -> None:
    hooks = _require_hooks()
    to_disconnect: list[ClaudeSDKClient] = []
    hooks.pending_runtime_rebuilds.discard(session_id)
    await drop_session_streams(session_id)
    async with CLIENT_LOCK:
        keys = [key for key in CLIENTS if key[0] == session_id]
        for key in keys:
            client = CLIENTS.pop(key, None)
            CLIENT_PERMISSION.pop(key, None)
            CLIENT_PLAN_RETURN.pop(key, None)
            CREATION_LOCKS.pop(key, None)
            if key in CLIENT_LRU:
                CLIENT_LRU.remove(key)
            if client is not None:
                to_disconnect.append(client)
    if not await hooks.join_session_disconnects(session_id, to_disconnect):
        raise RuntimeCleanupTimeout(
            "session runtime cleanup did not finish; retry the operation"
        )


async def disconnect_background_task_owner(
    session_id: str,
    client: ClaudeSDKClient,
) -> None:
    hooks = _require_hooks()
    async with CLIENT_LOCK:
        pooled = any(
            key[0] == session_id and candidate is client
            for key, candidate in CLIENTS.items()
        )
    if pooled:
        await hooks.disconnect_client(session_id)
        return
    existing = set(SESSION_DISCONNECT_TASKS.get(session_id, set()))
    if existing:
        done, pending = await asyncio.wait(
            existing, timeout=CLIENT_DISCONNECT_DEADLINE_S
        )
        if pending:
            raise RuntimeCleanupTimeout(
                "background task runtime cleanup is still in progress"
            )
        results = await asyncio.gather(*done, return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeCleanupTimeout(
                "background task runtime cleanup failed"
            )
        SESSION_DISCONNECT_FAILED.discard(session_id)
        return
    disconnect = getattr(client, "disconnect", None)
    if not callable(disconnect):
        raise RuntimeCleanupTimeout(
            "background task owner cannot confirm runtime cleanup"
        )
    cleanup = asyncio.create_task(disconnect())
    track_session_disconnect(session_id, cleanup)
    done, pending = await asyncio.wait(
        {cleanup}, timeout=CLIENT_DISCONNECT_DEADLINE_S
    )
    if pending:
        raise RuntimeCleanupTimeout(
            "background task runtime cleanup did not finish"
        )
    result = (await asyncio.gather(*done, return_exceptions=True))[0]
    if isinstance(result, BaseException):
        raise RuntimeCleanupTimeout("background task runtime cleanup failed")
    SESSION_DISCONNECT_FAILED.discard(session_id)


async def shutdown_clients() -> None:
    """Stop every stream and disconnect every pooled SDK client."""
    hooks = _require_hooks()
    streams = list(SESSION_STREAMS.values())
    SESSION_STREAMS.clear()
    if streams:
        close_tasks = {
            asyncio.create_task(stream.aclose()) for stream in streams
        }
        done, pending = await asyncio.wait(close_tasks, timeout=1.0)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for task in pending:
            task.cancel()
            hooks.retain_detached_cleanup(task)

    existing_disconnects = {
        task
        for owners in SESSION_DISCONNECT_TASKS.values()
        for task in owners
        if not task.done()
    }
    async with CLIENT_LOCK:
        clients_by_session: dict[str, list[ClaudeSDKClient]] = {}
        seen_clients: set[int] = set()
        for key, client in CLIENTS.items():
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            clients_by_session.setdefault(key[0], []).append(client)
        shutdown_disconnects: set[asyncio.Task] = set()
        for session_id, clients in clients_by_session.items():
            for client in clients:
                task = asyncio.create_task(client.disconnect())
                track_session_disconnect(session_id, task)
                shutdown_disconnects.add(task)
        CLIENTS.clear()
        CLIENT_PERMISSION.clear()
        CLIENT_PLAN_RETURN.clear()
        CREATION_LOCKS.clear()
        CLIENT_LRU.clear()
        hooks.pending_runtime_rebuilds.clear()

    owners = existing_disconnects | shutdown_disconnects
    if owners:
        done, _pending = await asyncio.wait(owners, timeout=4.0)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
