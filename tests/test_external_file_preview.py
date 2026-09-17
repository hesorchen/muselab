"""Authenticated external reads never expand workspace mutation scope."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def external_file(tmp_path):
    directory = tmp_path / "outside workspace"
    directory.mkdir()
    target = directory / "report notes.md"
    target.write_text("# External report\npreview fixture\n", encoding="utf-8")
    return target


def test_external_text_requires_explicit_scope_and_auth(client, auth, external_file):
    params = {"path": str(external_file), "external": "true"}
    assert client.get("/api/files/read", params=params).status_code == 401
    assert client.get("/api/files/read", params={"path": str(external_file)}, headers=auth).status_code != 200
    response = client.get("/api/files/read", params=params, headers=auth)
    assert response.status_code == 200
    assert response.text == external_file.read_text(encoding="utf-8")
    meta = client.get("/api/files/stat", params=params, headers=auth)
    assert meta.status_code == 200
    assert meta.json()["path"] == str(external_file.resolve())
    assert meta.json()["is_dir"] is False


@pytest.mark.parametrize("filename", [".env", ".env.local", "id_ed25519", "private.pem", ".muselab/state.txt"])
def test_external_sensitive_files_and_aliases_stay_blocked(client, auth, external_file, filename):
    target = external_file.parent / filename
    target.parent.mkdir(exist_ok=True)
    target.write_text("protected fixture", encoding="utf-8")
    alias = external_file.parent / "ordinary.txt"
    alias.symlink_to(target)
    for path in [target, alias]:
        params = {"path": str(path), "external": "true"}
        assert client.get("/api/files/read", params=params, headers=auth).status_code == 403
        assert client.get("/api/files/stat", params=params, headers=auth).status_code == 403
        for endpoint in ["preview-ticket", "download-ticket"]:
            assert client.post(f"/api/files/{endpoint}", json={"path": str(path), "external": True}, headers=auth).status_code == 403


@pytest.mark.parametrize("path", ["../report.md", "report.md", "bad\x00path"])
def test_external_relative_and_invalid_paths_are_rejected(client, auth, path):
    assert client.get("/api/files/read", params={"path": path, "external": "true"}, headers=auth).status_code == 400


def test_external_home_path_and_safe_alias(client, auth, external_file, monkeypatch):
    monkeypatch.setenv("HOME", str(external_file.parent))
    response = client.get("/api/files/stat", params={"path": "~/report notes.md", "external": "true"}, headers=auth)
    assert response.status_code == 200
    assert response.json()["path"] == str(external_file.resolve())
    alias = external_file.parent / "alias.md"
    alias.symlink_to(external_file)
    assert client.get("/api/files/read", params={"path": str(alias), "external": "true"}, headers=auth).status_code == 200


def test_external_preview_tickets_bind_file_and_mode(client, auth, external_file):
    payload = {"path": str(external_file), "external": True}
    assert client.post("/api/files/preview-ticket", json=payload).status_code == 401
    response = client.post("/api/files/preview-ticket", json=payload, headers=auth)
    assert response.status_code == 200
    params = {**payload, "ticket": response.json()["ticket"]}
    assert client.get("/api/files/raw", params=params).status_code == 200
    sibling = external_file.with_name("sibling.md")
    sibling.write_text("different fixture", encoding="utf-8")
    assert client.get("/api/files/raw", params={**params, "path": str(sibling)}).status_code == 401
    assert client.get("/api/files/raw", params={**params, "external": False}).status_code == 401
    assert client.get("/api/files/raw", params=payload).status_code == 401


def test_external_download_is_bound_and_single_use(client, auth, external_file):
    payload = {"path": str(external_file), "external": True}
    response = client.post("/api/files/download-ticket", json=payload, headers=auth)
    assert response.status_code == 200
    params = {**payload, "ticket": response.json()["ticket"]}
    response = client.get("/api/files/download", params=params)
    assert response.status_code == 200
    assert response.content == external_file.read_bytes()
    assert "attachment" in response.headers["content-disposition"]
    assert client.get("/api/files/download", params=params).status_code == 401


def test_external_csv_and_xlsx_share_read_scope(client, auth, external_file):
    from openpyxl import Workbook
    csv = external_file.with_suffix(".csv")
    csv.write_text("name,value\nitem,7\n", encoding="utf-8")
    workbook = Workbook()
    workbook.active.append(["name", "value"])
    workbook.active.append(["item", 7])
    xlsx = external_file.with_suffix(".xlsx")
    workbook.save(xlsx)
    workbook.close()
    for endpoint, path in [("csv", csv), ("xlsx", xlsx)]:
        response = client.get(f"/api/files/{endpoint}", params={"path": str(path), "external": "true"}, headers=auth)
        assert response.status_code == 200
        assert "item" in str(response.json())


def test_external_reads_do_not_expand_directory_or_write_scope(client, auth, external_file):
    before = external_file.read_bytes()
    directory = client.get("/api/files/list", params={"path": str(external_file.parent), "external": "true"}, headers=auth)
    assert directory.status_code != 200
    response = client.put("/api/files/write", params={"external": "true"}, json={"path": str(external_file), "content": "changed"}, headers=auth)
    assert response.status_code in {200, 400, 403, 404}
    assert external_file.read_bytes() == before
    assert client.get("/api/files/read", params={"path": str(external_file.parent), "external": "true"}, headers=auth).status_code == 404


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="Linux virtual filesystem")
def test_external_virtual_process_files_are_not_previewable(client, auth):
    response = client.get("/api/files/read", params={"path": "/proc/self/status", "external": "true"}, headers=auth)
    assert response.status_code == 403


def test_external_legacy_raw_url_redirect_preserves_scope_without_token(client, auth, external_file):
    response = client.get("/api/files/raw", params={
        "path": str(external_file), "external": "true", "preview": "true",
        "token": auth["X-Auth-Token"],
    }, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert "token" not in query
    assert query["external"] == ["1"]
    assert query["preview"] == ["1"]
    assert query["path"] == [str(external_file)]
    assert client.get(location).content == external_file.read_bytes()


def test_external_reads_protect_relocated_internal_state(external_file, monkeypatch):
    from fastapi import HTTPException
    from backend import files

    workspace = external_file.parent.parent / "registered"
    workspace.mkdir()
    (workspace / ".muselab").symlink_to(external_file.parent, target_is_directory=True)
    monkeypatch.setattr(files.workspace_registry, "paths", lambda: [workspace])
    with pytest.raises(HTTPException) as denied:
        files.safe_read_resolve(str(external_file), root=workspace, external=True)
    assert denied.value.status_code == 403
