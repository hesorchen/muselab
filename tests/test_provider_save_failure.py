"""Provider metadata and its optional credential must survive failed saves."""
import errno
import json
import os
from pathlib import Path
import stat

from fastapi.testclient import TestClient
import pytest

from tests.test_settings_save_consistency import synthetic_settings as synthetic_settings

SLOT = "MUSELAB_PROVIDER_SYNTHETIC_FAILURE_API_KEY"


def payload(**changes):
    return {
        "base_url": "https://provider.example.test/anthropic",
        "prefix": "synthetic-failure:",
        "models": ["synthetic-failure:model"],
        "env_key": SLOT,
        "api_key": "synthetic-new-credential",
        **changes,
    }


def post(client, auth, body):
    with TestClient(client.app, raise_server_exceptions=False) as failed_client:
        return failed_client.post("/api/settings/providers", headers=auth, json=body)


class ConfigOS:
    """Fault only exact test-owned rename destinations; delegate all other I/O."""
    def __init__(self, path, *, fail_at=1):
        self.path = path
        self.fail_at = fail_at
        self.replaces = 0
        self.streams = []

    def __getattr__(self, name):
        return getattr(os, name)

    def fdopen(self, *args, **kwargs):
        stream = os.fdopen(*args, **kwargs)
        self.streams.append(stream)
        return stream

    def replace(self, source, destination):
        if Path(destination) == self.path:
            self.replaces += 1
            if self.replaces == self.fail_at:
                raise OSError(errno.EIO, "controlled synthetic config rename failure")
        return os.replace(source, destination)


def assert_clean(api, path, *observers):
    assert not list(path.parent.glob(".env.*"))
    from backend import endpoints
    assert not list(endpoints.OVERRIDES_PATH.parent.glob("provider_overrides.json.tmp.*"))
    for observer in observers:
        assert observer.streams and all(stream.closed for stream in observer.streams)


def receipt(name, **values):
    # Optional private evidence contains only synthetic status/byte-preservation
    # booleans, never API-key values or request bodies.
    directory = os.environ.get("MUSELAB_PROVIDER_SAVE_RECEIPTS")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(json.dumps(values, indent=2) + "\n")


@pytest.mark.parametrize("fault", ["decode", "replace"])
def test_failed_provider_key_save_keeps_metadata_and_allows_original_retry(
    synthetic_settings, monkeypatch, fault,
):
    from backend import endpoints
    api, _settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.setenv(SLOT, "synthetic-old-process-credential")
    retained = client.post("/api/settings/providers", headers=auth, json=payload(
        base_url="https://retained.example.test/anthropic", prefix="synthetic-retained:",
        models=["synthetic-retained:model"], api_key=None,
    ))
    assert retained.status_code == 200
    original_provider = endpoints.OVERRIDES_PATH.read_bytes()
    good_env = b"# retained synthetic\nUNRELATED=synthetic-kept\n"
    original_env = good_env + (b"\xff\n" if fault == "decode" else b"")
    path.write_bytes(original_env)
    observer = ConfigOS(path, fail_at=1 if fault == "replace" else 0)
    monkeypatch.setattr(api, "os", observer)
    body = payload()
    failed = post(client, auth, body)
    unchanged = endpoints.OVERRIDES_PATH.read_bytes() == original_provider
    env_unchanged = path.read_bytes() == original_env
    process_unchanged = os.environ[SLOT] == "synthetic-old-process-credential"
    if fault == "decode":
        path.write_bytes(good_env)
    retry = post(client, auth, body)
    receipt(f"env-{fault}", failure_status=failed.status_code, retry_status=retry.status_code,
            provider_bytes_unchanged=unchanged, env_bytes_unchanged=env_unchanged,
            process_env_unchanged=process_unchanged,
            retained_id_present=retained.json()["id"] in json.loads(endpoints.OVERRIDES_PATH.read_text())["providers"])
    assert failed.status_code == 500
    assert unchanged and env_unchanged and process_unchanged
    assert retry.status_code == 200
    assert path.read_bytes().startswith(good_env)
    assert os.environ[SLOT] == "synthetic-new-credential"
    saved = json.loads(endpoints.OVERRIDES_PATH.read_text())["providers"]
    assert len(saved) == 2
    assert retained.json()["id"] in saved and retry.json()["id"] in saved
    assert_clean(api, path, observer)


