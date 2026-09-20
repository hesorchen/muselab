"""File preview races must preserve the missing-file HTTP contract."""
import pytest


@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_text_preview_handles_file_removed_after_content_sniff(
    client, auth, temp_root, monkeypatch, replacement,
):
    from backend import files

    target = temp_root / "changing-preview.txt"
    target.write_text("synthetic preview contents", encoding="utf-8")
    sniff = files._looks_binary

    def sniff_then_remove(path):
        result = sniff(path)
        if path == target:
            target.unlink()
            if replacement == "directory":
                target.mkdir()
        return result

    monkeypatch.setattr(files, "_looks_binary", sniff_then_remove)
    response = client.get(
        "/api/files/read", params={"path": target.name}, headers=auth,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "not a file"
