"""Provider visibility must survive migrations and concurrent partial edits."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from dotenv import dotenv_values


@pytest.mark.parametrize("stored", ["legacy", "stable", "both"])
@pytest.mark.parametrize("request_form", ["legacy", "stable"])
def test_enabling_provider_clears_both_persisted_aliases(
    client, auth, monkeypatch, tmp_path, stored, request_form,
):
    from backend import api_settings, endpoints

    provider = next(item for item in endpoints.provider_meta() if item["env_key"] == "DEEPSEEK_API_KEY")
    stable, legacy = provider["id"], provider["probe_model"]
    assert stable != legacy
    values = {"unrelated-provider"}
    if stored in {"legacy", "both"}:
        values.add(legacy)
    if stored in {"stable", "both"}:
        values.add(stable)
    raw = ",".join(sorted(values))
    path = tmp_path / "visibility.env"
    path.write_text(f"MUSELAB_DISABLED_PROVIDERS={raw}\n", encoding="utf-8")
    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    monkeypatch.setenv("MUSELAB_DISABLED_PROVIDERS", raw)
    before = client.get("/api/settings", headers=auth).json()
    assert next(p for p in before["providers"] if p["id"] == stable)["disabled"]

    response = client.put(
        "/api/settings", headers=auth,
        json={"provider_disabled": {legacy if request_form == "legacy" else stable: False}},
    )
    assert response.status_code == 200
    after = client.get("/api/settings", headers=auth).json()
    assert not next(p for p in after["providers"] if p["id"] == stable)["disabled"]
    assert os.environ["MUSELAB_DISABLED_PROVIDERS"] == "unrelated-provider"
    assert dotenv_values(path)["MUSELAB_DISABLED_PROVIDERS"] == "unrelated-provider"


@pytest.mark.parametrize("disabled", [True, False])
def test_concurrent_visibility_edits_preserve_both_provider_changes(
    client, auth, monkeypatch, tmp_path, disabled,
):
    from backend import api_settings, endpoints

    providers = {item["env_key"]: item["id"] for item in endpoints.provider_meta()}
    first_id = providers["DEEPSEEK_API_KEY"]
    second_id = providers["MINIMAX_API_KEY"]
    initial_values = {"unrelated-provider"}
    if not disabled:
        initial_values.update({first_id, second_id})
    initial = ",".join(sorted(initial_values))
    path = tmp_path / "concurrent-visibility.env"
    path.write_text(f"MUSELAB_DISABLED_PROVIDERS={initial}\n", encoding="utf-8")
    monkeypatch.setattr(api_settings, "ENV_PATH", path)
    monkeypatch.setenv("MUSELAB_DISABLED_PROVIDERS", initial)
    first_write = threading.Event()
    second_write = threading.Event()
    write = api_settings._write_env

    def gated_write(updates):
        if first_write.is_set():
            second_write.set()
        else:
            first_write.set()
            second_write.wait(timeout=1)
        write(updates)

    monkeypatch.setattr(api_settings, "_write_env", gated_write)

    def toggle(provider):
        return client.put(
            "/api/settings", headers=auth,
            json={"provider_disabled": {provider: disabled}},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(toggle, first_id)
        assert first_write.wait(timeout=5)
        second = executor.submit(toggle, second_id)
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == 200

    expected = {"unrelated-provider"}
    if disabled:
        expected.update({first_id, second_id})
    assert set(os.environ["MUSELAB_DISABLED_PROVIDERS"].split(",")) == expected
    assert set(dotenv_values(path)["MUSELAB_DISABLED_PROVIDERS"].split(",")) == expected
