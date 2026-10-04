"""A committed rename needs each actual parent directory synced once."""

import errno
import os

from fastapi.testclient import TestClient
import pytest


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("cross_parent", [False, True])
def test_rename_does_not_fail_after_parent_barriers_already_succeeded(
    app_module, auth, temp_root, monkeypatch, directory, cross_parent,
):
    source = temp_root / "notes" / ("deep" if directory else "a.md")
    content_path = "c.py" if directory else ""
    original = (source / content_path if directory else source).read_bytes()
    destination_parent = temp_root / ("archive" if cross_parent else "notes")
    destination_parent.mkdir(exist_ok=True)
    destination = destination_parent / ("renamed" if directory else "renamed.md")
    parent_ids = {
        (parent.stat().st_dev, parent.stat().st_ino)
        for parent in (source.parent, destination_parent)
    }
    real_fsync = os.fsync
    completed = set()

    def transient_error_on_redundant_sync(fd):
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if identity in parent_ids and identity in completed:
            raise OSError(errno.EIO, "simulated later directory sync failure")
        real_fsync(fd)
        if identity in parent_ids:
            completed.add(identity)

    monkeypatch.setattr(os, "fsync", transient_error_on_redundant_sync)
    client = TestClient(app_module.app, raise_server_exceptions=False)
    try:
        response = client.post("/api/files/rename", headers=auth, json={
            "src": source.relative_to(temp_root).as_posix(),
            "dst": destination.relative_to(temp_root).as_posix(),
        })
    finally:
        client.close()
    # Verify the real syscall and required parent barriers actually completed.
    assert completed == parent_ids
    assert not source.exists()
    assert (destination / content_path if directory else destination).read_bytes() == original
    assert response.status_code == 200
    assert response.json()["path"] == destination.relative_to(temp_root).as_posix()


def test_rename_keeps_parent_sync_failure_visible(
    app_module, auth, temp_root, monkeypatch,
):
    source = temp_root / "notes" / "a.md"
    destination = source.with_name("renamed.md")
    original = source.read_bytes()
    parent_info = source.parent.stat()
    real_fsync = os.fsync

    def fail_required_parent_sync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino):
            raise OSError(errno.EIO, "simulated required directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_required_parent_sync)
    client = TestClient(app_module.app, raise_server_exceptions=False)
    try:
        response = client.post("/api/files/rename", headers=auth, json={
            "src": "notes/a.md", "dst": "notes/renamed.md",
        })
    finally:
        client.close()
    assert response.status_code == 500
    assert not source.exists()
    assert destination.read_bytes() == original
