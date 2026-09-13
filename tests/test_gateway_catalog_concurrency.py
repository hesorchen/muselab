import asyncio
import time

import httpx
import pytest


def configure(chat, monkeypatch, handler):
    route = {"ANTHROPIC_BASE_URL": "https://gateway.fixture", "ANTHROPIC_API_KEY": "fixture-a"}
    monkeypatch.setattr(chat.endpoints, "routing_env", lambda _: dict(route))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return route


def test_catalog_probe_is_shared_and_cache_age_starts_on_completion(app_module, monkeypatch):
    from backend import chat
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"models": [
            {"slug": "fixture-model", "context_window": 256000}]})
    configure(chat, monkeypatch, handler)
    async def run():
        callers = [asyncio.create_task(chat._detect_gateway_context_capability("codex:fixture-model"))
                   for _ in range(12)]
        await entered.wait()
        callers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await callers[0]
        completed_after = time.monotonic()
        release.set()
        results = await asyncio.gather(*callers[1:])
        assert len(requests) == 1
        assert all(result["context_limit"] == 243200 for result in results)
        key = chat._context_capability_key("https://gateway.fixture", "codex:fixture-model", "fixture-a")
        assert chat._CONTEXT_CAPABILITY_CACHE[key][0] >= completed_after
    asyncio.run(run())


def test_catalog_outage_preserves_bounded_last_known_capacity_but_not_other_credentials(app_module, monkeypatch):
    from backend import chat
    requests = []
    async def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("fixture", request=request)
    route = configure(chat, monkeypatch, handler)
    model = "codex:fixture-model"
    key = chat._context_capability_key(route["ANTHROPIC_BASE_URL"], model, route["ANTHROPIC_API_KEY"])
    chat._CONTEXT_CAPABILITY_CACHE[key] = (time.monotonic() - 600,
        {"context_limit": 243200, "context_raw_limit": 256000, "context_limit_source": "gateway_catalog"})
    async def run():
        result = await chat._detect_gateway_context_capability(model)
        assert result["context_limit"] == 243200 and result["context_catalog_stale"]
        again = await chat._detect_gateway_context_capability(model)
        assert again["context_limit"] == 243200 and len(requests) == 1
        route["ANTHROPIC_API_KEY"] = "fixture-b"
        assert await chat._detect_gateway_context_capability(model) is None
        assert len(requests) == 2
        assert chat._cached_gateway_context_capability(model) is None
    asyncio.run(run())


def test_compatibility_probes_share_one_whole_operation_deadline(app_module, monkeypatch):
    from backend import chat
    requests, cancelled = [], []
    async def handler(request):
        requests.append(request)
        try:
            await asyncio.sleep(.15)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(404)
    configure(chat, monkeypatch, handler)
    monkeypatch.setenv("MUSELAB_CONTEXT_CATALOG_TIMEOUT_S", "0.2")
    async def run():
        assert await asyncio.wait_for(chat._detect_gateway_context_capability("codex:fixture-model"), 2) is None
        assert len(requests) < 4 and cancelled
        before = len(requests)
        assert await chat._detect_gateway_context_capability("codex:fixture-model") is None
        assert len(requests) == before
    asyncio.run(run())


def test_batch_catalog_discovery_isolates_credentials_on_same_route(app_module, monkeypatch):
    from backend import chat
    requests = []
    async def handler(request):
        requests.append(request)
        credential = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(200, json={"models": [
            {"slug": credential, "context_window": 256000}]})
    configure(chat, monkeypatch, handler)
    monkeypatch.setattr(chat.endpoints, "routing_env", lambda model: {
        "ANTHROPIC_BASE_URL": "https://gateway.fixture",
        "ANTHROPIC_API_KEY": model.removeprefix("codex:"),
    })
    async def run():
        result = await chat._detect_gateway_context_capabilities(
            ["codex:fixture-a", "codex:fixture-b"])
        assert set(result) == {"codex:fixture-a", "codex:fixture-b"}
        assert len(requests) == 2
        assert {row.headers["authorization"] for row in requests} == {
            "Bearer fixture-a", "Bearer fixture-b"}
    asyncio.run(run())
