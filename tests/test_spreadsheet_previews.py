"""Spreadsheet previews preserve cells across supported export formats."""

import pytest


@pytest.mark.parametrize("suffix,delimiter", [("csv", ","), ("tsv", "\t")])
def test_csv_preview_bom_keeps_quoted_header_and_pagination(client, auth, temp_root, suffix, delimiter):
    text = f'"label{delimiter}detail"{delimiter}count\nalpha{delimiter}1\nbeta{delimiter}2\n'
    plain = f"plain.{suffix}"
    marked = f"marked.{suffix}"
    (temp_root / plain).write_text(text, encoding="utf-8")
    (temp_root / marked).write_text(text, encoding="utf-8-sig")

    for offset in (0, 1):
        params = {"offset": offset, "limit": 1}
        expected = client.get("/api/files/csv", params={"path": plain, **params}, headers=auth)
        actual = client.get("/api/files/csv", params={"path": marked, **params}, headers=auth)
        assert expected.status_code == actual.status_code == 200
        expected_body = expected.json()
        actual_body = actual.json()
        expected_body.pop("path")
        actual_body.pop("path")
        assert expected_body["header"] == [f"label{delimiter}detail", "count"]
        assert expected_body["total_rows"] == 2
        assert actual_body == expected_body


def test_xlsx_preview_skips_chart_sheets_without_hiding_data(client, auth, temp_root, monkeypatch):
    import openpyxl
    from openpyxl.chart import BarChart, Reference
    from backend import files

    book = openpyxl.Workbook()
    data = book.active
    data.title = "Data"
    data.append(["Label", "Value"])
    data.append(["Example", 7])
    chart = BarChart()
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=2), titles_from_data=True)
    book.create_chartsheet("Chart", index=0).add_chart(chart)
    book.save(temp_root / "with-chart.xlsx")
    book.close()
    monkeypatch.setattr(files, "XLSX_MAX_SHEETS", 1)

    response = client.get("/api/files/xlsx", params={"path": "with-chart.xlsx"}, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["sheets_truncated"] is False
    assert body["sheets"] == [{
        "name": "Data", "rows": [["Label", "Value"], ["Example", "7"]],
        "rows_truncated": False, "cols_truncated": False,
    }]


@pytest.mark.parametrize("replacement", [False, True])
def test_csv_total_cache_refreshes_after_same_size_and_mtime_change(
    client, auth, temp_root, replacement,
):
    import os
    import time

    target = temp_root / "changed.csv"
    before = "1,alpha\n2,bravo\n"
    after = "1,a\n2,b\n3,c    \n"
    assert len(before) == len(after)
    target.write_text(before, encoding="utf-8")
    response = client.get("/api/files/csv", params={"path": target.name}, headers=auth)
    assert response.status_code == 200
    assert response.json()["has_header"] is False
    assert response.json()["total_rows"] == 2
    old = target.stat()
    # Coarse filesystem clock ticks must separate the two independent writes.
    time.sleep(0.01)
    if replacement:
        temporary = temp_root / "replacement.csv"
        temporary.write_text(after, encoding="utf-8")
        os.utime(temporary, ns=(old.st_atime_ns, old.st_mtime_ns))
        temporary.replace(target)
    else:
        target.write_text(after, encoding="utf-8")
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert target.stat().st_size == old.st_size
    assert target.stat().st_mtime_ns == old.st_mtime_ns
    assert (target.stat().st_ino, target.stat().st_ctime_ns) != (old.st_ino, old.st_ctime_ns)

    response = client.get("/api/files/csv", params={
        "path": target.name, "offset": 2, "limit": 1,
    }, headers=auth)
    assert response.status_code == 200
    assert response.json()["total_rows"] == 3
    assert response.json()["rows"] == [["3", "c    "]]
