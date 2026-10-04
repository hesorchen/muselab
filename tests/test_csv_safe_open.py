"""CSV preview cannot follow a path replaced after read-scope validation."""

import pytest


@pytest.mark.parametrize("replacement", ["sensitive_symlink", "parent_symlink"])
def test_csv_safe_open_rejects_symlink_replacement_after_resolution(
    client, auth, temp_root, tmp_path, monkeypatch, replacement,
):
    from backend import files

    parent = temp_root / "nested"
    parent.mkdir()
    target = parent / "report.csv"
    target.write_text("id,label\n1,PUBLIC_ORIGINAL\n", encoding="utf-8")
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    sensitive = outside / ".env"
    sensitive.write_text("id,label\n1,SYNTHETIC_PRIVATE_REPLACEMENT\n", encoding="utf-8")
    (outside / target.name).write_bytes(sensitive.read_bytes())
    resolve = files.safe_read_resolve
    replaced = False

    def resolve_then_replace(*args, **kwargs):
        nonlocal replaced
        resolved = resolve(*args, **kwargs)
        assert resolved == target
        if replacement == "sensitive_symlink":
            target.unlink()
            target.symlink_to(sensitive)
        else:
            parent.rename(temp_root / "saved")
            parent.symlink_to(outside, target_is_directory=True)
        replaced = True
        return resolved

    monkeypatch.setattr(files, "safe_read_resolve", resolve_then_replace)
    response = client.get("/api/files/csv", params={"path": "nested/report.csv"}, headers=auth)
    assert replaced
    assert response.status_code == 404, response.text
    assert "SYNTHETIC_PRIVATE_REPLACEMENT" not in response.text
    assert response.json()["detail"] == "not a file"


@pytest.mark.parametrize("oversize_field", [False, True])
def test_csv_safe_file_closes_after_success_or_parse_failure(
    client, auth, temp_root, monkeypatch, oversize_field,
):
    import csv
    from backend import files

    target = temp_root / "owned.csv"
    target.write_text(
        "id,label\n1,ordinary\n2,"
        + ("x" * (csv.field_size_limit() + 1) if oversize_field else "normal")
        + "\n", encoding="utf-8-sig",
    )
    opened = []
    open_file = files._open_response_file

    def capture_open(path):
        stream, info = open_file(path)
        opened.append(stream)
        return stream, info

    monkeypatch.setattr(files, "_open_response_file", capture_open)
    response = client.get("/api/files/csv", params={"path": target.name, "limit": 1}, headers=auth)
    assert response.status_code == (422 if oversize_field else 200)
    assert len(opened) == 1 and opened[0].closed
    if oversize_field:
        assert "field" in response.json()["detail"]
    else:
        assert response.json()["rows"] == [["1", "ordinary"]]
        assert response.json()["total_rows"] == 2


def test_csv_replacement_after_safe_open_reads_original_without_caching_stale_path(
    client, auth, temp_root, monkeypatch,
):
    from backend import files

    target = temp_root / "opened.csv"
    target.write_text("id,label\n1,ORIGINAL\n", encoding="utf-8")
    replacement = temp_root / "replacement.csv"
    replacement.write_text("id,label\n1,REPLACEMENT\n2,OTHER\n", encoding="utf-8")
    opened = []
    open_file = files._open_response_file
    replaced = False

    def open_then_replace(path):
        nonlocal replaced
        stream, info = open_file(path)
        opened.append(stream)
        replacement.replace(target)
        replaced = True
        return stream, info

    monkeypatch.setattr(files, "_open_response_file", open_then_replace)
    response = client.get("/api/files/csv", params={"path": target.name}, headers=auth)
    assert replaced
    assert response.status_code == 200
    assert response.json()["rows"] == [["1", "ORIGINAL"]]
    assert response.json()["total_rows"] == 1
    assert len(opened) == 1 and opened[0].closed
    with files._CSV_TOTAL_CACHE_LOCK:
        assert str(target) not in files._CSV_TOTAL_CACHE
