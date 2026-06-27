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
    # Critical: redirect ENV_PATH to a throwaway file so PUT /api/settings
    # tests don't clobber the developer's real ~/muselab/.env. Without
    # this, test_regressions.py was silently overwriting DEEPSEEK_API_KEY
    # on every run (the placeholder "sk-test-key-12345" landed in
    # production .env, breaking real chat against DeepSeek). 2026-05-24.
    test_env_path = tmp_path / "test.env"
    test_env_path.write_text(f"MUSELAB_TOKEN={TEST_TOKEN}\nMUSELAB_ROOT={temp_root}\n")
    monkeypatch.setenv("MUSELAB_ENV_PATH", str(test_env_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("XIAOMI_MIMO_API_KEY", raising=False)
    monkeypatch.delenv("QIANFAN_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_BASE_URL", raising=False)
    monkeypatch.delenv("MUSELAB_IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("CODEX_IMAGEGEN_ENABLED", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)

    # NOTE (audit I/312 — fragility, intentionally left as-is for now):
    # Deleting every `backend.*` module forces a full re-import of the whole
    # tree on each test, which re-runs module-level init (e.g. backend.chat
    # snapshots SESS_DIR/active_turns at import) so the monkeypatched ROOT /
    # SESS_DIR / env take effect. The downside is that any module-level mutable
    # global (chat._clients, scheduler._state, …) is recreated per test — which
    # mostly isolates state, but couples correctness to import order and means a
    # module that caches a path/handle BEFORE the relevant monkeypatch silently
    # leaks (see the SESS_DIR ordering dance below; test_scheduler.py:20 also
    # resets _state by hand). The proper fix is to move that global state into
    # injectable objects (e.g. an app-scoped registry) so tests construct a
    # fresh instance instead of nuking sys.modules — that's a larger refactor
    # touching backend/, out of scope for this CI/test-hardening pass.
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

    # Provider overrides are local runtime state (like mcp.json). Isolate them
    # for every app-backed test so a developer's real custom providers don't
    # change default-model resolution, provider lists, or auth status behavior.
    from backend import endpoints as ep_mod
    monkeypatch.setattr(ep_mod, "OVERRIDES_PATH", tmp_path / "provider_overrides.json")
    ep_mod._OVERRIDES_CACHE = None
    ep_mod._CATALOG_CACHE = None
    ep_mod._SORTED_CATALOG_CACHE = None

    import backend.main as main_mod  # type: ignore[import]

    # Isolate Claude Auth status/disconnect tests from the developer's real
    # ~/.claude/.credentials.json. Individual tests that need a credentials file
    # monkeypatch this path explicitly.
    from backend import api_settings as api_settings_mod
    monkeypatch.setattr(api_settings_mod, "_CLAUDE_CRED", tmp_path / ".credentials.json")

    # Re-delenv AFTER the import, because backend.settings calls
    # `load_dotenv` at module import time and the dev's real .env (if
    # present locally) repopulates the keys we just cleared. CI has no
    # .env so this loop is a no-op there, but on a dev machine it's
    # what keeps the suite hermetic. (2026-05-23 fix — found via the
    # api_settings._changed() comparison being too lenient when env
    # carries the test's chosen value from the host .env, e.g.
    # MUSELAB_DEFAULT_MODEL=claude-opus-4-7 → test PUTs opus → "no
    # change, skip write" → file assertion fails.)
    for k in ("DEEPSEEK_API_KEY", "ZHIPUAI_API_KEY", "MINIMAX_API_KEY",
              "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "XIAOMI_MIMO_API_KEY",
              "QIANFAN_API_KEY",
              "OPENAI_API_KEY", "OPENAI_IMAGE_API_KEY", "OPENAI_IMAGE_BASE_URL",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL",
              "MUSELAB_DEFAULT_PERMISSION", "MUSELAB_THINKING_BUDGET",
              "MUSELAB_MAX_TURNS"):
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
