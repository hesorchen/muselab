"""Real PTY terminal API and WebSocket integration."""

from __future__ import annotations

import os
import re
import time

import pytest
from starlette.websockets import WebSocketDisconnect


@pytest.fixture()
def terminal_client(app_module):
    """Keep one TestClient portal alive so terminal background tasks persist."""
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        yield test_client


def _recv_until(ws, needle: bytes, limit: int = 40) -> bytes:
    output = bytearray()
    for _ in range(limit):
        message = ws.receive()
        data = message.get("bytes")
        if data:
            output.extend(data)
            if needle in output:
                return bytes(output)
        if message.get("type") == "websocket.close":
            break
    raise AssertionError(f"terminal output did not contain {needle!r}: {bytes(output)!r}")


def test_terminal_create_list_rename_attach_and_close(
    terminal_client, auth, temp_root,
):
    client = terminal_client
    created = client.post(
        "/api/terminals",
        headers=auth,
        json={"name": "Build", "rows": 30, "cols": 100},
    )
    assert created.status_code == 200, created.text
    terminal = created.json()
    terminal_id = terminal["id"]
    assert terminal["name"] == "Build"
    assert terminal["workspace"] == str(temp_root)
    assert terminal["status"] == "running"

    listed = client.get("/api/terminals", headers=auth)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["terminals"]] == [terminal_id]

    renamed = client.patch(
        f"/api/terminals/{terminal_id}",
        headers=auth,
        json={"name": "Tests"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Tests"

    ticket_response = client.post(
        f"/api/terminals/{terminal_id}/ticket", headers=auth)
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]
    with client.websocket_connect(
        f"/api/terminals/{terminal_id}/ws",
        subprotocols=["muselab-terminal-v1", ticket],
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        ws.send_bytes(b"printf '__MUSELAB_PTY_OK__:%s\\n' \"$PWD\"\n")
        output = _recv_until(ws, str(temp_root).encode())
        assert b"__MUSELAB_PTY_OK__:" in output
        assert str(temp_root).encode() in output
        ws.send_bytes(b"sleep 60 & printf '__BG_PID__:%s\\n' \"$!\"\n")
        background_output = bytearray()
        background_pid = None
        for _ in range(40):
            message = ws.receive()
            background_output.extend(message.get("bytes") or b"")
            match = re.search(rb"__BG_PID__:(\d+)", background_output)
            if match:
                background_pid = int(match.group(1))
                break
        assert background_pid is not None

    deleted = client.delete(f"/api/terminals/{terminal_id}", headers=auth)
    assert deleted.status_code == 200
    assert client.get("/api/terminals", headers=auth).json()["terminals"] == []
    for _ in range(40):
        try:
            os.kill(background_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("closing a terminal left its background job running")


def test_terminal_ticket_is_single_use_and_global_token_is_not_in_shell_env(
    terminal_client, auth,
):
    client = terminal_client
    terminal_id = client.post("/api/terminals", headers=auth, json={}).json()["id"]
    ticket = client.post(
        f"/api/terminals/{terminal_id}/ticket", headers=auth).json()["ticket"]

    with client.websocket_connect(
        f"/api/terminals/{terminal_id}/ws",
        subprotocols=["muselab-terminal-v1", ticket],
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"printf '__TOKEN_ENV__=%s\\n' \"${MUSELAB_TOKEN-unset}\"\n")
        output = _recv_until(ws, b"__TOKEN_ENV__=unset")
        assert b"test-token-123456" not in output

    # A consumed ticket cannot be replayed to attach again.
    try:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                f"/api/terminals/{terminal_id}/ws",
                subprotocols=["muselab-terminal-v1", ticket],
            ):
                pass
        assert rejected.value.code == 1008
    finally:
        client.delete(f"/api/terminals/{terminal_id}", headers=auth)


def test_terminal_workspace_scope_and_terminate_all(
    terminal_client, auth, temp_root, tmp_path,
):
    client = terminal_client
    other = tmp_path / "other-terminal-workspace"
    other.mkdir()
    response = client.post(
        "/api/chat/workspaces", headers=auth, json={"path": str(other)})
    assert response.status_code == 200
    other_headers = {**auth, "X-Muselab-Workspace": str(other)}

    first = client.post("/api/terminals", headers=auth, json={}).json()
    second = client.post("/api/terminals", headers=other_headers, json={}).json()
    assert first["workspace"] == str(temp_root)
    assert second["workspace"] == str(other.resolve())

    assert [row["id"] for row in client.get(
        "/api/terminals", headers=auth).json()["terminals"]] == [first["id"]]
    assert [row["id"] for row in client.get(
        "/api/terminals", headers=other_headers).json()["terminals"]] == [second["id"]]
    assert client.delete(
        f"/api/terminals/{second['id']}", headers=auth).status_code == 404

    closed = client.post("/api/terminals/terminate-all", headers=other_headers)
    assert closed.status_code == 200
    assert closed.json()["closed"] == 1
    assert client.get("/api/terminals", headers=other_headers).json()["terminals"] == []
    client.post("/api/terminals/terminate-all", headers=auth)


def test_terminal_replays_output_after_websocket_disconnect(terminal_client, auth):
    client = terminal_client
    terminal_id = client.post("/api/terminals", headers=auth, json={}).json()["id"]
    ticket = client.post(
        f"/api/terminals/{terminal_id}/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(
        f"/api/terminals/{terminal_id}/ws",
        subprotocols=["muselab-terminal-v1", ticket],
    ) as ws:
        ws.receive_json()
        ws.send_bytes(b"printf '__REPLAY_ME__\\n'\n")
        _recv_until(ws, b"__REPLAY_ME__")

    # Give the detach path a moment to publish its subscriber count.
    time.sleep(0.02)
    ticket2 = client.post(
        f"/api/terminals/{terminal_id}/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(
        f"/api/terminals/{terminal_id}/ws",
        subprotocols=["muselab-terminal-v1", ticket2],
    ) as ws:
        ready = ws.receive_json()
        assert ready["replay_bytes"] > 0
        assert ws.receive_json() == {"type": "replay_start"}
        assert b"__REPLAY_ME__" in ws.receive_bytes()
        assert ws.receive_json() == {"type": "replay_end"}
    client.delete(f"/api/terminals/{terminal_id}", headers=auth)


def test_terminal_profiles_crud_default_and_startup_command(
    terminal_client, auth, temp_root,
):
    client = terminal_client
    created_profile = client.post(
        "/api/terminals/profiles",
        headers=auth,
        json={
            "name": "Dev server",
            "command": "printf '__PROFILE_BOOT__:%s\\n' \"$PWD\"",
            "is_default": True,
        },
    )
    assert created_profile.status_code == 200, created_profile.text
    profile = created_profile.json()
    assert profile["name"] == "Dev server"
    assert profile["is_default"] is True

    listed = client.get("/api/terminals", headers=auth).json()
    assert listed["default_profile_id"] == profile["id"]
    assert listed["profiles"] == [profile]
    profile_file = temp_root / ".muselab" / "terminal_profiles.json"
    assert profile_file.is_file()
    assert profile_file.stat().st_mode & 0o777 == 0o600

    # Omitting profile_id applies the server default and feeds its command to
    # the interactive shell. Output is buffered even before the browser joins.
    terminal = client.post("/api/terminals", headers=auth, json={}).json()
    assert terminal["name"] == "Dev server"
    assert terminal["profile_id"] == profile["id"]
    assert terminal["profile_name"] == "Dev server"
    ticket = client.post(
        f"/api/terminals/{terminal['id']}/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(
        f"/api/terminals/{terminal['id']}/ws",
        subprotocols=["muselab-terminal-v1", ticket],
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        output = _recv_until(
            ws, b"__PROFILE_BOOT__:" + str(temp_root).encode())
        assert str(temp_root).encode() in output

    # A non-default profile must win over the server default when its id is
    # explicitly selected by the client.
    alternate_profile = client.post(
        "/api/terminals/profiles",
        headers=auth,
        json={
            "name": "Alternate",
            "command": "printf '__ALTERNATE_PROFILE__\\n'",
            "is_default": False,
        },
    ).json()
    alternate = client.post(
        "/api/terminals",
        headers=auth,
        json={"profile_id": alternate_profile["id"]},
    ).json()
    assert alternate["name"] == "Alternate"
    assert alternate["profile_id"] == alternate_profile["id"]
    assert alternate["profile_name"] == "Alternate"
    alternate_ticket = client.post(
        f"/api/terminals/{alternate['id']}/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(
        f"/api/terminals/{alternate['id']}/ws",
        subprotocols=["muselab-terminal-v1", alternate_ticket],
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        _recv_until(ws, b"__ALTERNATE_PROFILE__")

    # An explicit empty id remains the escape hatch for a plain shell.
    plain = client.post(
        "/api/terminals", headers=auth, json={"profile_id": ""}).json()
    assert plain["profile_id"] == ""
    assert plain["profile_name"] == ""

    updated = client.patch(
        f"/api/terminals/profiles/{profile['id']}",
        headers=auth,
        json={"name": "Dev", "command": "pwd", "is_default": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Dev"
    assert updated.json()["is_default"] is False
    assert client.get("/api/terminals/profiles", headers=auth).json()[
        "default_profile_id"] == ""

    deleted = client.delete(
        f"/api/terminals/profiles/{profile['id']}", headers=auth)
    assert deleted.status_code == 200
    assert client.delete(
        f"/api/terminals/profiles/{alternate_profile['id']}",
        headers=auth,
    ).status_code == 200
    assert client.get("/api/terminals/profiles", headers=auth).json()["profiles"] == []
    client.post("/api/terminals/terminate-all", headers=auth)
