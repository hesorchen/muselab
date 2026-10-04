"""Full supported preview grids keep plain text, navigation and scroll behavior."""
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

# Reuse the static-only frontend server: it imports no backend/config/SDK and
# has no application API routes. This module routes every API to synthetic data.
from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


_VALUES = [None, "", 0, False, True, 1.25, '<em>literal & "quoted"</em>']


def _matrix(start, count):
    rows = [[f"r{r + 1:04d}-c{c + 1:02d}" for c in range(50)]
            for r in range(start, start + count)]
    if start == 0:
        rows[0][:len(_VALUES)] = _VALUES
    return rows


def _assert_plain_values(cells):
    for index, value in enumerate(_VALUES):
        cell = cells.nth(index)
        if value is None:
            text = ""
        elif isinstance(value, bool):
            text = str(value).lower()
        else:
            text = str(value)
        expect(cell).to_have_text(text)
        title = None if value is None or value is False else text
        assert cell.get_attribute("title") == title
    expect(cells.locator("em")).to_have_count(0)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_full_supported_spreadsheet_grid_preserves_values_and_navigation(
    page, settings_frontend_url, kind,
):
    name = f"supported-wide.{kind}"
    requests = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    header = [*_VALUES, *[f"column-{c + 1:02d}" for c in range(len(_VALUES), 50)]]

    def handle(route):
        parsed = urlsplit(route.request.url)
        params = parse_qs(parsed.query)
        if parsed.path == "/api/files/csv":
            offset = int(params.get("offset", ["0"])[0])
            limit = int(params["limit"][0])
            assert limit == 200  # Real UI page size; no test-only oversized page.
            requests.append(offset)
            route.fulfill(json={
                "path": name, "header": header, "rows": _matrix(offset, min(limit, 400 - offset)),
                "offset": offset, "limit": limit, "total_rows": 400, "has_header": True,
                "delimiter": ",", "cols_truncated": False,
            })
        elif parsed.path == "/api/files/xlsx":
            requests.append(name)
            route.fulfill(json={
                "path": name, "sheets": [
                    {"name": "Main", "rows": _matrix(0, 500),
                     "rows_truncated": False, "cols_truncated": False},
                    {"name": "Second", "rows": _matrix(500, 20),
                     "rows_truncated": False, "cols_truncated": False},
                ], "sheets_truncated": False,
                "limits": {"max_rows": 500, "max_cols": 50, "max_sheets": 20},
            })
        elif parsed.path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.token='spreadsheet-synthetic-token-32plus';
      app.authed=true; app.appReady=true;
      app.availableModels=[{model:'fixture',label:'Fixture',group:'Fixture'}];
    }""")
    page.evaluate("name => document.querySelector('#app')._x_dataStack[0].openFile({path:name,name})", name)
    grid = page.locator(".csv-preview .xlsx-scroll" if kind == "csv" else ".xlsx-sheet-wrap .xlsx-scroll")
    count = 200 if kind == "csv" else 500
    expect(grid.locator("tbody tr")).to_have_count(count)
    expect(grid.locator("tbody td")).to_have_count(count * 51)
    expect(grid.locator("tbody tr").first.locator("td").first).to_have_text("1")
    expect(grid.locator("tbody tr").last.locator("td").first).to_have_text(str(count))
    _assert_plain_values(grid.locator("tbody tr").first.locator("td:not(.xlsx-rownum)"))
    if kind == "csv":
        expect(grid.locator("thead th")).to_have_count(51)
        _assert_plain_values(grid.locator("thead th:not(.xlsx-rownum)"))

    grid.hover()
    page.mouse.wheel(850, 0)
    page.wait_for_function("kind => document.querySelector(kind === 'csv' ? '.csv-preview .xlsx-scroll' : '.xlsx-sheet-wrap .xlsx-scroll').scrollLeft > 0", arg=kind)
    if kind == "csv":
        toolbar = page.locator(".csv-toolbar")
        toolbar.get_by_role("button", name="Next →").click()
        expect(toolbar.locator(".csv-range")).to_have_text("201–400 / 400")
        expect(grid.locator("tbody tr").first.locator("td").first).to_have_text("201")
        expect(grid.locator("tbody tr").first.locator("td").nth(1)).to_have_text("r0201-c01")
        expect(toolbar.get_by_role("button", name="Next →")).to_be_disabled()
        assert grid.evaluate("el => el.scrollLeft") > 0
        toolbar.get_by_role("button", name="← Prev").click()
        expect(toolbar.locator(".csv-range")).to_have_text("1–200 / 400")
        _assert_plain_values(grid.locator("tbody tr").first.locator("td:not(.xlsx-rownum)"))
        assert requests == [0, 200, 0]
    else:
        tabs = page.locator(".xlsx-tabs")
        tabs.get_by_role("button", name="Second", exact=True).click()
        expect(grid.locator("tbody tr")).to_have_count(20)
        expect(grid.locator("tbody tr").first.locator("td").nth(1)).to_have_text("r0501-c01")
        tabs.get_by_role("button", name="Main", exact=True).click()
        expect(grid.locator("tbody tr")).to_have_count(500)
        _assert_plain_values(grid.locator("tbody tr").first.locator("td:not(.xlsx-rownum)"))
        assert requests == [name]
    page.locator(".pane.preview .tab.active .tab-close").click()
    page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].selected")
    expect(grid.locator("tbody tr")).to_have_count(0)
    assert errors == []
