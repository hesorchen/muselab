"""CSV pagination seeks only to bounded, complete decoded record boundaries."""

import csv
import os
from pathlib import Path
import time

import pytest


class _CountLines:
    def __init__(self, handle, counter):
        self.handle = handle
        self.counter = counter

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self.handle)
        self.counter["lines"] += 1
        return value

    def readline(self, *args):
        value = self.handle.readline(*args)
        self.counter["lines"] += bool(value)
        return value


def _count_open_lines(monkeypatch, target, counter):
    original = Path.open

    def counted_open(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        if path == target and args and args[0] == "r":
            return _CountLines(handle, counter)
        return handle

    monkeypatch.setattr(Path, "open", counted_open)


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [False, True])
def test_cached_csv_page_resumes_multiline_unicode_records_across_handles(
    client, auth, temp_root, monkeypatch, suffix, delimiter, line_ending, bom,
):
    from backend import files

    monkeypatch.setattr(files, "CSV_SEEK_STRIDE", 16)
    target = temp_root / f"indexed.{suffix}"
    data = [[str(index), f"雪,{index}\nsecond line", str(index * 2)] for index in range(256)]
    with target.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator=line_ending)
        writer.writerow(["id", "description", "value"])
        writer.writerows(data)
    first = client.get("/api/files/csv", params={"path": target.name, "limit": 3}, headers=auth)
    assert first.status_code == 200
    assert first.json()["total_rows"] == len(data)
    assert first.json()["rows"] == data[:3]

    counter = {"lines": 0}
    _count_open_lines(monkeypatch, target, counter)
    deep = client.get("/api/files/csv", params={
        "path": target.name, "offset": 241, "limit": 3,
    }, headers=auth)
    assert deep.status_code == 200
    body = deep.json()
    assert body["header"] == ["id", "description", "value"]
    assert body["rows"] == data[241:244]
    assert body["total_rows"] == len(data)
    assert body["delimiter"] == delimiter
    # Count actual consumed lines, not elapsed time: a full parse needs >480.
    assert counter["lines"] < 32


@pytest.mark.parametrize("line_ending", [b"\n", b"\r", b"\r\n"])
def test_cached_csv_seek_preserves_bom_no_header_and_replacement_decoding(
    client, auth, temp_root, monkeypatch, line_ending,
):
    from backend import files

    monkeypatch.setattr(files, "CSV_SEEK_STRIDE", 16)
    target = temp_root / "no-header.csv"
    target.write_bytes(b"\xef\xbb\xbf" + b"".join(
        str(index).encode() + b",bad-\xff-value" + line_ending for index in range(128)
    ))
    first = client.get("/api/files/csv", params={"path": target.name}, headers=auth)
    assert first.status_code == 200
    assert first.json()["has_header"] is False
    assert first.json()["total_rows"] == 128
    deep = client.get("/api/files/csv", params={
        "path": target.name, "offset": 115, "limit": 2,
    }, headers=auth)
    assert deep.status_code == 200
    assert deep.json()["rows"] == [["115", "bad-�-value"], ["116", "bad-�-value"]]
    assert deep.json()["header"] == []


@pytest.mark.parametrize("replace", [False, True])
def test_cached_csv_seek_invalidates_equal_size_and_mtime_layout_changes(
    client, auth, temp_root, replace,
):
    target = temp_root / "layout.csv"
    before = "id,label\n" + "".join(
        f"{index},{'alpha' if index < 2000 else 'alpha' * 5}\n" for index in range(4000)
    )
    after = "id,label\n" + "".join(
        f"{index},{'bravo' * 5 if index < 2000 else 'bravo'}\n" for index in range(4000)
    )
    assert len(before.encode()) == len(after.encode())
    target.write_text(before, encoding="utf-8")
    assert client.get("/api/files/csv", params={"path": target.name}, headers=auth).status_code == 200
    info = target.stat()
    time.sleep(0.01)
    if replace:
        staged = temp_root / "new.csv"
        staged.write_text(after, encoding="utf-8")
        os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
        staged.replace(target)
    else:
        target.write_text(after, encoding="utf-8")
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert target.stat().st_size == info.st_size
    assert target.stat().st_mtime_ns == info.st_mtime_ns
    assert (target.stat().st_ino, target.stat().st_ctime_ns) != (info.st_ino, info.st_ctime_ns)
    response = client.get("/api/files/csv", params={
        "path": target.name, "offset": 3900, "limit": 2,
    }, headers=auth)
    assert response.status_code == 200
    assert response.json()["rows"] == [["3900", "bravo"], ["3901", "bravo"]]
    assert response.json()["total_rows"] == 4000


