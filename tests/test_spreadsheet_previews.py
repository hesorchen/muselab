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
