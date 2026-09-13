"""Engine-owned HTTP connections with per-request budgets and safe timings."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from http.cookiejar import CookieJar, DefaultCookiePolicy
import time

import httpx

from .observability import perf_event

_transport = ContextVar("memory_http_transport", default=None)


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class MemoryHTTPTransport:
    """Reuse transport, never headers/cookies, across an engine's adapters."""

    def __init__(self):
        self._clients = {}
        self._users = {}
        self._closing = set()

    def reopen(self):
        try:
            self._closing.discard(asyncio.get_running_loop())
        except RuntimeError:
            pass

    @asynccontextmanager
    async def lease(self):
        loop = asyncio.get_running_loop()
        if loop in self._closing:
            raise RuntimeError("memory transport is closing")
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=None,
                cookies=CookieJar(policy=_NoCookies()),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8,
                                    keepalive_expiry=30))
            self._clients[loop] = client
        task = asyncio.current_task()
        users = self._users.setdefault(loop, Counter())
        users[task] += 1
        try:
            yield client
        finally:
            users[task] -= 1
            if not users[task]:
                users.pop(task, None)

    async def close(self):
        loop = asyncio.get_running_loop()
        self._closing.add(loop)
        users = self._users.get(loop, {})
        pending = [task for task in users if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        client = self._clients.pop(loop, None)
        self._users.pop(loop, None)
        if client is not None:
            await client.aclose()


@contextmanager
def provider_transport_scope(transport):
    token = _transport.set(transport)
    try:
        yield
    finally:
        _transport.reset(token)


class _BudgetedClient:
    def __init__(self, client, timeout, operation):
        self.client, self.timeout, self.operation = client, timeout, operation

    def __getattr__(self, method):
        if method not in {"get", "post", "put", "patch", "delete", "request"}:
            raise AttributeError(method)
        async def request(*args, **kwargs):
            started = time.perf_counter()
            phases, starts = {}, {}
            async def trace(name, _info):
                # Do not retain trace payloads: they include URLs and headers.
                phase, _, action = name.rpartition(".")
                if action == "started":
                    starts[phase] = time.perf_counter()
                elif action in {"complete", "failed"} and phase in starts:
                    phases[phase] = phases.get(phase, 0) + time.perf_counter() - starts.pop(phase)
            kwargs["timeout"] = httpx.Timeout(self.timeout)
            kwargs["extensions"] = {**kwargs.get("extensions", {}), "trace": trace}
            status, status_code = "ok", 0
            try:
                response = await getattr(self.client, method)(*args, **kwargs)
                status_code = response.status_code
                if status_code >= 400:
                    status = "error"
                return response
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                if elapsed >= 50 or status != "ok":
                    now = time.perf_counter()
                    for phase, at in starts.items():
                        phases[phase] = phases.get(phase, 0) + now - at
                    def duration(suffix):
                        return round(sum(value for name, value in phases.items()
                                         if name.endswith(suffix)) * 1000, 1)
                    perf_event("memory.http", operation=self.operation,
                        status=status, status_code=status_code, duration_ms=round(elapsed, 1),
                        connect_ms=duration("connect_tcp"), tls_ms=duration("start_tls"),
                        response_wait_ms=duration("receive_response_headers"))
        return request


@asynccontextmanager
async def provider_http_client(timeout, *, operation):
    transport = _transport.get()
    if transport is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            yield client
    else:
        async with transport.lease() as client:
            yield _BudgetedClient(client, timeout, operation)
