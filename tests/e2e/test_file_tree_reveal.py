"""A file locate succeeds only when its actual virtual row is visible."""
import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _capture_browser_errors, _assert_no_browser_errors, _login


TARGET = "locate-folder/deep/target.txt"


def _fixture(page, backend_url, auth_token, *, include_target=True):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    page.wait_for_function("() => !document.querySelector('#app')._x_dataStack[0].treeLoading")
    def listing(route):
        path = parse_qs(urlparse(route.request.url).query).get("path", [""])[0]
        if path == "locate-folder":
            entries = [{"path": path + "/deep", "name": "deep", "is_dir": True}]
        elif path == "locate-folder/deep":
            entries = [{"path": path + f"/file-{i:03}.txt", "name": f"file-{i:03}.txt", "is_dir": False}
                       for i in range(240 if include_target else 500)]
            if include_target:
                entries.append({"path": TARGET, "name": "target.txt", "is_dir": False})
        else:
            return route.fallback()
        route.fulfill(content_type="application/json", body=json.dumps({"entries": entries}))
    def metadata(route):
        path = parse_qs(urlparse(route.request.url).query).get("path", [""])[0]
        if path != TARGET:
            return route.fallback()
        route.fulfill(content_type="application/json", body=json.dumps({
            "path": TARGET, "name": "target.txt", "is_dir": False, "size": 14, "mtime": 0,
        }))
    page.route("**/api/files/stat?**", metadata)
    page.route("**/api/files/list?**", listing)
    page.route("**/api/files/read?path=locate-folder*", lambda r: r.fulfill(body="Locate fixture"))
    _app_eval(page, """
        app._stopFileEvents(false);
        // This virtual-tree fixture owns its rows; keep real backend events
        // from replacing them with the separate on-disk fixture workspace.
        app._startFileEvents = () => {};
        app._treeLoadSeq += 1;
        app.leftOpen = true;
        app.desktopFullPane = '';
        app.searchMode = false;
        app.expanded = new Set();
        app.childCache = {};
        await app.openFile({path: arg, name: 'target.txt'}, {reveal:true});
        app.visible = Array.from({length:240}, (_,i) => ({
          path: 'root-' + String(i).padStart(3,'0') + '.txt',
          name: 'root-' + i + '.txt', is_dir:false, depth:0,
        }));
        app.visible.splice(160,0,{path:'locate-folder',name:'locate-folder',is_dir:true,depth:0});
        app.fileTreeViewport = {start:0,end:80};
        app.$refs.fileList.scrollTop = 0;
        await app.$nextTick();
    """, TARGET)
    return errors


def _expect_located(page, target=TARGET):
    row = page.locator(f'.filelist [role="treeitem"][data-path="{target}"]')
    expect(row).to_be_visible()
    expect(row).to_have_attribute("aria-selected", "true")
    page.wait_for_function("""path => {
        const list=document.querySelector('.filelist');
        const row=Array.from(list.querySelectorAll('[role=treeitem]')).find(r=>r.dataset.path===path);
        if (!row) return false;
        const a=list.getBoundingClientRect(), b=row.getBoundingClientRect();
        return a.height>0 && b.top>=a.top-1 && b.bottom<=a.bottom+1;
    }""", arg=target)
    assert page.locator('.filelist [role="treeitem"]').count() < 100


@pytest.mark.parametrize("entry", ["tab", "open-files", "context"])
def test_file_tab_and_context_menu_reveal_collapsed_virtual_row(page, backend_url, auth_token, entry):
    page.set_viewport_size({"width":1440,"height":900})
    errors = _fixture(page, backend_url, auth_token)
    if entry == "context":
        # Explicit locate must restore a hidden tree, even in preview focus mode.
        _app_eval(page, "app.leftOpen=false; app.desktopFullPane='preview'; app.searchMode=true;")
        tab = page.locator(f'.pane.preview .tab[data-path="{TARGET}"]')
        tab.click(button="right")
        page.locator('.tab-ctx-menu button').filter(has_text="Reveal in tree").or_(
            page.locator('.tab-ctx-menu button').filter(has_text="在文件树定位")).click()
    elif entry == "open-files":
        page.locator(f'.open-files-list [data-path="{TARGET}"] .open-files-main').click()
    else:
        page.locator(f'.pane.preview .tab[data-path="{TARGET}"] .tab-main').click()
    _expect_located(page)
    _assert_no_browser_errors(page, errors)


def test_mobile_tab_defers_scroll_until_files_pane_is_visible(page, backend_url, auth_token):
    page.set_viewport_size({"width":390,"height":844})
    errors = _fixture(page, backend_url, auth_token)
    page.locator(f'.pane.preview .tab[data-path="{TARGET}"] .tab-main').click()
    assert _app_eval(page, "return app.mobileTab;") == "preview"
    _app_eval(page, "app.setMobileTab('files');")
    _expect_located(page)
    _assert_no_browser_errors(page, errors)


def test_explicit_reveal_unhides_parent_and_selects_without_switching_preview(page, backend_url, auth_token):
    page.set_viewport_size({"width":1440,"height":900})
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    headers = {"X-Auth-Token": auth_token}
    parent = ".locate-hidden"
    target = parent + "/target.txt"
    response = page.request.post(backend_url + "/api/files/mkdir", headers=headers, data={"path":parent})
    assert response.ok or response.status == 409
    response = page.request.put(backend_url + "/api/files/write", headers=headers,
                               data={"path":target,"content":"Hidden locate fixture"})
    assert response.ok
    _app_eval(page, """
        if (app.showHidden) await app.toggleHidden();
        await app.openFile({path:arg,name:'target.txt'}, {reveal:true});
        await app.openFile({path:'notes.md',name:'notes.md'}, {reveal:true});
        await app.reloadTree();
        app.leftOpen = false;
    """, target)
    tab = page.locator(f'.pane.preview .tab[data-path="{target}"]')
    tab.click(button="right")
    page.locator('.tab-ctx-menu button').filter(has_text="Reveal in tree").or_(
        page.locator('.tab-ctx-menu button').filter(has_text="在文件树定位")).click()
    _expect_located(page, target)
    assert _app_eval(page, "return app.selected;") == "notes.md"
    assert _app_eval(page, "return app.showHidden;") is True
    _assert_no_browser_errors(page, errors)


def test_late_directory_response_cannot_override_newer_reveal(page, backend_url, auth_token):
    page.set_viewport_size({"width":1440,"height":900})
    errors = _fixture(page, backend_url, auth_token)
    result = _app_eval(page, """
        const original = app.fetchChildren;
        let release;
        const response = new Promise(resolve => release = resolve);
        app.fetchChildren = async (path, options) => path === 'locate-folder'
          ? response : original.call(app, path, options);
        const older = app.revealInTree(arg);
        await app.$nextTick();
        await app.revealInTree('root-210.txt');
        release([{path:'locate-folder/deep',name:'deep',is_dir:true}]);
        await older;
        app.fetchChildren = original;
        return {
          selected:app.treeFocusPath,
          olderExpanded:app.expanded.has('locate-folder'),
        };
    """, TARGET)
    assert result == {"selected":"root-210.txt","olderExpanded":False}
    _expect_located(page, "root-210.txt")
    _assert_no_browser_errors(page, errors)


def test_reveal_finds_file_beyond_bounded_directory_listing(page, backend_url, auth_token):
    page.set_viewport_size({"width":1440,"height":900})
    errors = _fixture(page, backend_url, auth_token, include_target=False)
    page.locator(f'.pane.preview .tab[data-path="{TARGET}"] .tab-main').click()
    _expect_located(page)
    _assert_no_browser_errors(page, errors)
