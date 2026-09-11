"""Upload disk waits and cancellation must not stall or race the event loop."""
import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile


def test_upload_open_and_close_run_off_loop(app_module, tmp_path, monkeypatch):
    from backend import files
    original = Path.open
    calls = []

    class Sink:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def write(self, data):
            return self.stream.write(data)
        def close(self):
            calls.append(threading.get_ident())
            self.stream.close()

    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        if path.name.endswith('.uploading'):
            calls.append(threading.get_ident())
            return Sink(stream)
        return stream

    monkeypatch.setattr(Path, 'open', opened)

    async def scenario():
        upload = UploadFile(io.BytesIO(b'synthetic'), filename='sample.txt')
        result = await files.upload(path='', file=upload, upload_id='', root=tmp_path)
        assert result['size'] == 9
        assert calls and threading.get_ident() not in calls
    asyncio.run(scenario())
    assert (tmp_path / 'sample.txt').read_bytes() == b'synthetic'


def test_pending_upload_lock_wait_does_not_block_loop(app_module, tmp_path):
    from backend import files
    entered, release = threading.Event(), threading.Event()

    def holder():
        with files._PENDING_UPLOAD_LOCK:
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    guard = threading.Timer(.5, release.set)
    guard.start()

    async def scenario():
        task = asyncio.create_task(files.cancel_upload(
            files.UploadControlReq(upload_id='a' * 32), root=tmp_path))
        try:
            start = time.perf_counter()
            await asyncio.sleep(.02)
            assert time.perf_counter() - start < .2
        finally:
            release.set()
            await task
            await files.cleanup_pending_uploads()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        guard.cancel()
        thread.join(2)


def test_cancelled_upload_joins_write_before_closing(app_module, tmp_path, monkeypatch):
    from backend import files
    original = Path.open
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Sink:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def write(self, data):
            entered.set()
            release.wait(2)
            return self.stream.write(data)
        def close(self):
            closed.set()
            self.stream.close()

    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Sink(stream) if path.name.endswith('.uploading') else stream

    monkeypatch.setattr(Path, 'open', opened)

    async def scenario():
        task = asyncio.create_task(files.upload(
            path='', file=UploadFile(io.BytesIO(b'synthetic'), filename='sample.txt'),
            upload_id='', root=tmp_path))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(.02)
            assert not closed.is_set()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed.is_set()
    asyncio.run(scenario())
    assert not (tmp_path / 'sample.txt').exists()
    assert not list(tmp_path.glob('.*.uploading'))
