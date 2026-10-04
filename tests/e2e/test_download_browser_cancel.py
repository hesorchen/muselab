"""A real Chromium download cancellation must stop owned-file response reads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlencode

import pytest


_TOKEN = "browser-download-synthetic-token-32-chars"
_SIZE = 128 * 1024 * 1024
_SERVER = r'''
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from backend import files

lock = threading.Lock()
release = threading.Event()
state = {
    "read_calls": 0, "bytes_read": 0, "gate_entered": False,
    "closed": False, "peer_closed": False, "disconnect_received": False,
    "response_finished": False,
}
original_open = files._open_response_file


class ObservedFile:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size=-1):
        with lock:
            should_wait = state["read_calls"] > 0 and not release.is_set()
            if should_wait:
                state["gate_entered"] = True
        if should_wait and not release.wait(30):
            raise RuntimeError("controlled file-read gate was not released")
        data = self.stream.read(size)
        with lock:
            state["read_calls"] += 1
            state["bytes_read"] += len(data)
        return data

    def close(self):
        self.stream.close()
        with lock:
            state["closed"] = self.stream.closed


def observed_open(target):
    stream, info = original_open(target)
    return ObservedFile(stream), info


files._open_response_file = observed_open
app = FastAPI()
app.include_router(files.router)


@app.get("/probe/state")
def get_state():
    with lock:
        return dict(state)


@app.post("/probe/release")
def release_reads():
    release.set()
    return {"released": True}


@app.get("/probe/page")
def page():
    return HTMLResponse("<!doctype html><title>Synthetic download probe</title><body></body>")


async def observed_app(scope, receive, send):
    if scope["type"] != "http" or scope["path"] != "/api/files/download":
        await app(scope, receive, send)
        return

    async def observed_receive():
        message = await receive()
        if message["type"] == "http.disconnect":
            with lock:
                state["disconnect_received"] = True
        return message

    try:
        await app(scope, observed_receive, send)
    finally:
        with lock:
            state["response_finished"] = True


class ObservedH11(H11Protocol):
    def connection_lost(self, exc):
        download = self.cycle and self.cycle.scope.get("path") == "/api/files/download"
        super().connection_lost(exc)
        if download:
            with lock:
                state["peer_closed"] = True


import os
uvicorn.run(
    observed_app, host="127.0.0.1", port=int(os.environ["MUSELAB_PORT"]),
    http=ObservedH11, access_log=False, log_level="warning",
)
'''


@pytest.fixture
def observed_download_backend(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with (root / "synthetic-large.bin").open("wb") as stream:
        stream.truncate(_SIZE)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "MUSELAB_TOKEN": _TOKEN,
        "MUSELAB_ROOT": str(root),
        "MUSELAB_SESSIONS_DIR": str(root / "sessions"),
        "MUSELAB_ENV_PATH": str(root / "runtime.env"),
        "MUSELAB_CONFIG_DIR": str(root / "config"),
        "MUSELAB_MEMORY_DIR": str(root / "memory"),
        "XDG_STATE_HOME": str(root / "state"),
        "MUSELAB_PORT": str(port),
    }
    for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY",
        "ZHIPUAI_API_KEY", "MINIMAX_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY",
        "XIAOMI_MIMO_API_KEY", "QIANFAN_API_KEY", "CODEX_GATEWAY_API_KEY", "OPENAI_API_KEY",
        "OPENAI_IMAGE_API_KEY", "OPENAI_IMAGE_BASE_URL",
    ):
        env[key] = ""
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "download-server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", _SERVER], env=env,
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(log_path.read_text())
                try:
                    with urllib.request.urlopen(base + "/probe/state", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("isolated download backend did not start")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _state(page, base):
    response = page.request.get(base + "/probe/state")
    assert response.ok
    return response.json()


def _wait_state(page, base, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = _state(page, base)
        if predicate(state):
            return state
        page.wait_for_timeout(25)
    pytest.fail(f"controlled state did not settle: {json.dumps(state, sort_keys=True)}")


def _start_download(page, base):
    minted = page.request.post(
        base + "/api/files/download-ticket", headers={"X-Auth-Token": _TOKEN},
        data={"path": "synthetic-large.bin"},
    )
    assert minted.ok
    query = urlencode({"path": "synthetic-large.bin", "ticket": minted.json()["ticket"]})
    page.goto(base + "/probe/page")
    page.set_content(f'<a id="download" href="{base}/api/files/download?{query}">Download</a>')
    with page.expect_download() as pending:
        page.locator("#download").click()
    download = pending.value
    paused = _wait_state(page, base, lambda state: state["gate_entered"])
    assert paused["read_calls"] == 1
    assert 0 < paused["bytes_read"] < _SIZE
    assert not paused["closed"]
    return download, paused


def test_browser_cancel_stops_remaining_download_reads(page, observed_download_backend):
    base = observed_download_backend
    download, paused = _start_download(page, base)
    download.cancel()
    assert download.failure() == "canceled"
    # The real Uvicorn protocol reports the browser socket closing, independently
    # of whether the application currently watches ASGI receive for disconnect.
    _wait_state(page, base, lambda state: state["peer_closed"])
    assert page.request.post(base + "/probe/release").ok
    final = _wait_state(page, base, lambda state: state["closed"] and state["response_finished"])
    print("BROWSER_CANCEL_STATE", json.dumps(final, sort_keys=True))
    # Allow the one real worker-thread read held at our explicit gate. Bounds
    # depend on controlled read ownership, not download speed or elapsed time.
    assert final["read_calls"] <= paused["read_calls"] + 1, final
    assert final["bytes_read"] <= 2 * paused["bytes_read"], final
    assert final["disconnect_received"], final


def test_browser_download_completion_still_reads_all_bytes(page, observed_download_backend):
    base = observed_download_backend
    download, _ = _start_download(page, base)
    assert page.request.post(base + "/probe/release").ok
    assert download.failure() is None
    local_path = download.path()
    assert local_path is not None and Path(local_path).stat().st_size == _SIZE
    final = _wait_state(page, base, lambda state: state["closed"] and state["response_finished"])
    assert final["bytes_read"] == _SIZE
