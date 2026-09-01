"""Authenticated CRUD tests for standard Claude hook settings files."""

from __future__ import annotations

import hashlib
import json
import stat

import pytest


@pytest.fixture()
def hook_paths(monkeypatch, tmp_path, temp_root, app_module):
    """Keep user-scope tests away from the developer's real ~/.claude."""
    from backend import hook_settings

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    user = fake_home / ".claude" / "settings.json"
    monkeypatch.setattr(hook_settings, "USER_SETTINGS_PATH", user)
    hook_settings._path_locks.clear()
    return {
        "user": user,
        "project": temp_root / ".claude" / "settings.json",
        "local": temp_root / ".claude" / "settings.local.json",
    }


def _scope(client, auth, scope: str):
    response = client.get(f"/api/settings/hooks/{scope}", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_is_authenticated_and_returns_three_standard_scopes(
    hook_paths, client, auth, temp_root,
):
    assert client.get("/api/settings/hooks").status_code == 401

    response = client.get("/api/settings/hooks", headers=auth)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["workspace"] == str(temp_root)
    assert [item["scope"] for item in payload["scopes"]] == [
        "user", "project", "local",
    ]
    assert [item["path"] for item in payload["scopes"]] == [
        "~/.claude/settings.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
    ]
    assert all(not item["exists"] for item in payload["scopes"])
    assert all(len(item["revision"]) == 64 for item in payload["scopes"])
    assert any(
        item["event"] == "PreToolUse"
        and item["matcher"] == "AskUserQuestion"
        for item in payload["builtin_hooks"]
    )


def test_workspace_query_must_name_a_registered_workspace(
    hook_paths, client, auth, tmp_path,
):
    unregistered = tmp_path / "unregistered"
    unregistered.mkdir()
    response = client.get(
        "/api/settings/hooks",
        params={"workspace": str(unregistered)},
        headers=auth,
    )
    assert response.status_code == 400
    assert "not registered" in response.text


def test_update_preserves_unknown_fields_and_restores_masked_headers(
    hook_paths, client, auth,
):
    path = hook_paths["project"]
    path.parent.mkdir()
    original = {
        "futureTopLevel": {"keep": True},
        "hooks": {
            "PreToolUse": [{
                "matcher": "Bash",
                "futureMatcherField": "keep-group",
                "hooks": [{
                    "type": "http",
                    "url": "https://hooks.example.test/check",
                    "headers": {
                        "Authorization": "Bearer super-secret-token",
                        "X-Public": "also-private",
                    },
                    "futureHandlerField": {"keep": 1},
                }],
            }],
        },
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    path.chmod(0o640)

    loaded = _scope(client, auth, "project")
    visible = loaded["hooks"]["PreToolUse"][0]["hooks"][0]
    masked_auth = visible["headers"]["Authorization"]
    masked_public = visible["headers"]["X-Public"]
    assert "•" in masked_auth and "super-secret-token" not in masked_auth
    assert "•" in masked_public and "also-private" not in masked_public

    response = client.put(
        "/api/settings/hooks/project/handlers",
        headers=auth,
        json={
            "revision": loaded["revision"],
            "event": "PreToolUse",
            "group_index": 0,
            "handler_index": 0,
            "matcher": "Bash|Edit",
            "handler": {
                "timeout": 15,
                "headers": {
                    "Authorization": masked_auth,
                    "X-Public": masked_public,
                },
            },
        },
    )
    assert response.status_code == 200, response.text

    stored = json.loads(path.read_text(encoding="utf-8"))
    group = stored["hooks"]["PreToolUse"][0]
    handler = group["hooks"][0]
    assert stored["futureTopLevel"] == {"keep": True}
    assert group["futureMatcherField"] == "keep-group"
    assert group["matcher"] == "Bash|Edit"
    assert handler["futureHandlerField"] == {"keep": 1}
    assert handler["url"] == "https://hooks.example.test/check"
    assert handler["timeout"] == 15
    assert handler["headers"] == original["hooks"]["PreToolUse"][0][
        "hooks"
    ][0]["headers"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_create_update_delete_handler_round_trip(hook_paths, client, auth):
    local = _scope(client, auth, "local")
    created = client.post(
        "/api/settings/hooks/local/handlers",
        headers=auth,
        json={
            "revision": local["revision"],
            "event": "PostToolUse",
            "matcher": "Edit|Write",
            "handler": {
                "type": "command",
                "command": "./check.sh",
                "futureField": "keep",
            },
        },
    )
    assert created.status_code == 200, created.text
    created_payload = created.json()
    assert created_payload["hooks"]["PostToolUse"][0]["matcher"] \
        == "Edit|Write"
    assert stat.S_IMODE(hook_paths["local"].stat().st_mode) == 0o600

    updated = client.put(
        "/api/settings/hooks/local/handlers",
        headers=auth,
        json={
            "revision": created_payload["revision"],
            "event": "PostToolUse",
            "group_index": 0,
            "handler_index": 0,
            "matcher": "",
            "handler": {"command": "./check-v2.sh", "timeout": 20},
        },
    )
    assert updated.status_code == 200, updated.text
    updated_payload = updated.json()
    group = updated_payload["hooks"]["PostToolUse"][0]
    assert "matcher" not in group
    assert group["hooks"][0]["command"] == "./check-v2.sh"
    assert group["hooks"][0]["futureField"] == "keep"

    deleted = client.request(
        "DELETE",
        "/api/settings/hooks/local/handlers",
        headers=auth,
        json={
            "revision": updated_payload["revision"],
            "event": "PostToolUse",
            "group_index": 0,
            "handler_index": 0,
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["hooks"] == {}
    stored = json.loads(hook_paths["local"].read_text(encoding="utf-8"))
    assert "hooks" not in stored


@pytest.mark.parametrize("scope, expected_mode", [
    ("user", 0o600),
    ("project", 0o644),
    ("local", 0o600),
])
def test_disable_all_hooks_preserves_scope_and_uses_safe_mode(
    hook_paths, client, auth, scope, expected_mode,
):
    loaded = _scope(client, auth, scope)
    response = client.patch(
        f"/api/settings/hooks/{scope}/disable-all",
        headers=auth,
        json={"revision": loaded["revision"], "disabled": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["disableAllHooks"] is True
    stored = json.loads(hook_paths[scope].read_text(encoding="utf-8"))
    assert stored == {"disableAllHooks": True}
    assert stat.S_IMODE(hook_paths[scope].stat().st_mode) == expected_mode


def test_successful_write_invalidates_affected_sdk_runtime(
    hook_paths, client, auth, monkeypatch, temp_root,
):
    from backend import hook_settings

    invalidations = []
    monkeypatch.setattr(
        hook_settings,
        "_runtime_invalidator",
        lambda scope, workspace: invalidations.append((scope, workspace)),
    )
    loaded = _scope(client, auth, "project")

    response = client.patch(
        "/api/settings/hooks/project/disable-all",
        headers=auth,
        json={"revision": loaded["revision"], "disabled": True},
    )

    assert response.status_code == 200, response.text
    assert invalidations == [("project", temp_root)]


def test_stale_revision_rejects_without_overwriting_external_change(
    hook_paths, client, auth,
):
    loaded = _scope(client, auth, "project")
    path = hook_paths["project"]
    path.parent.mkdir()
    external = {"changedElsewhere": True}
    path.write_text(json.dumps(external), encoding="utf-8")

    response = client.post(
        "/api/settings/hooks/project/handlers",
        headers=auth,
        json={
            "revision": loaded["revision"],
            "event": "Stop",
            "handler": {"type": "command", "command": "true"},
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["message"] == "hook settings changed; reload and retry"
    assert len(detail["current_revision"]) == 64
    assert json.loads(path.read_text(encoding="utf-8")) == external


def test_invalid_json_is_rejected_and_never_replaced(hook_paths, client, auth):
    path = hook_paths["project"]
    path.parent.mkdir()
    raw = b'{"hooks": '
    path.write_bytes(raw)
    revision = hashlib.sha256(raw).hexdigest()

    assert _scope(client, auth, "user")["exists"] is False
    read = client.get("/api/settings/hooks/project", headers=auth)
    assert read.status_code == 422
    assert "invalid JSON" in read.text
    mutate = client.patch(
        "/api/settings/hooks/project/disable-all",
        headers=auth,
        json={"revision": revision, "disabled": True},
    )
    assert mutate.status_code == 422
    assert path.read_bytes() == raw


def test_settings_file_symlink_is_rejected_without_touching_target(
    hook_paths, client, auth, tmp_path,
):
    target = tmp_path / "outside.json"
    original = {"outside": "must-not-change"}
    target.write_text(json.dumps(original), encoding="utf-8")
    path = hook_paths["local"]
    path.parent.mkdir()
    path.symlink_to(target)

    read = client.get("/api/settings/hooks/local", headers=auth)
    assert read.status_code == 409
    assert "regular file" in read.text
    mutate = client.patch(
        "/api/settings/hooks/local/disable-all",
        headers=auth,
        json={
            "revision": hashlib.sha256(target.read_bytes()).hexdigest(),
            "disabled": True,
        },
    )
    assert mutate.status_code == 409
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert path.is_symlink()


def test_settings_directory_symlink_cannot_escape_workspace(
    hook_paths, client, auth, tmp_path,
):
    outside = tmp_path / "outside-claude"
    outside.mkdir()
    hook_paths["project"].parent.symlink_to(outside, target_is_directory=True)

    response = client.get("/api/settings/hooks/project", headers=auth)
    assert response.status_code == 409
    assert "symlink" in response.text
    assert not (outside / "settings.json").exists()


def test_masked_header_without_stored_secret_is_rejected(
    hook_paths, client, auth,
):
    loaded = _scope(client, auth, "user")
    response = client.post(
        "/api/settings/hooks/user/handlers",
        headers=auth,
        json={
            "revision": loaded["revision"],
            "event": "PreToolUse",
            "handler": {
                "type": "http",
                "url": "https://hooks.example.test",
                "headers": {"Authorization": "••••••••"},
            },
        },
    )
    assert response.status_code == 422
    assert not hook_paths["user"].exists()
