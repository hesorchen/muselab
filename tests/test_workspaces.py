"""Registered workspaces bind files and sessions to the same safe root."""

from urllib.parse import quote


def _make_workspace(tmp_path, name="other"):
    path = tmp_path / name
    path.mkdir()
    (path / "project.txt").write_text("workspace-owned\n", encoding="utf-8")
    return path


def test_register_list_and_remove_workspace(client, auth, temp_root, tmp_path):
    other = _make_workspace(tmp_path)

    created = client.post(
        "/api/chat/workspaces", headers=auth, json={"path": str(other)})
    assert created.status_code == 200
    assert created.json() == {
        "path": str(other.resolve()), "name": "other", "primary": False,
    }

    rows = client.get("/api/chat/workspaces", headers=auth).json()["workspaces"]
    assert rows[0]["path"] == str(temp_root.resolve())
    assert rows[0]["primary"] is True
    assert any(row["path"] == str(other.resolve()) for row in rows)

    removed = client.delete(
        "/api/chat/workspaces", headers=auth, params={"path": str(other)})
    assert removed.status_code == 200
    rows = client.get("/api/chat/workspaces", headers=auth).json()["workspaces"]
    assert [row["path"] for row in rows] == [str(temp_root.resolve())]


def test_workspace_header_and_query_scope_file_access(client, auth, tmp_path):
    other = _make_workspace(tmp_path)
    assert client.post(
        "/api/chat/workspaces", headers=auth, json={"path": str(other)}
    ).status_code == 200

    workspace_headers = {
        **auth,
        "X-Muselab-Workspace": quote(str(other.resolve()), safe=""),
    }
    listing = client.get("/api/files/list?path=", headers=workspace_headers)
    assert listing.status_code == 200
    assert {item["name"] for item in listing.json()["entries"]} == {"project.txt"}

    raw = client.get(
        "/api/files/raw",
        params={"path": "project.txt", "token": auth["X-Auth-Token"],
                "workspace": str(other.resolve())},
    )
    assert raw.status_code == 200
    assert raw.text == "workspace-owned\n"

    # The same relative path does not fall through to another workspace.
    primary = client.get("/api/files/read?path=project.txt", headers=auth)
    assert primary.status_code == 404


def test_session_cwd_must_be_registered_and_is_returned(client, auth, tmp_path):
    other = _make_workspace(tmp_path)
    unregistered = tmp_path / "not-registered"
    unregistered.mkdir()

    rejected = client.post(
        "/api/chat/sessions", headers=auth,
        json={"name": "wrong root", "cwd": str(unregistered)},
    )
    assert rejected.status_code == 400

    client.post("/api/chat/workspaces", headers=auth, json={"path": str(other)})
    created = client.post(
        "/api/chat/sessions", headers=auth,
        json={"name": "workspace chat", "cwd": str(other)},
    )
    assert created.status_code == 200
    assert created.json()["cwd"] == str(other.resolve())
    sid = created.json()["id"]

    rows = client.get("/api/chat/sessions", headers=auth).json()["sessions"]
    assert next(row for row in rows if row["id"] == sid)["cwd"] == str(other.resolve())


def test_removing_workspace_hides_but_does_not_delete_its_session(
    client, auth, app_module, tmp_path,
):
    other = _make_workspace(tmp_path)
    client.post("/api/chat/workspaces", headers=auth, json={"path": str(other)})
    created = client.post(
        "/api/chat/sessions", headers=auth,
        json={"name": "keep on disk", "cwd": str(other)},
    ).json()

    assert client.delete(
        "/api/chat/workspaces", headers=auth, params={"path": str(other)}
    ).status_code == 200
    rows = client.get("/api/chat/sessions", headers=auth).json()["sessions"]
    assert all(row["id"] != created["id"] for row in rows)

    # Registry removal is a view operation; the metadata remains recoverable
    # when the directory is registered again.
    from backend import sessions
    assert any(row["id"] == created["id"] for row in sessions._load_index())
    client.post("/api/chat/workspaces", headers=auth, json={"path": str(other)})
    rows = client.get("/api/chat/sessions", headers=auth).json()["sessions"]
    assert any(row["id"] == created["id"] for row in rows)


def test_primary_and_sensitive_roots_cannot_be_removed_or_registered(
    client, auth, temp_root,
):
    assert client.delete(
        "/api/chat/workspaces", headers=auth, params={"path": str(temp_root)}
    ).status_code == 400
    assert client.post(
        "/api/chat/workspaces", headers=auth, json={"path": "/"}
    ).status_code == 400
    assert client.get(
        "/api/files/list?path=",
        headers={**auth, "X-Muselab-Workspace": quote("/", safe="")},
    ).status_code == 400


def test_workspace_browser_detects_project_folders(client, auth, tmp_path):
    project = _make_workspace(tmp_path, "project-a")
    (project / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")

    # The browser begins at the primary root's parent-accessible scope; ask for
    # the test parent explicitly so the project is a direct child.
    response = client.get(
        "/api/chat/workspaces/browse", headers=auth,
        params={"path": str(tmp_path)},
    )
    assert response.status_code == 200
    row = next(item for item in response.json()["directories"]
               if item["path"] == str(project.resolve()))
    assert row["selectable"] is True
    assert row["project"] == "Python"
