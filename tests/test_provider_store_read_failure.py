"""A failed provider-store read must not authorize rewriting it as empty."""
import errno
import json

from fastapi.testclient import TestClient
import pytest

from tests.test_settings_save_consistency import synthetic_settings as synthetic_settings


def body(prefix="synthetic-new:"):
    return {"base_url": "https://provider.example.test/anthropic", "prefix": prefix,
            "models": [prefix + "model"]}


def post(client, auth, payload, route="/api/settings/providers"):
    with TestClient(client.app, raise_server_exceptions=False) as failing_client:
        return failing_client.post(route, headers=auth, json=payload)


def seed(client, auth):
    from backend import endpoints
    saved = post(client, auth, body("synthetic-retained:"))
    assert saved.status_code == 200
    deleted = "b:deepseek-"
    assert post(client, auth, {"id": deleted}, "/api/settings/providers/delete").status_code == 200
    assert post(client, auth, {"models": ["claude-synthetic-retained"]},
                "/api/settings/providers/anthropic-models").status_code == 200
    return endpoints.OVERRIDES_PATH, saved.json()["id"], deleted


def io_fault(monkeypatch, endpoints, path, fault):
    state = {"armed": True, "stat_calls": 0, "read_calls": 0}

    class FaultPath(type(path)):
        # Only this synthetic path object is faulted. The real writer and
        # unrelated filesystem/process operations keep their actual methods.
        def stat(self, *args, **kwargs):
            state["stat_calls"] += 1
            if state["armed"] and fault == "stat-eio":
                raise OSError(errno.EIO, "controlled synthetic provider stat failure")
            return super().stat(*args, **kwargs)

        def read_text(self, *args, **kwargs):
            state["read_calls"] += 1
            if state["armed"] and fault == "read-eio":
                raise OSError(errno.EIO, "controlled synthetic provider read failure")
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", FaultPath(path))
    monkeypatch.setattr(endpoints, "_OVERRIDES_CACHE", None)
    monkeypatch.setattr(endpoints, "_CATALOG_CACHE", None)
    return state


@pytest.mark.parametrize("fault", ["stat-eio", "read-eio", "malformed-json", "decode"])
def test_provider_write_rejects_failed_cold_read_and_recovers_without_losing_state(
    synthetic_settings, monkeypatch, fault,
):
    from backend import endpoints
    _api, _settings, _chat, client, auth, _env = synthetic_settings
    path, retained, deleted = seed(client, auth)
    good = path.read_bytes()
    before = good + {"malformed-json": b" broken-json", "decode": b"\xff"}.get(fault, b"")
    path.write_bytes(before)
    state = io_fault(monkeypatch, endpoints, path, fault)
    failed = post(client, auth, body())
    assert failed.status_code == 500  # Stored JSON/decode errors are not metadata 422.
    assert path.read_bytes() == before
    assert retained.encode() in before and deleted.encode() in before
    if fault.endswith("eio"):
        assert state["stat_calls" if fault == "stat-eio" else "read_calls"] > 0
    if fault != "decode":
        # Preserve the existing read-only degradation for unavailable/invalid
        # stores. The strict branch is used only by mutation transactions.
        assert endpoints._load_overrides()["providers"] == {}
    state["armed"] = False
    if fault in ("malformed-json", "decode"):
        path.write_bytes(good)
    retry = post(client, auth, body())
    assert retry.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert {retained, retry.json()["id"]} == set(saved["providers"])
    assert deleted in saved["deleted"]
    assert saved["anthropic_models"] == ["claude-synthetic-retained"]
    assert not list(path.parent.glob("provider_overrides.json.tmp.*"))


@pytest.mark.parametrize("route,payload", [
    ("/api/settings/providers/delete", {"id": "b:codex:"}),
    ("/api/settings/providers/restore", {"id": "b:deepseek-"}),
    ("/api/settings/providers/anthropic-models", {"models": ["claude-synthetic-next"]}),
    ("/api/settings/providers/restore", {"id": "anthropic"}),
])
def test_other_provider_store_mutations_reject_unreadable_state(
    synthetic_settings, monkeypatch, route, payload,
):
    from backend import endpoints
    _api, _settings, _chat, client, auth, _env = synthetic_settings
    path, retained, deleted = seed(client, auth)
    before = path.read_bytes()
    state = io_fault(monkeypatch, endpoints, path, "read-eio")
    failed = post(client, auth, payload, route)
    assert failed.status_code == 500
    assert state["read_calls"] > 0
    assert path.read_bytes() == before
    state["armed"] = False
    retry = post(client, auth, payload, route)
    assert retry.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert retained in saved["providers"]
    if payload.get("id") != deleted:
        assert deleted in saved["deleted"]
    else:
        assert deleted not in saved["deleted"]


@pytest.mark.parametrize("invalid", [
    ["invalid-top-level"], {"providers": []}, {"deleted": {}}, {"anthropic_models": {}},
])
def test_invalid_provider_store_containers_block_writes(synthetic_settings, invalid):
    from backend import endpoints
    _api, _settings, _chat, client, auth, _env = synthetic_settings
    before = json.dumps(invalid).encode()
    endpoints.OVERRIDES_PATH.write_bytes(before)
    failed = post(client, auth, body())
    assert failed.status_code == 500
    assert endpoints.OVERRIDES_PATH.read_bytes() == before


def test_missing_provider_store_and_partial_builtin_override_remain_writable(synthetic_settings):
    from backend import endpoints
    _api, _settings, _chat, client, auth, _env = synthetic_settings
    path = endpoints.OVERRIDES_PATH
    path.unlink()
    first = post(client, auth, body("synthetic-first:"))
    assert first.status_code == 200
    partial = json.loads(path.read_text(encoding="utf-8"))
    partial["providers"]["b:deepseek-"] = {"display": "synthetic-partial-override"}
    path.write_text(json.dumps(partial), encoding="utf-8")
    second = post(client, auth, body())
    assert second.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved["providers"]) == {first.json()["id"], second.json()["id"], "b:deepseek-"}
    assert saved["providers"]["b:deepseek-"] == {"display": "synthetic-partial-override"}
    assert endpoints.get_provider("b:deepseek-").display == "synthetic-partial-override"


def test_stat_missing_does_not_skip_later_existing_provider_store(synthetic_settings, monkeypatch):
    from backend import endpoints
    _api, _settings, _chat, client, auth, _env = synthetic_settings
    path, retained, deleted = seed(client, auth)
    state = {"missed": False, "read_after_miss": False}

    class MissingOnce(type(path)):
        def stat(self, *args, **kwargs):
            if not state["missed"]:
                state["missed"] = True
                raise FileNotFoundError("controlled transient stat miss")
            return super().stat(*args, **kwargs)

        def read_text(self, *args, **kwargs):
            state["read_after_miss"] |= state["missed"]
            return super().read_text(*args, **kwargs)

    # Exercise the strict reader directly: even when stat says absent, its
    # actual read of the existing synthetic file must establish the baseline.
    monkeypatch.setattr(endpoints, "OVERRIDES_PATH", MissingOnce(path))
    loaded = endpoints._load_overrides(for_write=True)
    assert state == {"missed": True, "read_after_miss": True}
    assert retained in loaded["providers"] and deleted in loaded["deleted"]
