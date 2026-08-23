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
# A failed or timed-out disconnect must retain the exact client object. A
# session-id-only failure bit cannot be retried after the pool entry has been
# removed, and permanently poisons every later get_client() call.
SESSION_DISCONNECT_CLIENTS: dict[
    str, dict[int, ClaudeSDKClient]
] = {}
CLIENT_DISCONNECT_OWNERS: dict[int, asyncio.Task] = {}
CLIENT_DISCONNECT_DEADLINE_S = 22.0
STREAM_EOF = object()


def creation_lock_for(key: ClientKey) -> asyncio.Lock:
    return CREATION_LOCKS.setdefault(key, asyncio.Lock())


async def disconnect_unpooled_client(
    client: ClaudeSDKClient,
    session_id: str,
) -> None:
    """Boundedly close a connected client that never entered the pool.

    Cancellation only releases the caller; the exact cleanup owner remains in
    the session registry. A later operation joins or retries it before a new
    runtime can be created.
    """
    if not await join_session_disconnects(session_id, (client,)):
        raise RuntimeCleanupTimeout(
            "unpooled session runtime cleanup did not finish; retry"
        )


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
            try:
                await hooks.disconnect_unpooled_client(client, session_id)
            except Exception as cleanup_exc:
                sys.stderr.write(
                    "[client-pool] MCP failure cleanup pending "
                    f"sid={session_id[:8]} "
                    f"exc={type(cleanup_exc).__name__}\n"
                )
                sys.stderr.flush()
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
            if not await join_session_disconnects(
                old_key[0], (old_client,)
            ):
                sys.stderr.write(
                    "[client-pool] evict cleanup pending "
                    f"sid={old_key[0][:8]}\n"
                )
                sys.stderr.flush()
                retry = asyncio.create_task(
                    _retry_pending_disconnects(old_key[0])
                )
                hooks.retain_detached_cleanup(retry)

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


async def _disconnect_owned_client(
    session_id: str,
    client_id: int,
    client: ClaudeSDKClient,
) -> None:
    """Run one disconnect attempt while retaining retriable ownership."""
    try:
        await client.disconnect()
    except BaseException:
        SESSION_DISCONNECT_FAILED.add(session_id)
        raise
    else:
        pending = SESSION_DISCONNECT_CLIENTS.get(session_id)
        if pending is not None:
            pending.pop(client_id, None)
            if not pending:
                SESSION_DISCONNECT_CLIENTS.pop(session_id, None)
                SESSION_DISCONNECT_FAILED.discard(session_id)
    finally:
        current = asyncio.current_task()
        if CLIENT_DISCONNECT_OWNERS.get(client_id) is current:
            CLIENT_DISCONNECT_OWNERS.pop(client_id, None)


def _queue_client_disconnect(
    session_id: str,
    client: ClaudeSDKClient,
) -> asyncio.Task:
    client_id = id(client)
    pending = SESSION_DISCONNECT_CLIENTS.setdefault(session_id, {})
    pending[client_id] = client
    owner = CLIENT_DISCONNECT_OWNERS.get(client_id)
    if owner is not None and not owner.done():
        return owner
    task = asyncio.create_task(
        _disconnect_owned_client(session_id, client_id, client)
    )
    CLIENT_DISCONNECT_OWNERS[client_id] = task
    track_session_disconnect(session_id, task)
    return task


async def join_session_disconnects(
    session_id: str,
    clients: Iterable[ClaudeSDKClient] = (),
    *,
    timeout: float = CLIENT_DISCONNECT_DEADLINE_S,
) -> bool:
    tasks = {
        task
        for task in SESSION_DISCONNECT_TASKS.get(session_id, set())
        if not task.done()
    }
    for client in {id(item): item for item in clients}.values():
        tasks.add(_queue_client_disconnect(session_id, client))
    # Failed attempts retain their clients. A later admission check with no
    # explicit client can therefore retry the exact subprocess.
    for client in tuple(
        SESSION_DISCONNECT_CLIENTS.get(session_id, {}).values()
    ):
        tasks.add(_queue_client_disconnect(session_id, client))
    if not tasks:
        return session_id not in SESSION_DISCONNECT_FAILED
    done, pending = await asyncio.wait(tasks, timeout=max(0.1, timeout))
    if done:
        await asyncio.gather(*done, return_exceptions=True)
    # The deadline exceeds the SDK's graceful -> TERM -> KILL contract. Once
    # exceeded, cancel the wedged attempt but retain its client for retry.
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.sleep(0)
    live = {
        task
        for task in SESSION_DISCONNECT_TASKS.get(session_id, set())
        if not task.done()
    }
    return not (
        pending
        or live
        or SESSION_DISCONNECT_CLIENTS.get(session_id)
        or session_id in SESSION_DISCONNECT_FAILED
    )


async def _retry_pending_disconnects(session_id: str) -> None:
    """Best-effort retry owner for LRU eviction, which has no HTTP caller."""
    for delay in (0.25, 1.0, 4.0):
        await asyncio.sleep(delay)
        if await join_session_disconnects(session_id):
            return
    sys.stderr.write(
        f"[client-pool] cleanup still pending sid={session_id[:8]}\n"
    )
    sys.stderr.flush()


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
    disconnect = getattr(client, "disconnect", None)
    if not callable(disconnect):
        raise RuntimeCleanupTimeout(
            "background task owner cannot confirm runtime cleanup"
        )
    if not await join_session_disconnects(session_id, (client,)):
        raise RuntimeCleanupTimeout(
            "background task runtime cleanup did not finish; retry"
        )


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
                shutdown_disconnects.add(
                    _queue_client_disconnect(session_id, client)
                )
        # Include LRU-evicted and never-pooled clients retained after an
        # earlier failed or timed-out attempt.
        for session_id, clients in tuple(
            SESSION_DISCONNECT_CLIENTS.items()
        ):
            for client in tuple(clients.values()):
                shutdown_disconnects.add(
                    _queue_client_disconnect(session_id, client)
                )
        CLIENTS.clear()
        CLIENT_PERMISSION.clear()
        CLIENT_PLAN_RETURN.clear()
        CREATION_LOCKS.clear()
        CLIENT_LRU.clear()
        hooks.pending_runtime_rebuilds.clear()

    owners = existing_disconnects | shutdown_disconnects
    if owners:
        done, pending = await asyncio.wait(
            owners, timeout=CLIENT_DISCONNECT_DEADLINE_S
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for task in pending:
            task.cancel()
            hooks.retain_detached_cleanup(task)
