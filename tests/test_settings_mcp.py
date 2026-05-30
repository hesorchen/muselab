"""Tests for MCP server CRUD endpoints in api_settings.

Uses the shared conftest fixtures (client / auth / app_module) so env / token /
sessions dir setup matches the rest of the suite.
"""
import json
import pytest


@pytest.fixture
def temp_mcp(monkeypatch, tmp_path, app_module):
    """Point MCP_CONFIG_PATH to a tmp file inside the test root.

    Also isolates the new (2026-05-23) Claude Code MCP auto-detect path —
    `_load_external_mcp_sources` scans `~/.claude.json` / `~/.claude/settings.json`
    / `<ROOT>/.mcp.json`. Without isolation, a developer's real `~/.claude.json`
    leaks inherited MCPs into tests (e.g. test_get_empty_returns_empty_list
    expects `servers: []` but sees the host's gmail/whatever entries). Point
    the scan paths into tmp_path so they're guaranteed absent.
    """
    from backend import api_settings
    p = tmp_path / "mcp.json"
    monkeypatch.setattr(api_settings, "MCP_CONFIG_PATH", p)
    monkeypatch.setattr(api_settings, "MCP_EXAMPLE_PATH", tmp_path / "missing.json")
    # Isolate Claude Code MCP auto-detect to tmp paths — must not read
    # the developer's / CI runner's actual ~/.claude.json.
    monkeypatch.setattr(api_settings, "_CLAUDE_USER_JSON", tmp_path / "claude.json")
    monkeypatch.setattr(api_settings, "_CLAUDE_USER_SETTINGS", tmp_path / "claude-settings.json")
    return p


def test_get_empty_returns_empty_list(temp_mcp, client, auth):
    r = client.get("/api/settings/mcp", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"servers": [], "examples": []}


def test_upsert_creates_server(temp_mcp, client, auth):
    body = {"name": "fetch", "command": "uvx",
            "args": ["mcp-server-fetch"], "env": {"X": "1"}, "disabled": False}
    r = client.put("/api/settings/mcp/fetch", json=body, headers=auth)
    assert r.status_code == 200
    assert temp_mcp.exists()
    cfg = json.loads(temp_mcp.read_text())
    assert "fetch" in cfg["mcpServers"]
    assert cfg["mcpServers"]["fetch"]["command"] == "uvx"


def test_upsert_replaces_existing(temp_mcp, client, auth):
    body = {"name": "fetch", "command": "uvx", "args": ["v1"]}
    client.put("/api/settings/mcp/fetch", json=body, headers=auth)
    body2 = {"name": "fetch", "command": "npx", "args": ["v2"]}
    client.put("/api/settings/mcp/fetch", json=body2, headers=auth)
    cfg = json.loads(temp_mcp.read_text())
    assert cfg["mcpServers"]["fetch"]["command"] == "npx"


def test_get_masks_env_values(temp_mcp, client, auth):
    body = {"name": "a", "command": "c", "args": [],
            "env": {"API_KEY": "sk-abcdef0123456789"}}
    client.put("/api/settings/mcp/a", json=body, headers=auth)
    r = client.get("/api/settings/mcp", headers=auth)
    assert r.status_code == 200
    server = r.json()["servers"][0]
    masked = server["env"]["API_KEY"]
    assert masked.startswith("sk-a")
    assert masked.endswith("6789")
    assert "•" in masked


def test_toggle_changes_disabled(temp_mcp, client, auth):
    client.put("/api/settings/mcp/x",
                json={"name": "x", "command": "c"}, headers=auth)
    r = client.patch("/api/settings/mcp/x/toggle",
                       json={"disabled": True}, headers=auth)
    assert r.status_code == 200
    cfg = json.loads(temp_mcp.read_text())
    assert cfg["mcpServers"]["x"]["disabled"] is True


def test_toggle_unknown_returns_404(temp_mcp, client, auth):
    r = client.patch("/api/settings/mcp/ghost/toggle",
                       json={"disabled": True}, headers=auth)
    assert r.status_code == 404


def test_delete_removes_server(temp_mcp, client, auth):
    client.put("/api/settings/mcp/x",
                json={"name": "x", "command": "c"}, headers=auth)
    r = client.delete("/api/settings/mcp/x", headers=auth)
    assert r.status_code == 200
    cfg = json.loads(temp_mcp.read_text())
    assert "x" not in cfg["mcpServers"]


