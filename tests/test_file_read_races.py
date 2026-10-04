"""File preview races must preserve the missing-file HTTP contract."""
import pytest


@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_text_preview_handles_file_removed_before_final_open(
    client, auth, temp_root, monkeypatch, replacement,
):
    from backend import files

    target = temp_root / "changing-preview.txt"
    target.write_text("synthetic preview contents", encoding="utf-8")
    open_file = files._open_response_file

    def remove_before_open(path):
        # The reader now retains its FD after sniffing; removal must happen
        # before the final open to exercise the missing-file 404 contract.
        if path == target:
            target.unlink()
            if replacement == "directory":
                target.mkdir()
        return open_file(path)

    monkeypatch.setattr(files, "_open_response_file", remove_before_open)
    response = client.get(
        "/api/files/read", params={"path": target.name}, headers=auth,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "not a file"
