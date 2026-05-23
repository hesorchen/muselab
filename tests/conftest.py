"""Shared pytest fixtures: spin up a backend.main app against a temp ROOT and
fresh sessions dir, with a known token. Each test gets a clean filesystem."""
import sys
from pathlib import Path

import pytest


TEST_TOKEN = "test-token-1234567890abcdef-secure-min-32"


@pytest.fixture()
def temp_root(tmp_path: Path) -> Path:
    """Throwaway directory used as MUSELAB_ROOT for the test run."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("# Hello\n\nfirst paragraph here\n")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# A\nbody of a\n")
    (root / "notes" / "b.txt").write_text("plain b text\n")
    (root / "notes" / "deep").mkdir()
    (root / "notes" / "deep" / "c.py").write_text("def hello():\n    pass\n")
    (root / ".secret").write_text("hidden file")
    (root / ".env").write_text("FAKE=secret")
    return root


@pytest.fixture()
def app_module(monkeypatch, temp_root, tmp_path):
    """Reload backend.main against the temp root so each test is isolated.
    Critically, redirect sessions/ to a tmp dir so tests don't pollute the
    real production sessions directory."""
    monkeypatch.setenv("MUSELAB_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("MUSELAB_ROOT", str(temp_root))
    monkeypatch.setenv("MUSELAB_PORT", "9999")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    # Tunable MUSELAB_* envs that the settings GET / PUT round-trip touches.
    # Critical now (2026-05-23): api_settings.put_settings only writes a key
    # when its requested value differs from `_current(env_key)`, and
    # _current() falls back to a canonical default when the env is unset.
    # If a prior test leaked these into os.environ they'd block legitimate
    # writes in the next test ("env already that value, no need to write").
    # Cleared upfront so every test starts from "no env set, default
    # applies", same as a fresh process.
    for k in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL",
               "MUSELAB_DEFAULT_PERMISSION", "MUSELAB_THINKING_BUDGET",
               "MUSELAB_MAX_TURNS", "MUSELAB_NOTIFY_SCHEDULED",
               "MUSELAB_NOTIFY_NORMAL"):
        monkeypatch.delenv(k, raising=False)

    for name in [n for n in list(sys.modules) if n.startswith("backend")]:
        del sys.modules[name]

    # Isolate sessions dir BEFORE backend.main imports backend.chat, which
    # snapshots `sess.SESS_DIR / "active_turns"` into `_ACTIVE_TURN_DIR` at
    # module import time. If we patched SESS_DIR after, chat.py would have
    # already cached production's active_turns path — leaking real in-flight
    # sidecars (including the dev's own muselab session) into the test.
    from backend import sessions as sess_mod
    test_sess_dir = tmp_path / "sessions"
    test_sess_dir.mkdir()
    monkeypatch.setattr(sess_mod, "SESS_DIR", test_sess_dir)
    monkeypatch.setattr(sess_mod, "INDEX", test_sess_dir / "index.json")

    import backend.main as main_mod  # type: ignore[import]

    for k in ("DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY", "MINIMAX_API_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    return main_mod


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


@pytest.fixture()
def auth():
    """Header dict with the test token."""
    return {"X-Auth-Token": TEST_TOKEN}
