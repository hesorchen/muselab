"""Opened response cleanup joins real reads after disconnect or cancellation."""

import asyncio
import os
import threading

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec_version", "stop"),
    [("2.3", "disconnect"), ("2.3", "cancel"), ("2.4", "cancel")],
)
async def test_opened_response_stops_after_owned_read_finishes(
    app_module, tmp_path, spec_version, stop,
):
    from backend import files

    target = tmp_path / "download.bin"
    chunk_size = files._OpenedFileResponse.chunk_size
    with target.open("wb") as output:
        output.truncate(chunk_size * 4)
    raw = target.open("rb")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    observed_disconnect = asyncio.Event()
    disconnect = asyncio.Event()
    release = threading.Event()

    class ObservedFile:
        reads = 0
        reading = False
        closed_while_reading = False

        def read(self, size=-1):
            self.reads += 1
            self.reading = True
            try:
                if self.reads == 2:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(5), "read worker was not released"
                return raw.read(size)
            finally:
                self.reading = False
                if self.reads == 2:
                    loop.call_soon_threadsafe(finished.set)

        def close(self):
            self.closed_while_reading |= self.reading
            raw.close()

        def __getattr__(self, name):
            return getattr(raw, name)

    stream = ObservedFile()
    response = files._OpenedFileResponse(
        target, stream, os.fstat(raw.fileno()),
    )

    async def receive():
        await disconnect.wait()
        observed_disconnect.set()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    task = asyncio.create_task(response({
        "type": "http", "method": "GET", "headers": [],
        "asgi": {"spec_version": spec_version},
    }, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "disconnect":
            disconnect.set()
            await asyncio.wait_for(observed_disconnect.wait(), 1)
        else:
            task.cancel()
            # Deliver the cancellation request while the real read is gated.
            await asyncio.sleep(0)
        assert not raw.closed, "the file closed while its disk worker still owned it"
        assert not task.done(), "response cleanup did not join the actual read"
        release.set()
        if stop == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)
        await asyncio.wait_for(finished.wait(), 2)
        assert raw.closed
        assert not stream.closed_while_reading
        assert stream.reads == 2, "disconnect allowed additional disk reads"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await asyncio.wait_for(finished.wait(), 2)
        raw.close()
