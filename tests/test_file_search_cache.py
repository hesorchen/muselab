"""Search caches must observe archive/sync operations that preserve mtimes."""
import os

import pytest


@pytest.mark.parametrize("replace_directory", [False, True])
def test_search_refreshes_preserved_mtime_directory(
    client, auth, temp_root, tmp_path, replace_directory,
):
    directory = temp_root / "synced"
    directory.mkdir()
    (directory / "old-file.txt").write_text("old", encoding="utf-8")
    original = directory.stat()

    warm = client.get(
        "/api/files/search", headers=auth, params={"q": "old-file"},
    )
    assert [entry["path"] for entry in warm.json()["entries"]] == ["synced/old-file.txt"]

    if replace_directory:
        directory.rename(tmp_path / "previous-directory")
        directory.mkdir()
    else:
        (directory / "old-file.txt").unlink()
    (directory / "new-file.txt").write_text("new", encoding="utf-8")
    os.utime(directory, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert directory.stat().st_mtime_ns == original.st_mtime_ns

    response = client.get(
        "/api/files/search", headers=auth, params={"q": "new-file"},
    )

    assert response.status_code == 200
    assert [entry["path"] for entry in response.json()["entries"]] == ["synced/new-file.txt"]


def test_search_reuses_unchanged_directory_listings(
    client, auth, temp_root, monkeypatch,
):
    from backend import files

    directory = temp_root / "stable"
    directory.mkdir()
    for index in range(100):
        (directory / f"item-{index:03}.txt").write_text("sample", encoding="utf-8")
    original_scandir = files.os.scandir
    scanned = []

    def counted_scandir(path):
        scanned.append(path)
        return original_scandir(path)

    monkeypatch.setattr(files.os, "scandir", counted_scandir)
    first = client.get(
        "/api/files/search", headers=auth, params={"q": "item-", "limit": 200},
    )
    assert len(first.json()["entries"]) == 100
    assert scanned
    scanned.clear()

    second = client.get(
        "/api/files/search", headers=auth, params={"q": "item-", "limit": 200},
    )
    assert second.json() == first.json()
    assert scanned == []
