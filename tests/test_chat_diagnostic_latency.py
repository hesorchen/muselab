"""A blocked stderr pipe must not stall chat diagnostics on the asyncio loop."""
import asyncio
import threading
import time

import pytest


@pytest.mark.parametrize('kind', ['context_failure', 'context_recovery', 'cli'])
def test_chat_diagnostics_do_not_wait_for_stderr(app_module, monkeypatch, kind):
    from backend import chat, observability as obs

    monkeypatch.setenv("MUSELAB_PERF_LOG", "0")
    release = threading.Event()
    entered = threading.Event()
    sink_threads = []
    lines = []
    class SlowSink:
        def write(self, line):
            sink_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            lines.append(line)
        def flush(self):
            pass

    chat._CONTEXT_PROBE_LOG_STATE.clear()
    if kind == 'context_recovery':
        chat._CONTEXT_PROBE_LOG_STATE[('fixture-model', 'TimeoutError')] = (0, 2)
    obs.start_diagnostics()
    monkeypatch.setattr(chat.sys, 'stderr', SlowSink())
    guard = threading.Timer(0.5, release.set)
    guard.start()
    try:
        async def scenario():
            started = time.perf_counter()
            if kind == 'context_failure':
                chat._log_context_probe_failure('fixture-model', TimeoutError('private payload'))
            elif kind == 'context_recovery':
                chat._log_context_probe_recovery('fixture-model')
            else:
                chat._privacy_safe_cli_stderr_logger('claude', 'fixture-session')('timeout private payload')
            await asyncio.sleep(0)
            return time.perf_counter() - started
        elapsed = asyncio.run(scenario())
        assert elapsed < 0.2, f'diagnostic stalled event loop for {elapsed:.3f}s'
        assert entered.wait(1)
        assert threading.get_ident() not in sink_threads
    finally:
        release.set()
        guard.cancel()
        obs.stop_diagnostics()
        chat._CONTEXT_PROBE_LOG_STATE.clear()
    assert lines
    assert 'private payload' not in ''.join(lines)
