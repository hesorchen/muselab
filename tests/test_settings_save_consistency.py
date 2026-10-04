"""Settings compare-and-save must use the same serialized transaction."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def synthetic_settings(monkeypatch, tmp_path, request):
    # Set selectors before importing any backend configuration module. The
    # shared app fixture also writes its own synthetic test.env in tmp_path.
    directory = tmp_path / "synthetic-config"
    directory.mkdir()
    monkeypatch.setenv("MUSELAB_CONFIG_DIR", str(directory))
    monkeypatch.setenv("MUSELAB_ENV_PATH", str(directory / "runtime.env"))
    request.getfixturevalue("app_module")
    from backend import api_settings, chat, config_paths, endpoints, hook_settings, settings

    paths = {
        "ENV_PATH": directory / "runtime.env",
        "MCP_CONFIG_PATH": directory / "mcp.json",
        "PROVIDER_OVERRIDES_PATH": directory / "provider_overrides.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(config_paths, name, path)
    monkeypatch.setattr(config_paths, "CONFIG_DIR", directory)
    monkeypatch.setattr(api_settings, "ENV_PATH", paths["ENV_PATH"])
    monkeypatch.setattr(api_settings, "MCP_CONFIG_PATH", paths["MCP_CONFIG_PATH"])
    monkeypatch.setattr(settings, "ENV_PATH", paths["ENV_PATH"])
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", paths["MCP_CONFIG_PATH"])
    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", paths["PROVIDER_OVERRIDES_PATH"])
    for name, suffix in [
        ("_CLAUDE_USER_JSON", "claude-user.json"),
        ("_CLAUDE_USER_SETTINGS", "claude-settings.json"),
        ("_CLAUDE_CRED", "claude-credentials.json"),
        ("MCP_EXAMPLE_PATH", "mcp-example.json"),
        ("SKILL_USER_DIR", "skills"),
        ("SKILL_PLUGIN_ROOT", "plugins"),
    ]:
        monkeypatch.setattr(api_settings, name, directory / suffix)
    monkeypatch.setattr(hook_settings, "USER_SETTINGS_PATH", directory / "claude-settings.json")
    for path in [paths["MCP_CONFIG_PATH"], paths["PROVIDER_OVERRIDES_PATH"],
                 api_settings._CLAUDE_USER_JSON, api_settings._CLAUDE_USER_SETTINGS,
                 api_settings._CLAUDE_CRED, api_settings.MCP_EXAMPLE_PATH]:
        path.write_text("{}\n", encoding="utf-8")
    assert all(path.is_relative_to(tmp_path) for path in paths.values())
    from tests.conftest import TEST_TOKEN

    app = FastAPI()
    app.include_router(api_settings.router)
    with TestClient(app) as client:
        yield api_settings, settings, chat, client, {"X-Auth-Token": TEST_TOKEN}, paths["ENV_PATH"]


@pytest.mark.parametrize("field,baseline,replacement,env_keys", [
    ("default_model", "synthetic-initial-model", "synthetic-next-model",
     ("MUSELAB_DEFAULT_MODEL", "MUSELAB_MODEL")),
    ("default_permission", "default", "bypassPermissions", ("MUSELAB_DEFAULT_PERMISSION",)),
    ("busy_send_mode", "adjust", "queue", ("MUSELAB_BUSY_SEND_MODE",)),
])
def test_queued_explicit_settings_save_compares_after_prior_commit(
    synthetic_settings, monkeypatch, field, baseline, replacement, env_keys,
):
    api_settings, settings, chat, client, auth, path = synthetic_settings
    for key in env_keys:
        monkeypatch.setenv(key, baseline)
    if field == "default_model":
        monkeypatch.setattr(settings, "MODEL", baseline)
        monkeypatch.setattr(chat, "MODEL", baseline)
    path.write_text(
        "# retained\nUNRELATED=keep\n" + "".join(f"{key}={baseline}\n" for key in env_keys),
        encoding="utf-8",
    )
    writing = threading.Event()
    queued = threading.Event()
    release = threading.Event()
    first_writer = None
    write_env = api_settings._write_env

    class ObservedTransaction:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if writing.is_set() and threading.get_ident() != first_writer:
                queued.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_exc):
            self.lock.release()

    def pause_first_write(updates):
        nonlocal first_writer
        if first_writer is None:
            first_writer = threading.get_ident()
            writing.set()
            assert release.wait(5), "first writer was not released"
        return write_env(updates)

    monkeypatch.setattr(api_settings, "_ENV_WRITE_LOCK", ObservedTransaction())
    monkeypatch.setattr(api_settings, "_write_env", pause_first_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.put, "/api/settings", headers=auth, json={field: replacement})
        try:
            assert writing.wait(5)
            second = executor.submit(client.put, "/api/settings", headers=auth, json={field: baseline})
            assert queued.wait(5), "second request did not wait for the transaction"
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        response = second.result(timeout=5)
        assert response.status_code == 200
    saved = dotenv_values(path)
    for key in env_keys:
        assert saved[key] == baseline
        assert os.environ[key] == baseline
    assert response.json()["updated_count"] == 1
    assert saved["UNRELATED"] == "keep"
    assert "# retained" in path.read_text(encoding="utf-8")
    current = client.get("/api/settings", headers=auth)
    assert current.status_code == 200
    visible_key = {"default_model": "model", "default_permission": "permission",
                   "busy_send_mode": "busy_send_mode"}[field]
    assert current.json()["defaults"][visible_key] == baseline
    if field == "default_model":
        assert settings.MODEL == chat.MODEL == baseline


def test_settings_partial_save_preserves_unspecified_fields(synthetic_settings, monkeypatch):
    api_settings, settings, chat, client, auth, path = synthetic_settings
    values = {
        "MUSELAB_MODEL": "synthetic-model",
        "MUSELAB_DEFAULT_MODEL": "synthetic-model",
        "MUSELAB_DEFAULT_PERMISSION": "default",
        "MUSELAB_BUSY_SEND_MODE": "adjust",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "MODEL", "synthetic-model")
    monkeypatch.setattr(chat, "MODEL", "synthetic-model")
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    response = client.put("/api/settings", headers=auth, json={"busy_send_mode": "queue"})
    assert response.status_code == 200
    assert response.json()["updated"] == ["MUSELAB_BUSY_SEND_MODE"]
    assert response.json()["updated_count"] == 1
    values["MUSELAB_BUSY_SEND_MODE"] = "queue"
    assert dotenv_values(path) == values
    for key, value in values.items():
        assert os.environ[key] == value
    assert settings.MODEL == chat.MODEL == "synthetic-model"


def test_failed_settings_replace_preserves_state_and_allows_retry(synthetic_settings, monkeypatch):
    import errno
    from pathlib import Path

    api_settings, settings, chat, client, auth, path = synthetic_settings
    baseline = "synthetic-initial-model"
    requested = "synthetic-requested-model"
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        monkeypatch.setenv(key, baseline)
    monkeypatch.setattr(settings, "MODEL", baseline)
    monkeypatch.setattr(chat, "MODEL", baseline)
    original = f"MUSELAB_MODEL={baseline}\nMUSELAB_DEFAULT_MODEL={baseline}\nUNRELATED=keep\n"
    path.write_text(original, encoding="utf-8")
    failure = OSError(errno.EIO, "controlled synthetic config replacement failure")

    class FailOneConfigReplace:
        pending = True

        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, source, destination):
            if self.pending and Path(destination) == path:
                self.pending = False
                raise failure
            return os.replace(source, destination)

    # Scope the fault to this module's synthetic config destination; other
    # modules and process-owned OS operations retain their real functions.
    monkeypatch.setattr(api_settings, "os", FailOneConfigReplace())
    with pytest.raises(OSError) as caught:
        client.put("/api/settings", headers=auth, json={"default_model": requested})
    assert caught.value is failure
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(".env.*"))
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert os.environ[key] == baseline
    assert settings.MODEL == chat.MODEL == baseline

    response = client.put("/api/settings", headers=auth, json={"default_model": requested})
    assert response.status_code == 200
    assert response.json()["updated_count"] == 1
    saved = dotenv_values(path)
    assert saved["UNRELATED"] == "keep"
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert saved[key] == os.environ[key] == requested
    assert settings.MODEL == chat.MODEL == requested
    assert not list(path.parent.glob(".env.*"))
