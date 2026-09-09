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
    page.locator("#file-sort").click()
    page.wait_for_function("""() => document.querySelector('#app')._x_dataStack[0].fileSort === 'mtime_desc'""")
    page.reload()
    expect(page.locator("#file-sort")).to_have_attribute("data-sort", "mtime_desc")


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


@pytest.mark.parametrize("width", [1440, 390])
def test_cancel_inflight_upload_keeps_other_file(page, backend_url, auth_token, width, tmp_path_factory):
    page.set_viewport_size({"width": width, "height": 900})
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setMobileTab('files');")
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 20,
        "downloadThroughput": 1024 * 1024, "uploadThroughput": 64 * 1024,
    })
    failed = []
    page.on("requestfailed", lambda req: failed.append(req.failure)
            if req.method == "POST" and req.url.endswith("/api/files/upload") else None)
    cancelled_name = f"cancel-mid-transfer-{width}.bin"
    kept_name = f"keep-transfer-{width}.txt"
    try:
        page.locator('.pane.files input[x-ref="upload"]').set_input_files([
            {"name": cancelled_name, "mimeType": "application/octet-stream", "buffer": b"x" * (2 * 1024 * 1024)},
            {"name": kept_name, "mimeType": "text/plain", "buffer": b"keep this"},
        ])
        page.wait_for_function("""name => {
            const app = document.querySelector('#app')._x_dataStack[0];
            const item = app.fileUploadProgress.items.find(x => x.name === name);
            return item && item.loaded > 0 && !item.done && app.canCancelFileUpload(item);
        }""", arg=cancelled_name)
        row = page.locator(f'[data-upload-name="{cancelled_name}"]')
        row.get_by_role("button", name="Cancel", exact=True).click()
        expect(row).to_contain_text("Transfer cancelled")
        expect(row.get_by_role("button", name="Locate")).to_be_hidden()
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0,
            "downloadThroughput": -1, "uploadThroughput": -1,
        })
        expect(page.locator(f'[data-upload-name="{kept_name}"]')).to_contain_text("Saved")
        page.wait_for_timeout(300)
        assert failed and any("ERR_ABORTED" in reason for reason in failed)
        root = next(tmp_path_factory.getbasetemp().glob("e2e-root[0-9]*"))
        assert not (root / cancelled_name).exists()
        assert not list(root.glob(f".~{cancelled_name}.*.uploading"))
        assert (root / kept_name).read_text() == "keep this"
        assert _app_eval(page, "return app.fileUploadProgress.failedFiles;") == 0
    finally:
        cdp.detach()


def test_cancel_preflight_never_starts_post_and_saving_cannot_cancel(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.setMobileTab('files');")
    posts = []
    page.on("request", lambda req: posts.append(req.url)
            if req.method == "POST" and req.url.endswith("/api/files/upload") else None)
    _app_eval(page, """
        app._workspaceUploadLimit = () => new Promise(resolve => { window.releaseUploadLimit = resolve; });
        window.preflightResult = app._uploadFileQuiet("", new File(["stop"], "cancel-preflight.txt"));
    """)
    row = page.locator('[data-upload-name="cancel-preflight.txt"]')
    row.get_by_role("button", name="Cancel", exact=True).click()
    expect(row).to_contain_text("Transfer cancelled")
    _app_eval(page, "window.releaseUploadLimit(1024);")
    page.wait_for_timeout(300)
    assert posts == []
    assert page.evaluate("window.preflightResult") is None
    result = _app_eval(page, """
        const token = app._beginFileUploadTransfer({name:'saving.txt',size:4});
        const current = app._fileUploadTransfer(token);
        current.transfer.saving = true;
        app.cancelFileUpload({id:token.transferId});
        return {cancelled:current.transfer.cancelled, done:current.transfer.done};
    """)
    assert result == {"cancelled": False, "done": False}


@pytest.mark.parametrize("width", [320, 390, 430])
def test_touch_file_metadata_hides_without_overlapping_names(browser, backend_url, auth_token, width):
    context = browser.new_context(viewport={"width": width, "height": 900},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
    page = context.new_page()
    try:
        _login(page, backend_url, auth_token)
        _app_eval(page, """
            app.setMobileTab('files');
            app._stopFileEvents();
            app.visible = [
              {path:'report.md', name:'long-report-name.md', is_dir:false, depth:0, size:1024, mtime:1700000000},
              {path:'nested/report.md', name:'nested-long-report.md', is_dir:false, depth:6, size:1024, mtime:1700000000},
              {path:'folder', name:'long-folder-name', is_dir:true, depth:0, mtime:1700000000},
            ];
            app.fileTreeViewport = {start:0,end:80};
        """)
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        rows = page.locator('.filelist li[role="treeitem"]')
        expect(rows).to_have_count(3)
        for row in rows.all():
            expect(row.locator(".file-modified")).to_be_hidden()
            metrics = row.evaluate("""el => {
                const name = el.querySelector('.name').getBoundingClientRect();
                const trailing = el.querySelector('.tree-trailing').getBoundingClientRect();
                return {overlap: name.right > trailing.left + 1,
                        overflow: el.scrollWidth > el.clientWidth + 1};
            }""")
            assert metrics == {"overlap": False, "overflow": False}
    finally:
        context.close()
