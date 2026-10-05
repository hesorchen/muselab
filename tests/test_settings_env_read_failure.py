"""Saving settings must distinguish a missing env file from a failed read."""

import errno
import os

from dotenv import dotenv_values
from fastapi.testclient import TestClient
import pytest

from tests.test_settings_save_consistency import synthetic_settings as synthetic_settings


def test_env_save_rejects_read_error_even_when_exists_returns_false(
    synthetic_settings, monkeypatch,
):
    api_settings, settings, chat, client, auth, path = synthetic_settings
    baseline = "synthetic-initial-model"
    requested = "synthetic-requested-model"
    original = (
        f"# retained\nMUSELAB_MODEL={baseline}\n"
        f"MUSELAB_DEFAULT_MODEL={baseline}\nUNRELATED=keep\n"
    ).encode()
    path.write_bytes(original)
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        monkeypatch.setenv(key, baseline)
    monkeypatch.setattr(settings, "MODEL", baseline)
    monkeypatch.setattr(chat, "MODEL", baseline)
    reads = []
    failure = OSError(errno.EIO, "controlled synthetic env read failure")

    class InaccessibleEnvPath(type(path)):
        def exists(self, **_kwargs):
            if self == path:
                # Python 3.14 suppresses any OSError here, including I/O errors.
                return False
            return super().exists(**_kwargs)

        def read_text(self, *args, **kwargs):
            if self == path:
                reads.append(True)
                raise failure
            return super().read_text(*args, **kwargs)

    failed_path = InaccessibleEnvPath(path)
    assert path.is_file() and not failed_path.exists()
    with pytest.raises(OSError) as caught:
        failed_path.read_text(encoding="utf-8")
    assert caught.value is failure
    monkeypatch.setattr(api_settings, "ENV_PATH", failed_path)
    with TestClient(client.app, raise_server_exceptions=False) as failed_client:
        response = failed_client.put(
            "/api/settings", headers=auth, json={"default_model": requested},
        )
    assert (response.status_code, path.read_bytes()) == (500, original)
    assert len(reads) == 2  # Direct control and the route both attempted the read.
    assert not list(path.parent.glob(".env.*"))
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert os.environ[key] == baseline
    assert settings.MODEL == chat.MODEL == baseline

    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    retry = client.put("/api/settings", headers=auth, json={"default_model": requested})
    assert retry.status_code == 200
    saved = dotenv_values(path)
    assert saved["UNRELATED"] == "keep"
    assert "# retained" in path.read_text(encoding="utf-8")
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert saved[key] == os.environ[key] == requested
    assert settings.MODEL == chat.MODEL == requested
    assert not list(path.parent.glob(".env.*"))


def test_env_save_initializes_a_genuinely_missing_file(synthetic_settings, monkeypatch):
    _, settings, chat, client, auth, path = synthetic_settings
    baseline = "synthetic-initial-model"
    requested = "synthetic-requested-model"
    assert not path.exists()
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        monkeypatch.setenv(key, baseline)
    monkeypatch.setattr(settings, "MODEL", baseline)
    monkeypatch.setattr(chat, "MODEL", baseline)
    response = client.put("/api/settings", headers=auth, json={"default_model": requested})
    assert response.status_code == 200
    saved = dotenv_values(path)
    for key in ("MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"):
        assert saved[key] == os.environ[key] == requested
    assert settings.MODEL == chat.MODEL == requested
    assert not list(path.parent.glob(".env.*"))