@pytest.mark.parametrize("requested", ["synthetic-new-credential", "_delete_"])
def test_provider_commit_failure_restores_existing_env_and_hot_catalog(
    synthetic_settings, monkeypatch, requested,
):
    from backend import endpoints
    api, settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.setenv(SLOT, "synthetic-old-credential")
    seeded = post(client, auth, payload(api_key="synthetic-old-credential"))
    assert seeded.status_code == 200
    path.write_bytes(path.read_bytes() + b"# retained\nUNRELATED=synthetic-kept\n")
    original_env = path.read_bytes()
    original_provider = endpoints.OVERRIDES_PATH.read_bytes()
    hot_catalog = endpoints.catalog()
    provider_os = ConfigOS(endpoints.OVERRIDES_PATH)
    env_os = ConfigOS(path, fail_at=0)
    monkeypatch.setattr(settings, "os", provider_os)
    monkeypatch.setattr(api, "os", env_os)
    body = payload(id=seeded.json()["id"], base_url="https://edited.example.test/anthropic",
                   display="synthetic-edited", api_key=requested)
    failed = post(client, auth, body)
    receipt("override-" + ("delete" if requested == "_delete_" else "replace"),
            failure_status=failed.status_code, env_bytes_unchanged=path.read_bytes() == original_env,
            provider_bytes_unchanged=endpoints.OVERRIDES_PATH.read_bytes() == original_provider,
            process_env_unchanged=os.environ[SLOT] == "synthetic-old-credential",
            hot_catalog_unchanged=endpoints.catalog() == hot_catalog,
            real_override_replace_attempts=provider_os.replaces)
    assert failed.status_code == 500
    assert provider_os.replaces == 1
    assert path.read_bytes() == original_env
    assert endpoints.OVERRIDES_PATH.read_bytes() == original_provider
    assert os.environ[SLOT] == "synthetic-old-credential"
    assert endpoints.catalog() == hot_catalog
    assert_clean(api, path, provider_os, env_os)
    retry = post(client, auth, body)
    assert retry.status_code == 200
    assert retry.json()["id"] == seeded.json()["id"]
    assert endpoints.get_provider(seeded.json()["id"]).display == "synthetic-edited"
    if requested == "_delete_":
        assert SLOT not in os.environ and SLOT.encode() not in path.read_bytes()
    else:
        assert os.environ[SLOT] == requested
    assert b"UNRELATED=synthetic-kept" in path.read_bytes()
    assert_clean(api, path, provider_os, env_os)


def test_provider_commit_failure_restores_genuinely_missing_env(synthetic_settings, monkeypatch):
    from backend import endpoints
    api, settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.delenv(SLOT, raising=False)
    assert not path.exists()
    original_provider = endpoints.OVERRIDES_PATH.read_bytes()
    provider_os = ConfigOS(endpoints.OVERRIDES_PATH)
    env_os = ConfigOS(path, fail_at=0)
    monkeypatch.setattr(settings, "os", provider_os)
    monkeypatch.setattr(api, "os", env_os)
    failed = post(client, auth, payload())
    assert failed.status_code == 500
    assert not path.exists() and SLOT not in os.environ
    assert endpoints.OVERRIDES_PATH.read_bytes() == original_provider
    assert_clean(api, path, provider_os, env_os)
    retry = post(client, auth, payload())
    assert retry.status_code == 200
    assert path.is_file() and os.environ[SLOT] == "synthetic-new-credential"
    assert_clean(api, path, provider_os, env_os)


def test_failed_provider_rollback_retains_closed_private_recovery_copy(
    synthetic_settings, monkeypatch, caplog,
):
    from backend import endpoints
    api, settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.setenv(SLOT, "synthetic-old-credential")
    original_env = b"# original bytes\nUNRELATED=synthetic-kept\n"
    path.write_bytes(original_env)
    original_provider = endpoints.OVERRIDES_PATH.read_bytes()
    provider_os = ConfigOS(endpoints.OVERRIDES_PATH)
    env_os = ConfigOS(path, fail_at=2)
    monkeypatch.setattr(settings, "os", provider_os)
    monkeypatch.setattr(api, "os", env_os)
    failed = post(client, auth, payload())
    backups = list(path.parent.glob(".env.provider-rollback.*"))
    assert failed.status_code == 500 and "recovery" in failed.json()["detail"]
    assert len(backups) == 1 and backups[0].read_bytes() == original_env
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert all(stream.closed for observer in (provider_os, env_os) for stream in observer.streams)
    assert endpoints.OVERRIDES_PATH.read_bytes() == original_provider
    assert os.environ[SLOT] == "synthetic-old-credential"
    assert "recovery copy retained" in caplog.text
    assert "synthetic-new-credential" not in caplog.text
    assert str(backups[0]) not in caplog.text
    # The private copy is usable for controlled recovery after the fault ends.
    os.replace(backups[0], path)
    assert path.read_bytes() == original_env
    assert_clean(api, path, provider_os, env_os)


def test_provider_nul_credential_is_rejected_before_either_file_changes(
    synthetic_settings, monkeypatch,
):
    from backend import endpoints
    api, _settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.setenv(SLOT, "synthetic-old-credential")
    original_env = b"UNRELATED=synthetic-kept\n"
    path.write_bytes(original_env)
    original_provider = endpoints.OVERRIDES_PATH.read_bytes()
    failed = post(client, auth, payload(api_key="synthetic\x00credential"))
    assert failed.status_code == 422
    assert path.read_bytes() == original_env
    assert endpoints.OVERRIDES_PATH.read_bytes() == original_provider
    assert os.environ[SLOT] == "synthetic-old-credential"
    assert_clean(api, path)


@pytest.mark.parametrize("ignored", [None, "", "synthetic••mask"])
def test_metadata_only_provider_save_does_not_read_unusable_env(
    synthetic_settings, monkeypatch, ignored,
):
    api, _settings, _chat, client, auth, path = synthetic_settings
    monkeypatch.setenv(SLOT, "synthetic-old-credential")
    original = b"UNRELATED=synthetic-kept\n\xff\n"
    path.write_bytes(original)
    response = post(client, auth, payload(api_key=ignored))
    assert response.status_code == 200
    assert path.read_bytes() == original
    assert os.environ[SLOT] == "synthetic-old-credential"
    assert_clean(api, path)
