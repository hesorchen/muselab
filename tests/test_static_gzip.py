import asyncio
import gzip
import threading
import time
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_static_gzip_cold_miss_is_single_flight_and_off_loop(
    app_module, tmp_path, monkeypatch,
):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    asset = static_dir / "app.js"
    payload = b"const value = 'muselab';\n" * 20_000
    asset.write_bytes(payload)

    static = app_module._VersionedStaticFiles(directory=static_dir)
    cls = app_module._VersionedStaticFiles
    with cls._gz_cache_lock:
        cls._gz_cache.clear()

    event_loop_thread = threading.get_ident()
    read_threads: list[int] = []
    original_read_bytes = Path.read_bytes

    def slow_read_bytes(path: Path) -> bytes:
        if path == asset:
            read_threads.append(threading.get_ident())
            time.sleep(0.05)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", slow_read_bytes)
    scope = {"headers": [(b"accept-encoding", b"gzip, deflate")]}

    responses = await asyncio.gather(*(
        static._try_gzip_response("app.js", scope) for _ in range(8)
    ))

    assert len(read_threads) == 1
    assert read_threads[0] != event_loop_thread
    assert all(response is not None for response in responses)
    assert all(response.headers["content-encoding"] == "gzip" for response in responses)
    assert all(gzip.decompress(response.body) == payload for response in responses)


@pytest.mark.asyncio
async def test_static_gzip_cache_key_includes_static_root(app_module, tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_payload = b"a" * 300_000
    second_payload = b"b" * 300_000
    (first_dir / "app.js").write_bytes(first_payload)
    (second_dir / "app.js").write_bytes(second_payload)

    cls = app_module._VersionedStaticFiles
    with cls._gz_cache_lock:
        cls._gz_cache.clear()
    first = cls(directory=first_dir)
    second = cls(directory=second_dir)
    scope = {"headers": [(b"accept-encoding", b"gzip")]}

    first_response, second_response = await asyncio.gather(
        first._try_gzip_response("app.js", scope),
        second._try_gzip_response("app.js", scope),
    )

    assert gzip.decompress(first_response.body) == first_payload
    assert gzip.decompress(second_response.body) == second_payload
