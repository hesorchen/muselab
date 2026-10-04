"""Upload cleanup retains ownership when its request is cancelled."""

import asyncio
import errno
import io
import threading
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile


@pytest.mark.parametrize("cancellations", [0, 1, 2])
def test_failed_upload_cancellation_during_registry_cleanup(
    app_module, tmp_path, monkeypatch, cancellations,
):
    asyncio.run(_cleanup_scenario(tmp_path, monkeypatch, cancellations=cancellations))


def test_cancelled_upload_preserves_first_cancel_during_cleanup(app_module, tmp_path, monkeypatch):
    asyncio.run(_cleanup_scenario(tmp_path, monkeypatch, cancel_write=True))


async def _cleanup_scenario(root, monkeypatch, *, cancellations=0, cancel_write=False):
    from backend import files

    target = root / "sample.txt"
    target.write_bytes(b"original target")
    original_open = Path.open
    real_lock = files._PENDING_UPLOAD_LOCK
    loop = asyncio.get_running_loop()
    cleanup_waiting = asyncio.Event()
    write_started = asyncio.Event()
    release_write = threading.Event()
    failure = OSError(errno.ENOSPC, "controlled upload sink failure")
    held = False
    streams = []

    class ObservedRegistryLock:
        def acquire(self, *args, **kwargs):
            if held:
                loop.call_soon_threadsafe(cleanup_waiting.set)
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *args):
            self.release()

    class Sink:
        def __init__(self, stream):
            self.stream = stream

        def write(self, chunk):
            nonlocal held
            # Real partial bytes reach disk before the failure/cancel window.
            written = self.stream.write(chunk[:8])
            self.stream.flush()
            real_lock.acquire()
            held = True
            if cancel_write:
                loop.call_soon_threadsafe(write_started.set)
                assert release_write.wait(5)
                return written
            raise failure

        def close(self):
            self.stream.close()

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.parent == root and path.name.endswith(".uploading") and mode == "wb":
            streams.append(stream)
            return Sink(stream)
        return stream

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(files, "_PENDING_UPLOAD_LOCK", ObservedRegistryLock())
    upload_id = "c" * 32
    upload = UploadFile(io.BytesIO(b"synthetic upload bytes"), filename=target.name)
    task = asyncio.create_task(files.upload(path="", file=upload, upload_id=upload_id, root=root))
    original_cancel = "upload owner cancelled"
    try:
        if cancel_write:
            await asyncio.wait_for(write_started.wait(), 2)
            task.cancel(original_cancel)
            await asyncio.sleep(0)
            assert not streams[0].closed
            release_write.set()
        await asyncio.wait_for(cleanup_waiting.wait(), 2)
        assert streams[0].closed
        unfinished = list(root.glob(".*.uploading"))
        assert len(unfinished) == 1
        assert unfinished[0].read_bytes() == b"syntheti"
        assert target.read_bytes() == b"original target"
        for index in range(1 if cancel_write else cancellations):
            task.cancel("second stage cancel" if cancel_write or index else original_cancel)
            # Deliver cancellation separately without releasing the actual lock.
            await asyncio.sleep(0)
        assert not task.done()
        real_lock.release()
        held = False
        if cancellations or cancel_write:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, 2)
            assert cancelled.value.args == (original_cancel,)
        else:
            with pytest.raises(OSError) as failed:
                await asyncio.wait_for(task, 2)
            assert failed.value is failure
        assert streams[0].closed
        assert not list(root.glob(".*.uploading"))
        assert files._upload_key(upload_id, "", root) not in files._PENDING_UPLOADS
        assert target.read_bytes() == b"original target"
    finally:
        release_write.set()
        if held:
            real_lock.release()
        await asyncio.gather(task, return_exceptions=True)
        await files.cleanup_pending_uploads()
        await upload.close()
