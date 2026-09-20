"""Tests for MCP server CRUD endpoints in api_settings.

Uses the shared conftest fixtures (client / auth / app_module) so env / token /
sessions dir setup matches the rest of the suite.
"""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_toggle_propagates_through_sdk_control_channel(
    temp_mcp, client, auth,
):
    from backend import chat as chat_mod

    class LiveClient:
        def __init__(self):
            self.calls = []

        async def toggle_mcp_server(self, name, enabled):
            self.calls.append((name, enabled))

    live = LiveClient()
    key = ("sid-mcp-toggle", "claude-sonnet-4-6", "auto", "")
    client.put(
        "/api/settings/mcp/x",
        json={"name": "x", "command": "c"},
        headers=auth,
    )
    chat_mod._clients[key] = live
    try:
        response = client.patch(
            "/api/settings/mcp/x/toggle",
            json={"disabled": True},
            headers=auth,
        )
    finally:
        chat_mod._clients.pop(key, None)

    assert response.status_code == 200, response.text
    assert response.json()["propagated"] == [
        "sid-mcp-toggle@claude-sonnet-4-6",
    ]
    assert response.json()["errors"] == []
    assert live.calls == [("x", False)]


def test_reconnect_propagates_through_sdk_control_channel(client, auth):
    from backend import chat as chat_mod

    class LiveClient:
        def __init__(self):
            self.calls = []

        async def reconnect_mcp_server(self, name):
            self.calls.append(name)

    live = LiveClient()
    key = ("sid-mcp-reconnect", "claude-sonnet-4-6", "auto", "")
    chat_mod._clients[key] = live
    try:
        response = client.post(
            "/api/settings/mcp/x/reconnect",
            headers=auth,
        )
    finally:
        chat_mod._clients.pop(key, None)

    assert response.status_code == 200, response.text
    assert response.json()["reconnected"] == [
        "sid-mcp-reconnect@claude-sonnet-4-6",
    ]
    assert response.json()["errors"] == []
    assert live.calls == ["x"]


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


@pytest.mark.parametrize("second_operation", ["upsert", "delete", "toggle"])
def test_concurrent_mcp_mutations_preserve_each_successful_edit(
    temp_mcp, client, auth, monkeypatch, second_operation,
):
    from backend import api_settings

    initial = {
        "futureField": {"keep": True},
        "mcpServers": {
            "alpha": {"command": "synthetic", "args": ["old"]},
            "beta": {"command": "synthetic", "args": ["old"], "disabled": False},
        },
    }
    temp_mcp.write_text(json.dumps(initial), encoding="utf-8")
    first_saving = threading.Event()
    second_read = threading.Event()
    load = api_settings._load_mcp
    save = api_settings._save_mcp

    def observed_load():
        cfg = load()
        if first_saving.is_set():
            second_read.set()
        return cfg

    def gated_save(cfg):
        if not first_saving.is_set():
            first_saving.set()
            # Before the fix, the second request reads the old file while the
            # first write is paused. A serialized transaction waits here, then
            # reads after the first request commits.
            second_read.wait(timeout=1)
        save(cfg)

    monkeypatch.setattr(api_settings, "_load_mcp", observed_load)
    monkeypatch.setattr(api_settings, "_save_mcp", gated_save)

    def edit_alpha():
        return client.put(
            "/api/settings/mcp/alpha", headers=auth,
            json={"name": "alpha", "command": "synthetic", "args": ["new"]},
        )

    def edit_beta():
        if second_operation == "delete":
            return client.delete("/api/settings/mcp/beta", headers=auth)
        if second_operation == "toggle":
            return client.patch(
                "/api/settings/mcp/beta/toggle", headers=auth,
                json={"disabled": True},
            )
        return client.put(
            "/api/settings/mcp/beta", headers=auth,
            json={"name": "beta", "command": "synthetic", "args": ["new"]},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(edit_alpha)
        assert first_saving.wait(timeout=5)
        second = executor.submit(edit_beta)
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == 200

    saved = json.loads(temp_mcp.read_text(encoding="utf-8"))
    assert saved["futureField"] == {"keep": True}
    assert saved["mcpServers"]["alpha"]["args"] == ["new"]
    if second_operation == "delete":
        assert "beta" not in saved["mcpServers"]
    elif second_operation == "toggle":
        assert saved["mcpServers"]["beta"]["disabled"] is True
    else:
        assert saved["mcpServers"]["beta"]["args"] == ["new"]


@pytest.mark.asyncio
async def test_overlapping_mcp_toggles_keep_live_client_at_latest_saved_state(
    temp_mcp, monkeypatch,
):
    from backend import api_settings, chat

    temp_mcp.write_text(json.dumps({
        "mcpServers": {"sample": {"command": "synthetic", "disabled": False}},
    }), encoding="utf-8")
    first_saved = threading.Event()
    second_applied = threading.Event()
    persist = api_settings._persist_mcp_toggle

    def delayed_persist(name, disabled):
        persist(name, disabled)
        if disabled:
            first_saved.set()
            # Delay delivery of the first worker's result until the second
            # toggle applies. A serialized toggle must finish this one first.
            second_applied.wait(timeout=1)

    class LiveClient:
        enabled = True

        async def toggle_mcp_server(self, name, enabled):
            assert name == "sample"
            self.enabled = enabled
            if enabled:
                second_applied.set()

    live = LiveClient()
    monkeypatch.setattr(api_settings, "_persist_mcp_toggle", delayed_persist)
    monkeypatch.setattr(chat, "_clients", {("synthetic-session", "model"): live})
    first = asyncio.create_task(api_settings.toggle_mcp_server(
        "sample", api_settings.MCPToggleReq(disabled=True),
    ))
    second = None
    try:
        assert await asyncio.to_thread(first_saved.wait, 5)
        second = asyncio.create_task(api_settings.toggle_mcp_server(
            "sample", api_settings.MCPToggleReq(disabled=False),
        ))
        responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        assert all(response["ok"] and not response["errors"] for response in responses)
    finally:
        second_applied.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        chat._clients.clear()

    saved = json.loads(temp_mcp.read_text(encoding="utf-8"))["mcpServers"]["sample"]
    assert saved["disabled"] is False
    assert live.enabled is True


@pytest.mark.asyncio
async def test_toggle_wait_for_one_mcp_does_not_block_another(temp_mcp, monkeypatch):
    from backend import api_settings, chat

    temp_mcp.write_text(json.dumps({"mcpServers": {
        name: {"command": "synthetic", "disabled": False} for name in ("alpha", "beta")
    }}), encoding="utf-8")
    entered = asyncio.Event()
    release = asyncio.Event()

    class LiveClient:
        async def toggle_mcp_server(self, name, enabled):
            if name == "alpha":
                entered.set()
                await release.wait()

    monkeypatch.setattr(chat, "_clients", {("synthetic-session", "model"): LiveClient()})
    first = asyncio.create_task(api_settings.toggle_mcp_server(
        "alpha", api_settings.MCPToggleReq(disabled=True),
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await asyncio.wait_for(api_settings.toggle_mcp_server(
            "beta", api_settings.MCPToggleReq(disabled=True),
        ), timeout=2)
        assert second["ok"] and not second["errors"]
        assert not first.done()
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=5)
        chat._clients.clear()

    saved = json.loads(temp_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert saved["alpha"]["disabled"] and saved["beta"]["disabled"]
