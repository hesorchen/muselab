"""Failed reads of owned MCP configuration must not turn edits into resets."""
import errno
import json

import pytest

from .test_settings_save_consistency import synthetic_settings as synthetic_settings


@pytest.mark.parametrize("operation", ["upsert", "delete", "toggle"])
@pytest.mark.parametrize("read_error", [errno.EIO, errno.EACCES])
def test_mcp_read_failure_preserves_config_and_allows_retry(
    synthetic_settings, monkeypatch, operation, read_error,
):
    api, _, chat, client, auth, _ = synthetic_settings
    path = api.MCP_CONFIG_PATH
    original = {
        "retained": {"synthetic": True},
        "mcpServers": {
            "alpha": {"command": "synthetic-a", "env": {"KEPT": "synthetic-value"}},
            "beta": {"command": "synthetic-b"},
        },
    }
    path.write_text(json.dumps(original) + "\n", encoding="utf-8")
    original_bytes = path.read_bytes()
    # An inherited alpha makes the old toggle path reach its unsafe stub save.
    api._CLAUDE_USER_JSON.write_text(json.dumps({
        "mcpServers": {"alpha": {"command": "synthetic-external"}},
    }), encoding="utf-8")
    armed = True
    reads = []

    class UnreadableConfigPath(type(path)):
        def read_text(self, *args, **kwargs):
            if armed and self == path:
                reads.append(True)
                raise OSError(read_error, "synthetic MCP read failure")
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(api, "MCP_CONFIG_PATH", UnreadableConfigPath(path))
    assert chat._clients == {}

    def mutate():
        if operation == "upsert":
            return client.put("/api/settings/mcp/new", headers=auth,
                              json={"name": "new", "command": "synthetic-new"})
        if operation == "delete":
            return client.delete("/api/settings/mcp/alpha", headers=auth)
        return client.patch("/api/settings/mcp/alpha/toggle", headers=auth,
                            json={"disabled": True})

    try:
        view = client.get("/api/settings/mcp", headers=auth)
        assert view.status_code == 200
        response = mutate()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        receipt = {"status": response.status_code, "bytes_unchanged": path.read_bytes() == original_bytes,
                   "server_count": len(on_disk["mcpServers"]),
                   "original_names_preserved": {"alpha", "beta"}.issubset(on_disk["mcpServers"])}
        assert response.status_code == 503, receipt
        assert reads
        assert path.read_bytes() == original_bytes
        assert set(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == {"alpha", "beta"}
    finally:
        armed = False

    # Disk is actually re-read after the transient fault; no stale empty cache.
    retry = mutate()
    assert retry.status_code == 200
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["retained"] == original["retained"]
    assert after["mcpServers"]["beta"] == original["mcpServers"]["beta"]
    if operation == "upsert":
        assert set(after["mcpServers"]) == {"alpha", "beta", "new"}
        assert after["mcpServers"]["alpha"] == original["mcpServers"]["alpha"]
    elif operation == "delete":
        assert set(after["mcpServers"]) == {"beta"}
    else:
        assert after["mcpServers"]["alpha"] == {
            **original["mcpServers"]["alpha"], "disabled": True,
        }


def test_mcp_mask_recovery_read_failure_does_not_drop_saved_value(synthetic_settings, monkeypatch):
    api, _, _, client, auth, _ = synthetic_settings
    path = api.MCP_CONFIG_PATH
    original = {"mcpServers": {
        "alpha": {"command": "synthetic-a", "env": {"KEPT": "synthetic-value"}},
        "beta": {"command": "synthetic-b"},
    }}
    path.write_text(json.dumps(original) + "\n", encoding="utf-8")
    original_bytes = path.read_bytes()
    reads = []
    armed = True

    class FailRecoveryReadPath(type(path)):
        def read_text(self, *args, **kwargs):
            if armed and self == path:
                reads.append(True)
                if len(reads) == 2:
                    raise OSError(errno.EIO, "synthetic second MCP read failure")
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(api, "MCP_CONFIG_PATH", FailRecoveryReadPath(path))
    body = {"name": "alpha", "command": "synthetic-edited", "env": {"KEPT": "••••"}}
    try:
        response = client.put("/api/settings/mcp/alpha", headers=auth, json=body)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        receipt = {"status": response.status_code, "bytes_unchanged": path.read_bytes() == original_bytes,
                   "retained_value_present": "KEPT" in on_disk["mcpServers"]["alpha"]["env"]}
        assert response.status_code == 503, receipt
        assert len(reads) == 2
        assert path.read_bytes() == original_bytes
    finally:
        armed = False
    retry = client.put("/api/settings/mcp/alpha", headers=auth, json=body)
    assert retry.status_code == 200
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mcpServers"]["alpha"]["env"] == original["mcpServers"]["alpha"]["env"]
    assert after["mcpServers"]["beta"] == original["mcpServers"]["beta"]


def test_mcp_missing_config_initializes_normally(synthetic_settings):
    api, _, _, client, auth, _ = synthetic_settings
    api.MCP_CONFIG_PATH.unlink()
    response = client.put("/api/settings/mcp/first", headers=auth,
                          json={"name": "first", "command": "synthetic-first"})
    assert response.status_code == 200
    assert set(json.loads(api.MCP_CONFIG_PATH.read_text(encoding="utf-8"))["mcpServers"]) == {"first"}


@pytest.mark.parametrize("invalid", ["json", "top_level", "server_map"])
def test_invalid_mcp_config_is_preserved_until_repaired(synthetic_settings, invalid):
    api, _, _, client, auth, _ = synthetic_settings
    path = api.MCP_CONFIG_PATH
    original = {"retained": True, "mcpServers": {
        "alpha": {"command": "synthetic-a"}, "beta": {"command": "synthetic-b"},
    }}
    good_bytes = (json.dumps(original) + "\n").encode()
    if invalid == "json":
        bad_bytes = good_bytes + b"{"
    elif invalid == "top_level":
        bad_bytes = json.dumps([original]).encode()
    else:
        bad_bytes = json.dumps({**original, "mcpServers": [original["mcpServers"]]}).encode()
    path.write_bytes(bad_bytes)
    # This malformed mapping raises from the old endpoint rather than FastAPI;
    # capture its real HTTP 500 without replacing the application/router.
    client._transport.raise_server_exceptions = False
    response = client.put("/api/settings/mcp/new", headers=auth,
                          json={"name": "new", "command": "synthetic-new"})
    assert response.status_code == 503, {"status": response.status_code,
                                        "bytes_unchanged": path.read_bytes() == bad_bytes}
    assert path.read_bytes() == bad_bytes
    path.write_bytes(good_bytes)
    retry = client.put("/api/settings/mcp/new", headers=auth,
                       json={"name": "new", "command": "synthetic-new"})
    assert retry.status_code == 200
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["retained"] is True
    assert set(after["mcpServers"]) == {"alpha", "beta", "new"}
    assert all(after["mcpServers"][name] == entry for name, entry in original["mcpServers"].items())


def test_mcp_inaccessible_exists_false_is_not_treated_as_missing(synthetic_settings, monkeypatch):
    api, _, _, client, auth, _ = synthetic_settings
    path = api.MCP_CONFIG_PATH
    original = {"mcpServers": {
        "alpha": {"command": "synthetic-a"}, "beta": {"command": "synthetic-b"},
    }}
    path.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = path.read_bytes()
    armed = True
    reads = []

    class InaccessibleConfigPath(type(path)):
        def exists(self, *args, **kwargs):
            if armed and self == path:
                return False  # Exact documented 3.14 result for inaccessible paths.
            return super().exists(*args, **kwargs)

        def read_text(self, *args, **kwargs):
            if armed and self == path:
                reads.append(True)
                raise PermissionError(errno.EACCES, "synthetic MCP read failure")
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(api, "MCP_CONFIG_PATH", InaccessibleConfigPath(path))
    try:
        response = client.put("/api/settings/mcp/new", headers=auth,
                              json={"name": "new", "command": "synthetic-new"})
        assert response.status_code == 503, {"status": response.status_code,
                                            "bytes_unchanged": path.read_bytes() == original_bytes}
        assert reads
        assert path.read_bytes() == original_bytes
    finally:
        armed = False
    retry = client.put("/api/settings/mcp/new", headers=auth,
                       json={"name": "new", "command": "synthetic-new"})
    assert retry.status_code == 200
    after = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert set(after) == {"alpha", "beta", "new"}
    assert all(after[name] == entry for name, entry in original["mcpServers"].items())
