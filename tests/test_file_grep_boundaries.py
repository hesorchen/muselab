"""Content search must preserve private-file boundaries and bounded reads."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("private_dir", [".muselab", ".muselab-dustbin"])
def test_grep_does_not_read_aliases_into_private_directories(
    client, auth, temp_root, private_dir,
):
    private = temp_root / private_dir
    private.mkdir(exist_ok=True)
    target = private / "search-private.txt"
    target.write_text("synthetic-private-needle", encoding="utf-8")
    (temp_root / "public-alias.txt").symlink_to(target)

    response = client.get(
        "/api/files/grep", headers=auth, params={"q": "synthetic-private-needle"},
    )

    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_grep_still_reads_regular_file_aliases(client, auth, temp_root):
    target = temp_root / "ordinary.txt"
    target.write_text("synthetic-public-needle", encoding="utf-8")
    (temp_root / "public-alias.txt").symlink_to(target)

    response = client.get(
        "/api/files/grep", headers=auth, params={"q": "synthetic-public-needle"},
    )

    assert response.status_code == 200
    assert {hit["path"] for hit in response.json()["hits"]} == {
        "ordinary.txt", "public-alias.txt",
    }


def test_grep_never_opens_named_pipes(client, auth, temp_root, monkeypatch):
    import os

    pipe = temp_root / "writer-pipe.txt"
    os.mkfifo(pipe)
    original_open = Path.open
    opened = []

    def guarded_open(path, *args, **kwargs):
        if path == pipe:
            opened.append(path)
            raise OSError("test prevents a blocking FIFO open")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    response = client.get(
        "/api/files/grep", headers=auth, params={"q": "synthetic-needle"},
    )

    assert response.status_code == 200
    assert response.json()["hits"] == []
    assert opened == []


@pytest.mark.parametrize("private_dir", [".muselab", ".muselab-dustbin"])
def test_grep_skips_relocated_private_directories(
    client, auth, temp_root, tmp_path, monkeypatch, private_dir,
):
    from backend import files

    relocated = temp_root / "relocated-private"
    relocated.mkdir()
    (relocated / "search-private.txt").write_text(
        "synthetic-private-needle", encoding="utf-8",
    )
    other = tmp_path / "other-workspace"
    other.mkdir()
    (other / private_dir).symlink_to(relocated, target_is_directory=True)
    monkeypatch.setattr(files.workspace_registry, "paths", lambda: [temp_root, other])

    response = client.get(
        "/api/files/grep", headers=auth,
        params={"q": "synthetic-private-needle", "show_hidden": True},
    )

    assert response.status_code == 200
    assert response.json()["hits"] == []


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_grep_handles_file_replaced_before_open(
    client, auth, temp_root, monkeypatch, replacement,
):
    import os
    from backend import files

    target = temp_root / "changing.txt"
    target.write_text("original text", encoding="utf-8")
    private = temp_root / ".muselab" / "search-private.txt"
    private.parent.mkdir(exist_ok=True)
    private.write_text("synthetic-private-needle", encoding="utf-8")
    original_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == target and not replaced:
            # Keep a regressed implementation from hanging the test process.
            assert flags & os.O_NONBLOCK
            assert flags & os.O_NOFOLLOW
            replaced = True
            target.unlink()
            if replacement == "fifo":
                os.mkfifo(target)
            else:
                target.symlink_to(private)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", replace_before_open)
    response = client.get(
        "/api/files/grep", headers=auth, params={"q": "synthetic-private-needle"},
    )

    assert replaced
    assert response.status_code == 200
    assert response.json()["hits"] == []