def test_csv_cache_compacts_checkpoints_and_evicts_files_without_losing_rows(
    client, auth, temp_root, monkeypatch,
):
    from backend import files

    monkeypatch.setattr(files, "CSV_SEEK_STRIDE", 2)
    monkeypatch.setattr(files, "CSV_SEEK_MAX_CHECKPOINTS", 4)
    monkeypatch.setattr(files, "CSV_TOTAL_CACHE_MAX", 3)
    text = "id,label\n" + "".join(f"{index},row-{index}\n" for index in range(2000))
    for index in range(7):
        (temp_root / f"cached-{index}.csv").write_text(text, encoding="utf-8")
    for index in range(6):
        response = client.get("/api/files/csv", params={"path": f"cached-{index}.csv"}, headers=auth)
        assert response.status_code == 200
        assert response.json()["total_rows"] == 2000
    counter = {"lines": 0}
    _count_open_lines(monkeypatch, temp_root / "cached-3.csv", counter)
    last = client.get("/api/files/csv", params={
        "path": "cached-3.csv", "offset": 1997, "limit": 10,
    }, headers=auth)
    assert last.status_code == 200
    assert last.json()["rows"] == [[str(index), f"row-{index}"] for index in range(1997, 2000)]
    assert last.json()["total_rows"] == 2000
    assert counter["lines"] < 1024
    assert client.get("/api/files/csv", params={"path": "cached-6.csv"}, headers=auth).status_code == 200
    with files._CSV_TOTAL_CACHE_LOCK:
        entries = list(files._CSV_TOTAL_CACHE.items())
    assert {Path(path).name for path, _ in entries} == {"cached-3.csv", "cached-5.csv", "cached-6.csv"}
    assert all(len(entry[2]) <= 4 for _, entry in entries)


def test_csv_replacement_before_open_uses_new_inode_for_cached_pagination(
    client, auth, temp_root, monkeypatch,
):
    target = temp_root / "replaced.csv"
    target.write_text("id,label\n" + "".join(f"{index},old\n" for index in range(128)))
    assert client.get("/api/files/csv", params={"path": target.name}, headers=auth).json()["total_rows"] == 128
    staged = temp_root / "staged.csv"
    staged.write_text("id,label\n" + "".join(f"{index},new\n" for index in range(256)))
    original = Path.open
    replaced = False

    def replace_before_open(path, *args, **kwargs):
        nonlocal replaced
        if path == target and args and args[0] == "r" and not replaced:
            replaced = True
            staged.replace(target)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)
    response = client.get("/api/files/csv", params={
        "path": target.name, "offset": 200, "limit": 2,
    }, headers=auth)
    assert response.status_code == 200
    assert response.json()["total_rows"] == 256
    assert response.json()["rows"] == [["200", "new"], ["201", "new"]]


def test_csv_seek_offset_and_parser_limits_keep_existing_contract(
    client, auth, temp_root,
):
    target = temp_root / "bounds.csv"
    target.write_text("id,label\n" + "".join(f"{index},row\n" for index in range(250)))
    first = client.get("/api/files/csv", params={
        "path": target.name, "offset": -1, "limit": 0,
    }, headers=auth)
    assert first.status_code == 200
    assert first.json()["offset"] == 0
    assert first.json()["limit"] == 200
    assert len(first.json()["rows"]) == 200
    for offset in (250, 100_000_000):
        response = client.get("/api/files/csv", params={
            "path": target.name, "offset": offset, "limit": 100_000,
        }, headers=auth)
        assert response.status_code == 200
        assert response.json()["rows"] == []
        assert response.json()["total_rows"] == 250
        assert response.json()["limit"] == 1000
    invalid = temp_root / "oversized-field.csv"
    invalid.write_text("id,label\n0,first\n1," + "x" * 140_000 + "\n")
    response = client.get("/api/files/csv", params={
        "path": invalid.name, "limit": 1,
    }, headers=auth)
    # Counting must still parse the trailing records, even beyond the page.
    assert response.status_code == 422
