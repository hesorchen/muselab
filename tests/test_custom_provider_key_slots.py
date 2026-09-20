"""Automatically created provider credentials belong to one provider."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolate_custom_credentials():
    before = {k: v for k, v in os.environ.items() if k.startswith("MUSELAB_PROVIDER_")}
    yield
    for key in list(os.environ):
        if key.startswith("MUSELAB_PROVIDER_"):
            os.environ.pop(key, None)
    os.environ.update(before)


def test_same_endpoint_providers_get_separate_automatic_key_slots(
    client, auth, monkeypatch, tmp_path,
):
    from backend import endpoints

    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", tmp_path / "providers.json")
    providers = []
    for prefix, key in [("account-a:", "synthetic-account-a"), ("account-b:", "synthetic-account-b")]:
        response = client.post("/api/settings/providers", headers=auth, json={
            "base_url": "https://shared.example.test/anthropic",
            "prefix": prefix,
            "models": [prefix + "model"],
            "api_key": key,
        })
        assert response.status_code == 200
        providers.append(response.json())
    first, second = providers
    assert first["id"] != second["id"]
    assert first["env_key"] != second["env_key"]
    assert os.environ[endpoints.lookup("account-a:model").env_key] == "synthetic-account-a"
    assert os.environ[endpoints.lookup("account-b:model").env_key] == "synthetic-account-b"


@pytest.mark.parametrize("builtin", [False, True], ids=["custom", "builtin"])
def test_edit_without_env_key_preserves_existing_credential_slot(
    client, auth, monkeypatch, tmp_path, builtin,
):
    from backend import endpoints

    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", tmp_path / "providers.json")
    slot = "DEEPSEEK_API_KEY" if builtin else "MUSELAB_PROVIDER_EXPLICIT_SLOT_API_KEY"
    prefix = "deepseek-" if builtin else "editable:"
    monkeypatch.delenv(slot, raising=False)
    response = client.post("/api/settings/providers", headers=auth, json={
        "id": "b:deepseek-" if builtin else None,
        "base_url": "https://old.example.test/anthropic",
        "prefix": prefix,
        "models": [prefix + "model"],
        "env_key": slot,
        "api_key": "synthetic-existing-credential",
    })
    assert response.status_code == 200
    saved = response.json()
    response = client.post("/api/settings/providers", headers=auth, json={
        "id": saved["id"],
        "base_url": "https://new.example.test/anthropic",
        "prefix": prefix,
        "models": [prefix + "model"],
    })
    assert response.status_code == 200
    assert response.json()["env_key"] == slot
    assert response.json()["configured"] is True
    assert endpoints.lookup(prefix + "model").env_key == slot
