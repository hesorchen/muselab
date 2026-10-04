"""Downloads keep checked file identity and bound every response body."""
import asyncio
import errno
from email.parser import BytesParser
import os

from fastapi import FastAPI
import httpx
import pytest


@pytest.fixture
def file_module(app_module):
    from backend import files
    return files


async def _download(module, auth, path, *, headers=None, mutate=None):
    app = FastAPI()
    app.include_router(module.router)
    empty_frames = 0

    async def wrapper(scope, receive, send):
        async def observe(message):
            nonlocal empty_frames
            if (scope["path"] == "/api/files/download"
                    and message["type"] == "http.response.start"
                    and message["status"] in {200, 206} and mutate is not None):
                mutate()
            if (message["type"] == "http.response.body"
                    and not message.get("body") and message.get("more_body")):
                empty_frames += 1
                if empty_frames >= 3:
                    raise RuntimeError("repeated empty download frames")
            await send(message)
        await app(scope, receive, observe)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapper), base_url="http://synthetic.local",
    ) as client:
        minted = await client.post(
            "/api/files/download-ticket", headers=auth, json={"path": path},
        )
        assert minted.status_code == 200
        return await client.get(
            "/api/files/download", headers=headers or {},
            params={"path": path, "ticket": minted.json()["ticket"]},
        )


@pytest.mark.parametrize("replacement", ["sensitive_symlink", "atomic_replace", "deep_symlink"])
@pytest.mark.parametrize("range_header", [None, "bytes=0-3", "bytes=0-3,8-11"])
def test_download_mutation_keeps_opened_file(
    file_module, auth, temp_root, replacement, range_header, download_streams,
):
    parent = temp_root / "parent" / "deep"
    parent.mkdir(parents=True)
    target = parent / "public.txt"
    original = b"PUBLIC_DOWNLOAD_MARKER"
    other = b"PRIVATE_REPLACED_VALUE"
    assert len(original) == len(other)
    target.write_bytes(original)
    private = temp_root / ".env"
    private.write_bytes(other)
    alternate = temp_root / "alternate"
    alternate.mkdir()
    (alternate / target.name).write_bytes(other)

    def mutate():
        if replacement == "sensitive_symlink":
            target.unlink()
            target.symlink_to(private)
        elif replacement == "atomic_replace":
            candidate = parent / "candidate.txt"
            candidate.write_bytes(other)
            candidate.replace(target)
        else:
            parent.rename(parent.with_name("saved"))
            parent.symlink_to(alternate, target_is_directory=True)

    response = asyncio.run(_download(
        file_module, auth, "parent/deep/public.txt", mutate=mutate,
        headers={"Range": range_header} if range_header else None,
    ))
    assert other not in response.content
    if range_header is None:
        assert response.status_code == 200
        assert response.content == original
    elif "," not in range_header:
        assert response.status_code == 206
        assert response.content == original[:4]
    else:
        assert response.status_code == 206
        assert b"\r\n\r\n" + original[:4] + b"\r\n" in response.content
        assert b"\r\n\r\n" + original[8:12] + b"\r\n" in response.content
    assert len(response.content) == int(response.headers["content-length"])


@pytest.mark.parametrize("range_header", [None, "bytes=0-3", "bytes=0-3,8-11"])
def test_download_truncation_fails_without_empty_frame_loop(
    file_module, auth, temp_root, range_header, download_streams,
):
    target = temp_root / "public.txt"
    target.write_bytes(b"PUBLIC_DOWNLOAD_MARKER")
    with pytest.raises(OSError, match="file changed during download"):
        asyncio.run(_download(
            file_module, auth, target.name,
            mutate=lambda: target.write_bytes(b""),
            headers={"Range": range_header} if range_header else None,
        ))


def test_download_append_does_not_exceed_content_length(file_module, auth, temp_root, download_streams):
    target = temp_root / "public.txt"
    original = b"PUBLIC_DOWNLOAD_MARKER"
    target.write_bytes(original)

    def append():
        with target.open("ab") as output:
            output.write(b"APPENDED_AFTER_HEADERS")

    response = asyncio.run(_download(file_module, auth, target.name, mutate=append))
    assert response.content == original
    assert len(response.content) == int(response.headers["content-length"])


