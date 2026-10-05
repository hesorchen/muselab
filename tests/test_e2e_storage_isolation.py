"""Browser fixtures must not inherit another process's mutable storage."""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import conftest as browser_fixtures
from tests.e2e.test_live_subagent_updates import live_mux_server


@pytest.mark.parametrize("fixture_name", ["backend_url", "live_mux_server"])
def test_browser_backend_uses_its_own_config_and_memory(
        monkeypatch, tmp_path, tmp_path_factory, fixture_name):
    inherited = tmp_path / "other-runtime"
    config = inherited / "config"
    memory = inherited / "memory"
    config.mkdir(parents=True)
    memory.mkdir()
    marker = memory / "config.json"
    marker.write_text('{"owner_id":"another-runtime"}\n', encoding="utf-8")
    original_marker = marker.read_bytes()
    monkeypatch.setenv("MUSELAB_CONFIG_DIR", str(config))
    monkeypatch.setenv("MUSELAB_MEMORY_DIR", str(memory))
    captured = {}
    finished = []

    class BackendProcess:
        def __init__(self, args, **kwargs):
            captured.update(kwargs["env"])

        def poll(self):
            return None

        def terminate(self):
            finished.append("terminate")

        def wait(self, timeout):
            finished.append("wait")
            return 0

    # Observe the actual fixture's launch boundary without starting a server.
    # Resolve those paths in a real fresh interpreter below, after restoring
    # Popen. No browser, model, credentials file or application lifespan runs.
    with monkeypatch.context() as launch:
        launch.setattr(subprocess, "Popen", BackendProcess)
        launch.setattr(urllib.request, "urlopen", lambda *a, **k: SimpleNamespace(close=lambda: None))
        launch.setattr(browser_fixtures, "E2E_ENABLED", True)
        if fixture_name == "backend_url":
            fixture = browser_fixtures.backend_url.__wrapped__(tmp_path_factory)
        else:
            fixture = live_mux_server.__wrapped__(tmp_path, SimpleNamespace(close=lambda: None))
        next(fixture)
        with pytest.raises(StopIteration):
            next(fixture)
    assert finished == ["terminate", "wait"]
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from backend.config_paths import CONFIG_DIR
from backend.memory_config import memory_dir, load_config
print(json.dumps({'config': str(CONFIG_DIR.resolve()),
                  'memory': str(memory_dir().resolve()),
                  'owner': load_config().owner_id}))
"""],
        cwd=Path(__file__).resolve().parents[1], env=captured,
        capture_output=True, text=True, check=True, timeout=15,
    )
    actual = json.loads(result.stdout.splitlines()[-1])
    root = Path(captured["MUSELAB_ROOT"]).resolve()
    outside = {kind: actual[kind] for kind in ("config", "memory")
               if not Path(actual[kind]).is_relative_to(root)}
    assert marker.read_bytes() == original_marker
    assert not outside, f"Browser fixture inherited mutable storage: {outside}"
    assert actual["owner"] == "default"
