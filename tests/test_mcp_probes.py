import asyncio
from types import SimpleNamespace

from backend import mcp_probes


def test_probe_deadline_returns_partial_results_and_reuses_inflight(monkeypatch):
    monkeypatch.setattr(mcp_probes, 'DEADLINE_S', 0.01)
    async def run():
        release = asyncio.Event()
        calls = 0
        cancelled = False
        async def slow():
            nonlocal calls, cancelled
            calls += 1
            try:
                await release.wait()
                return {'mcpServers': []}
            except asyncio.CancelledError:
                cancelled = True
                raise
        async def healthy():
            return {'mcpServers': []}
        live = [(('slow', 'model'), SimpleNamespace(get_mcp_status=slow)),
                (('healthy', 'model'), SimpleNamespace(get_mcp_status=healthy))]
        first = await mcp_probes.probe_clients(live, 'get_mcp_status')
        second = await mcp_probes.probe_clients(live, 'get_mcp_status')
        assert first[0]['pending'] and second[0]['pending']
        assert first[1]['result'] == {'mcpServers': []}
        assert calls == 1 and not cancelled
        pool = mcp_probes._pools[asyncio.get_running_loop()]
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pool.tasks
    asyncio.run(run())


def test_probe_concurrency_is_bounded_and_errors_do_not_include_payload(monkeypatch):
    monkeypatch.setattr(mcp_probes, 'CONCURRENCY', 2)
    monkeypatch.setattr(mcp_probes, 'DEADLINE_S', 1)
    async def run():
        active = peak = 0
        async def probe():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.005)
                raise ValueError('synthetic-private-payload')
            finally:
                active -= 1
        live = [((str(i), 'm'), SimpleNamespace(get_mcp_status=probe)) for i in range(7)]
        result = await mcp_probes.probe_clients(live, 'get_mcp_status')
        assert peak == 2
        assert all(item['error'] == 'ValueError' for item in result)
        assert 'synthetic-private-payload' not in str(result)
    asyncio.run(run())