@pytest.fixture
def download_streams(file_module, monkeypatch):
    opened = []
    original_open = file_module._open_response_file

    def capture(target):
        stream, info = original_open(target)
        assert os.fstat(stream.fileno()).st_ino == info.st_ino
        opened.append(stream)
        return stream, info

    monkeypatch.setattr(file_module, "_open_response_file", capture)
    yield opened
    assert all(stream.closed for stream in opened)


@pytest.mark.parametrize("method,range_header,if_range,extension,status", [
    ("GET", None, None, False, 200),
    ("GET", "bytes=0-3", None, False, 206),
    ("GET", "bytes=0-3,8-11", None, False, 206),
    ("GET", "bytes=1000-1001", None, False, 416),
    ("GET", "invalid=0-3", None, False, 400),
    ("GET", "bytes=0-3", "outdated", False, 200),
    ("HEAD", None, None, False, 200),
    ("HEAD", "bytes=0-3", None, False, 206),
    ("HEAD", "bytes=0-3,8-11", None, False, 206),
    ("GET", None, None, True, 200),
])
def test_download_protocol_and_success_closes_fd(
    file_module, temp_root, download_streams,
    method, range_header, if_range, extension, status,
):
    original = b"PUBLIC_DOWNLOAD_MARKER"
    (temp_root / "public.txt").write_bytes(original)
    response = file_module.download_file(target=temp_root / "public.txt")
    frames = []
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode("ascii")))
    if if_range:
        headers.append((b"if-range", if_range.encode("ascii")))

    async def send(message):
        assert message["type"] != "http.response.pathsend"
        frames.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": method, "headers": headers,
             "extensions": {"http.response.pathsend": {}} if extension else {}}
    asyncio.run(response(scope, receive, send))
    assert frames[0]["status"] == status
    assert download_streams and download_streams[0].closed
    body = b"".join(frame.get("body", b"") for frame in frames[1:])
    actual_headers = dict(frames[0]["headers"])
    if status in {200, 206}:
        assert b"attachment" in actual_headers[b"content-disposition"]
        assert b"accept-ranges" in actual_headers
        if method == "HEAD":
            assert body == b""
        else:
            assert len(body) == int(actual_headers[b"content-length"])
            if status == 200:
                assert body == original
            elif "," not in range_header:
                assert body == original[:4]
    if status == 416:
        assert actual_headers[b"content-range"] == b"bytes */22"


@pytest.mark.parametrize("phase", ["start", "body"])
def test_download_send_disconnect_closes_fd(
    file_module, temp_root, download_streams, phase,
):
    (temp_root / "public.txt").write_bytes(b"PUBLIC_DOWNLOAD_MARKER" * 7500)
    response = file_module.download_file(target=temp_root / "public.txt")
    failure = OSError(errno.EPIPE, "synthetic client disconnected")

    async def send(message):
        if message["type"] == f"http.response.{phase}":
            raise failure

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(OSError) as caught:
        asyncio.run(response({"type": "http", "method": "GET", "headers": []}, receive, send))
    assert caught.value is failure
    assert download_streams[0].closed


def test_download_task_cancellation_closes_fd(file_module, temp_root, download_streams):
    (temp_root / "public.txt").write_bytes(b"PUBLIC_DOWNLOAD_MARKER" * 7500)
    response = file_module.download_file(target=temp_root / "public.txt")

    async def exercise():
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                assert message["more_body"] is True
                entered.set()
                await blocked.wait()

        async def receive():
            return {"type": "http.disconnect"}

        task = asyncio.create_task(response(
            {"type": "http", "method": "GET", "headers": []}, receive, send,
        ))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            blocked.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert download_streams[0].closed


@pytest.mark.parametrize("replacement", ["fifo", "deep_symlink", "removed"])
def test_download_open_rejects_changed_path(
    file_module, auth, temp_root, tmp_path, monkeypatch, replacement,
):
    parent = temp_root / "parent" / "deep"
    parent.mkdir(parents=True)
    target = parent / "public.txt"
    target.write_bytes(b"PUBLIC_DOWNLOAD_MARKER")
    alternate = tmp_path / "unregistered"
    alternate.mkdir()
    (alternate / target.name).write_bytes(b"PRIVATE_REPLACED_VALUE")
    original_open = file_module._open_response_file

    def replace_before_open(path):
        if replacement == "fifo":
            target.unlink()
            os.mkfifo(target)
        elif replacement == "deep_symlink":
            parent.rename(parent.with_name("saved"))
            parent.symlink_to(alternate, target_is_directory=True)
        else:
            target.unlink()
        return original_open(path)

    monkeypatch.setattr(file_module, "_open_response_file", replace_before_open)
    response = asyncio.run(_download(file_module, auth, "parent/deep/public.txt"))
    assert response.status_code == 404
    assert b"PRIVATE_REPLACED_VALUE" not in response.content


