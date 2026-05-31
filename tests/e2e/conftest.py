"""Shared fixtures for e2e tests. Skipped unless RUN_E2E=1 because they
require Playwright + Chromium binary (~200 MB) and a running backend."""
from __future__ import annotations
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


E2E_ENABLED = os.environ.get("RUN_E2E") == "1"
TEST_TOKEN = "test-token-1234567890abcdef-secure-min-32"


def pytest_collection_modifyitems(config, items):
    """Skip all tests in this directory unless RUN_E2E=1."""
    if E2E_ENABLED:
        return
    skip = pytest.mark.skip(reason="set RUN_E2E=1 to enable Playwright e2e tests")
    for item in items:
        if "tests/e2e" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def backend_url(tmp_path_factory):
    """Boot a real backend.main subprocess against a throwaway root, yield
    its base URL. Tears down on session exit."""
    if not E2E_ENABLED:
        pytest.skip("RUN_E2E=1 required")

    root = tmp_path_factory.mktemp("e2e-root")
    (root / "README.md").write_text("# muselab e2e fixture\n")
    (root / "notes.md").write_text("scratch\n")

    port = _free_port()
    env = {
        **os.environ,
        "MUSELAB_TOKEN": TEST_TOKEN,
        "MUSELAB_ROOT": str(root),
        "MUSELAB_PORT": str(port),
    }
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.main"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    # Wait for server readiness; fail fast if the process died.
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            raise RuntimeError(f"backend died during startup:\n{out}")
        try:
            urllib.request.urlopen(f"{base}/static/app.js", timeout=0.5).close()
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("backend never became ready")

    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def auth_token() -> str:
    return TEST_TOKEN
