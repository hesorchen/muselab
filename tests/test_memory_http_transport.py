import asyncio

import httpx
import pytest

from backend.memory_http import MemoryHTTPTransport, provider_http_client, provider_transport_scope


def test_reuses_connections_without_sharing_auth_cookies_or_request_deadlines(monkeypatch):
    made, seen = [], []
    async def handler(request):
        seen.append((request.headers.get("authorization"), request.headers.get("cookie"),
                     request.extensions["timeout"]["read"]))
        return httpx.Response(200, json={}, headers={"set-cookie": "fixture=private; Path=/"})
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        made.append(client)
        return client
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    async def run():
        transport = MemoryHTTPTransport()
        try:
            with provider_transport_scope(transport):
                for credential, timeout in [("fixture-a", 1), ("fixture-b", None), ("fixture-a", 3)]:
                    async with provider_http_client(timeout, operation="embedding") as client:
                        await client.post("https://memory.fixture/embeddings",
                            headers={"authorization": credential}, json={})
            assert len(made) == 1
            assert seen == [("fixture-a", None, 1), ("fixture-b", None, None), ("fixture-a", None, 3)]
        finally:
            await transport.close()
        assert made[0].is_closed and not transport._users
    asyncio.run(run())


def test_shutdown_cancels_and_joins_active_transport_users(monkeypatch):
    entered = asyncio.Event()
    async def handler(_request):
        entered.set()
        await asyncio.Event().wait()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    async def run():
        transport = MemoryHTTPTransport()
        async def request():
            with provider_transport_scope(transport):
                async with provider_http_client(None, operation="embedding") as client:
                    await client.post("https://memory.fixture/embeddings", json={})
        caller = asyncio.create_task(request())
        await entered.wait()
        await transport.close()
        assert caller.cancelled() and not transport._clients
        with pytest.raises(RuntimeError):
            async with transport.lease():
                pass
    asyncio.run(run())