@pytest.mark.parametrize("scope", ["workspace", "registered_symlink", "external"])
def test_download_preserves_supported_scopes(
    file_module, client, auth, temp_root, tmp_path, scope, download_streams,
):
    original = b"PUBLIC_DOWNLOAD_MARKER"
    external = scope == "external"
    if scope == "registered_symlink":
        nested = temp_root / "nested"
        nested.mkdir()
        target = temp_root / "shared.txt"
        target.write_bytes(original)
        (nested / "shared-link.txt").symlink_to(target)
        assert client.post(
            "/api/chat/workspaces", headers=auth, json={"path": str(nested)},
        ).status_code == 200
        auth = {**auth, "X-Muselab-Workspace": str(nested)}
        path = "shared-link.txt"
    elif external:
        target = tmp_path / "outside.txt"
        target.write_bytes(original)
        path = str(target)
    else:
        (temp_root / "public.txt").write_bytes(original)
        path = "public.txt"
    app = FastAPI()
    app.include_router(file_module.router)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.local",
        ) as api:
            minted = await api.post(
                "/api/files/download-ticket", headers=auth,
                json={"path": path, "external": external},
            )
            assert minted.status_code == 200
            params = {"path": path, "external": external, "ticket": minted.json()["ticket"]}
            workspace_headers = {key: value for key, value in auth.items() if key != "X-Auth-Token"}
            result = await api.get("/api/files/download", params=params, headers=workspace_headers)
            assert result.status_code == 200
            assert result.content == original
            assert "attachment" in result.headers["content-disposition"]
            replay = await api.get("/api/files/download", params=params, headers=workspace_headers)
            assert replay.status_code == 401

    asyncio.run(exercise())


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
def test_download_matching_if_range_uses_partial_response(
    file_module, temp_root, download_streams, validator,
):
    (temp_root / "public.txt").write_bytes(b"PUBLIC_DOWNLOAD_MARKER")
    response = file_module.download_file(target=temp_root / "public.txt")
    frames = []

    async def send(message):
        frames.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "headers": [
        (b"range", b"bytes=0-3"),
        (b"if-range", response.headers[validator].encode("ascii")),
    ]}
    asyncio.run(response(scope, receive, send))
    assert frames[0]["status"] == 206
    assert b"".join(frame.get("body", b"") for frame in frames[1:]) == b"PUBL"


@pytest.mark.parametrize("range_header,ranges", [
    (None, [(0, 262144)]),
    ("bytes=65530-131100", [(65530, 131101)]),
    ("bytes=-7", [(262137, 262144)]),
    ("bytes=0-10,5-15", [(0, 16)]),
    ("bytes=0-65540,131000-131100", [(0, 65541), (131000, 131101)]),
])
def test_download_large_body_and_ranges(
    file_module, auth, temp_root, download_streams, range_header, ranges,
):
    original = bytes(range(256)) * 1024
    (temp_root / "public.bin").write_bytes(original)
    response = asyncio.run(_download(
        file_module, auth, "public.bin",
        headers={"Range": range_header} if range_header else None,
    ))
    assert response.status_code == (206 if range_header else 200)
    assert len(response.content) == int(response.headers["content-length"])
    if len(ranges) == 1:
        start, end = ranges[0]
        assert response.content == original[start:end]
    else:
        message = BytesParser().parsebytes(
            b"Content-Type: " + response.headers["content-type"].encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + response.content,
        )
        parts = message.get_payload()
        assert isinstance(parts, list)
        assert len(parts) == len(ranges)
        for part, (start, end) in zip(parts, ranges, strict=True):
            assert part["Content-Range"] == f"bytes {start}-{end - 1}/{len(original)}"
            assert part.get_payload(decode=True) == original[start:end]
