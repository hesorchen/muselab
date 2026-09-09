"""Uploads remain identifiable, and file dates/order survive refresh and live updates."""
import re
import pytest
from playwright.sync_api import expect
from tests.e2e.test_files_preview import _login
from tests.e2e.test_chat_render_perf import _app_eval


@pytest.mark.parametrize("width", [1440, 390])
def test_named_upload_results_preflight_and_locate(page, backend_url, auth_token, width, tmp_path):
    page.set_viewport_size({"width":width, "height":900})
    page.route("**/api/files/upload-limits", lambda route: route.fulfill(json={"max_file_bytes":1024}))
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setMobileTab('files');")
    posts = []
    page.on("request", lambda request: posts.append(request.url)
            if request.method == "POST" and request.url.endswith("/api/files/upload") else None)
    page.locator('.pane.files input[x-ref="upload"]').set_input_files([
        {"name":"recent-upload.txt", "mimeType":"text/plain", "buffer":b"hello"},
        {"name":"too-large.bin", "mimeType":"application/octet-stream", "buffer":b"x"*2048},
    ])
    good = page.locator('[data-upload-name="recent-upload.txt"]')
    bad = page.locator('[data-upload-name="too-large.bin"]')
    expect(good).to_contain_text("Saved")
    expect(bad).to_contain_text("exceeds")
    assert len(posts) == 1
    page.wait_for_timeout(1000)
    expect(bad).to_be_visible()
    good.get_by_role("button", name="Locate").click()
    row = page.locator('.filelist [data-path="recent-upload.txt"]')
    expect(row).to_be_visible()
    expect(row.locator("time")).to_have_attribute("datetime", re.compile(r"^\d{4}-"))
    page.screenshot(path=str(tmp_path / "upload-results.png"))
    page.select_option("#file-sort", "mtime_desc")
    page.wait_for_function("""() => document.querySelector('#app')._x_dataStack[0].fileSort === 'mtime_desc'""")
    page.reload()
    expect(page.locator("#file-sort")).to_have_value("mtime_desc")


def test_sort_updates_existing_file_on_mtime_event(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
        app.showHidden = false;
        app.fileSort = 'mtime_desc';
        const entries = [
          {path:'folder', name:'folder', is_dir:true, mtime:1, size:0},
          {path:'a.txt', name:'a.txt', is_dir:false, mtime:100, size:1},
          {path:'z.txt', name:'z.txt', is_dir:false, mtime:200, size:1},
        ];
        Object.assign(app, app._materializeFileSnapshot(entries, []));
        const before = app.visible.map(n => n.path);
        app._applyFileTreeDelta([{type:'modified', path:'a.txt', mtime:300, size:2}]);
        return {before, after:app.visible.map(n=>n.path), stamp:app.visible[1].mtime};
    """)
    assert result == {"before":["folder","z.txt","a.txt"],
                      "after":["folder","a.txt","z.txt"], "stamp":300}
