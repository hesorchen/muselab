"""CSV preview preserves the standard writer's doubled-quote field values."""

import csv

import pytest


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t"), ("csv", ";")])
@pytest.mark.parametrize("bom", [False, True])
@pytest.mark.parametrize("value", [
    "", "plain", '"quoted"', 'line\nsecond "quoted"', 'line\n""', "has,{index};\tvalue",
], ids=["empty", "plain", "quotes", "multiline-quotes", "multiline-only-quotes", "delimiters"])
def test_csv_preview_roundtrips_standard_writer_values(
    client, auth, temp_root, monkeypatch, suffix, delimiter, bom, value,
):
    from backend import files

    monkeypatch.setattr(files, "CSV_SEEK_STRIDE", 2)
    target = temp_root / f"roundtrip.{suffix}"
    data = [[str(index), value.format(index=index)] for index in range(4)]
    with target.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        csv.writer(handle, delimiter=delimiter, lineterminator="\n").writerows(data)
    first = client.get("/api/files/csv", params={
        "path": target.name, "limit": 2,
    }, headers=auth)
    assert first.status_code == 200
    body = first.json()
    assert body["header"] == []
    assert body["has_header"] is False
    assert body["total_rows"] == len(data)
    assert body["delimiter"] == delimiter
    assert body["rows"] == data[:2]

    later = client.get("/api/files/csv", params={
        "path": target.name, "offset": 3, "limit": 1,
    }, headers=auth)
    assert later.status_code == 200
    assert later.json()["rows"] == data[3:4]
    assert later.json()["total_rows"] == len(data)
    assert later.json()["header"] == []


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t"), ("csv", ";")])
def test_csv_preview_keeps_detected_header_with_multiline_doubled_quotes(
    client, auth, temp_root, suffix, delimiter,
):
    target = temp_root / f"header.{suffix}"
    header = ["id", "description"]
    data = [[str(index), 'line\nsecond "quoted"'] for index in range(4)]
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(data)
    for offset in (0, 3):
        response = client.get("/api/files/csv", params={
            "path": target.name, "offset": offset, "limit": 1,
        }, headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["header"] == header
        assert body["has_header"] is True
        assert body["total_rows"] == len(data)
        assert body["rows"] == data[offset:offset + 1]