def test_delete_unknown_returns_404(temp_mcp, client, auth):
    r = client.delete("/api/settings/mcp/nope", headers=auth)
    assert r.status_code == 404


def test_examples_from_mcp_example_file(monkeypatch, tmp_path, client, auth, app_module):
    from backend import api_settings
    p = tmp_path / "mcp.json"
    ex = tmp_path / "mcp.json.example"
    ex.write_text(json.dumps({
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"],
                       "description": "HTTP fetch tool"},
        }
    }))
    monkeypatch.setattr(api_settings, "MCP_CONFIG_PATH", p)
    monkeypatch.setattr(api_settings, "MCP_EXAMPLE_PATH", ex)
    r = client.get("/api/settings/mcp", headers=auth)
    assert r.status_code == 200
    examples = r.json()["examples"]
    assert len(examples) == 1
    assert examples[0]["name"] == "fetch"
    assert examples[0]["description"] == "HTTP fetch tool"


def test_unauthorized_get_returns_401(temp_mcp, client):
    r = client.get("/api/settings/mcp")
    assert r.status_code == 401


# --- Remote (http/sse) connectors (2026-05-30) ---

def test_upsert_remote_connector(temp_mcp, client, auth):
    body = {"name": "gmail", "type": "http",
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer secret-token-xyz"}}
    r = client.put("/api/settings/mcp/gmail", json=body, headers=auth)
    assert r.status_code == 200
    cfg = json.loads(temp_mcp.read_text())
    entry = cfg["mcpServers"]["gmail"]
    assert entry["type"] == "http"
    assert entry["url"] == "https://mcp.example.com/sse"
    assert entry["headers"]["Authorization"] == "Bearer secret-token-xyz"
    assert "command" not in entry


def test_remote_connector_listed_with_masked_headers(temp_mcp, client, auth):
    client.put("/api/settings/mcp/gmail", json={
        "name": "gmail", "url": "https://mcp.example.com/sse",
        "headers": {"Authorization": "Bearer secret-token-xyz"}}, headers=auth)
    server = client.get("/api/settings/mcp", headers=auth).json()["servers"][0]
    assert server["type"] == "http"
    assert server["url"] == "https://mcp.example.com/sse"
    assert "•" in server["headers"]["Authorization"]


def test_remote_connector_not_treated_as_stub(temp_mcp, client, auth):
    """A remote spec has no `command`; it must NOT be mistaken for a pure
    {disabled} override stub (regression: it'd be dropped in favour of an
    external entry). Round-tripping it back through GET must preserve url."""
    client.put("/api/settings/mcp/notion", json={
        "name": "notion", "type": "sse",
        "url": "https://notion.example.com/mcp"}, headers=auth)
    server = client.get("/api/settings/mcp", headers=auth).json()["servers"][0]
    assert server["name"] == "notion"
    assert server["type"] == "sse"
    assert server["url"] == "https://notion.example.com/mcp"
    assert server["source"] == "muselab"


def test_upsert_rejects_both_transports(temp_mcp, client, auth):
    r = client.put("/api/settings/mcp/bad", json={
        "name": "bad", "command": "uvx",
        "url": "https://x.example.com"}, headers=auth)
    assert r.status_code == 422


def test_upsert_rejects_no_transport(temp_mcp, client, auth):
    r = client.put("/api/settings/mcp/empty",
                    json={"name": "empty"}, headers=auth)
    assert r.status_code == 422


def test_remote_header_mask_recovery(temp_mcp, client, auth):
    """PUTting back a masked header value must recover the stored secret,
    not persist bullets (mirror of the env-mask guard)."""
    client.put("/api/settings/mcp/gmail", json={
        "name": "gmail", "url": "https://mcp.example.com/sse",
        "headers": {"Authorization": "Bearer secret-token-xyz"}}, headers=auth)
    masked = client.get("/api/settings/mcp", headers=auth).json()[
        "servers"][0]["headers"]["Authorization"]
    assert "•" in masked
    # Echo the masked value back (what a FE save-without-edit would do).
    client.put("/api/settings/mcp/gmail", json={
        "name": "gmail", "url": "https://mcp.example.com/sse",
        "headers": {"Authorization": masked}}, headers=auth)
    cfg = json.loads(temp_mcp.read_text())
    assert cfg["mcpServers"]["gmail"]["headers"]["Authorization"] \
        == "Bearer secret-token-xyz"
