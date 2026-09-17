"""Click real chat links and preview files outside the server workspace."""
import re
import struct
import zlib
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from .test_tool_path_links import _login


APP = "document.querySelector('#app')._x_dataStack[0]"


def _show_link(page, path, kind="tool"):
    page.evaluate("""({path, kind}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.conciseChat = false;
      const sid = app.currentId, st = app._ensureTabState(sid);
      st._loaded = true;
      st.messages = kind === 'tool'
        ? [{role:'tool_use', name:'Read', summary:path, input:{file_path:path}, _k:'external-read'}]
        : [{role:'assistant', text:'[Open report](' + encodeURI(path) + ')', _k:'external-link'}];
      st.messageRange.visibleStart = 0;
      st.messageRange.visibleEnd = st.messages.length;
      st.messageRange.total = st.messages.length;
      app._activateTabState(sid);
      st.messagesReady = true;
      app.mobileTab = 'chat';
    }""", {"path": str(path), "kind": kind})
    link = page.locator('.chat-body .file-link').first
    expect(link).to_be_visible()
    return link


def _create_file(directory: Path, kind: str) -> Path:
    path = directory / ("外部 report." + kind)
    if kind == "png":
        def chunk(name, data):
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
        path.write_bytes(b"\x89PNG\r\n\x1a\n"
                         + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
                         + chunk(b"IEND", b""))
    elif kind == "xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["external", 7])
        workbook.save(path)
        workbook.close()
    elif kind == "pdf":
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"]
        data = b"%PDF-1.4\n"
        offsets = []
        for index, content in enumerate(objects, 1):
            offsets.append(len(data))
            data += f"{index} 0 obj\n".encode() + content + b"\nendobj\n"
        xref = len(data)
        data += b"xref\n0 4\n0000000000 65535 f \n"
        for offset in offsets:
            data += f"{offset:010d} 00000 n \n".encode()
        data += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        path.write_bytes(data)
    else:
        content = {"md": "# External fixture\npreview content\n", "txt": "external text fixture", "csv": "name,value\nexternal,7\n", "html": "<!doctype html><html><body><h1>External HTML fixture</h1></body></html>"}[kind]
        path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("kind", ["md", "txt", "png", "pdf", "html", "csv", "xlsx"])
def test_external_chat_link_opens_real_readonly_preview(page, backend_url, auth_token, tmp_path, kind):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    target = _create_file(tmp_path, kind)
    _login(page, backend_url, auth_token)
    link = _show_link(page, target)
    original_url = page.url
    link.click()
    mode = "img" if kind == "png" else "text" if kind == "txt" else kind
    page.wait_for_function("""({path, mode}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      return app.selected === path && app.previewMode === mode && !app.previewLoading;
    }""", arg={"path": str(target.resolve()), "mode": mode})
    assert page.url == original_url
    assert page.evaluate(f"{APP}.isEditable({APP}.selected)") is False
    expect(page.locator('button[x-show*="isEditable(selected)"]')).not_to_be_visible()
    page.evaluate(f"{APP}.toggleEdit()")
    assert page.evaluate(f"{APP}.editing") is False
    expect(page.locator('.pane-fileinfo-meta').filter(has_text=re.compile("Read-only|只读"))).to_be_visible()
    if kind in {"md", "txt"}:
        assert "fixture" in page.evaluate(f"{APP}.rawText")
    elif kind in {"png", "pdf", "html"}:
        page.wait_for_function(f"{APP}.rawUrl({APP}.selected) !== 'about:blank'")
        result = page.evaluate(f"""async () => {{
          const app = {APP};
          const response = await fetch(app.rawUrl(app.selected));
          return {{status: response.status, type: response.headers.get('content-type')}};
        }}""")
        assert result["status"] == 200
        assert {"png": "image/png", "pdf": "application/pdf", "html": "text/html"}[kind] in result["type"]
        if kind == "png":
            expect(page.locator('.preview-img')).to_be_visible()
            assert page.locator('.preview-img').evaluate("img => img.complete && img.naturalWidth > 0")
        if kind == "html":
            page.evaluate(f"{APP}.toggleHtmlAnnotation()")
            assert page.evaluate(f"{APP}.htmlAnnotation.active") is False
            expect(page.frame_locator('iframe[data-preview-html-path]').locator('h1')).to_have_text("External HTML fixture")
    assert not errors


@pytest.mark.parametrize("width", [1440, 390])
def test_external_markdown_link_and_download(page, backend_url, auth_token, tmp_path, width):
    page.set_viewport_size({"width": width, "height": 900})
    target = _create_file(tmp_path, "md")
    _login(page, backend_url, auth_token)
    _show_link(page, target, "markdown").click()
    page.wait_for_function(f"{APP}.previewMode === 'md' && {APP}.rawText.includes('External fixture')")
    with page.expect_download() as pending:
        page.evaluate(f"{APP}.downloadFile({APP}.selected)")
    download = pending.value
    assert download.failure() is None
    assert Path(download.path()).read_bytes() == target.read_bytes()
    assert download.suggested_filename == target.name


def test_external_denial_does_not_open_same_named_workspace_file(page, backend_url, auth_token, tmp_path):
    _login(page, backend_url, auth_token)
    blocked = tmp_path / ".env"
    blocked.write_text("protected fixture", encoding="utf-8")
    previous = page.evaluate(f"{APP}.selected")
    _show_link(page, blocked).click()
    expect(page.locator('.toast').filter(has_text=re.compile("protected|读取权限"))).to_be_visible()
    assert page.evaluate(f"{APP}.selected") == previous


def test_workspace_preview_remains_editable_after_external_file(page, backend_url, auth_token, tmp_path):
    _login(page, backend_url, auth_token)
    _show_link(page, _create_file(tmp_path, "txt")).click()
    page.wait_for_function(f"{APP}.previewMode === 'text'")
    page.evaluate(f"{APP}.openByPathToasted('notes.md')")
    page.wait_for_function(f"{APP}.selected === 'notes.md' && {APP}.rawText.includes('scratch')")
    assert page.evaluate(f"{APP}.isEditable('notes.md')") is True
    page.evaluate(f"{APP}.toggleEdit()")
    assert page.evaluate(f"{APP}.editing") is True


def test_external_markdown_relative_image_loads(page, backend_url, auth_token, tmp_path):
    from urllib.parse import quote
    picture = _create_file(tmp_path, "png")
    report = tmp_path / "report.md"
    report.write_text(f"# Report\n![external picture]({quote(picture.name)})\n", encoding="utf-8")
    _login(page, backend_url, auth_token)
    _show_link(page, report).click()
    picture_node = page.locator('.markdown img[alt="external picture"]')
    expect(picture_node).to_be_visible()
    page.wait_for_function("""() => {
      const img = document.querySelector('.markdown img[alt="external picture"]');
      return img && img.complete && img.naturalWidth > 0;
    }""")
    assert "external=1" in picture_node.get_attribute("src")


def test_missing_external_path_never_falls_back_to_workspace_basename(page, backend_url, auth_token, tmp_path):
    _login(page, backend_url, auth_token)
    page.evaluate(f"{APP}.openByPathToasted('notes.md')")
    page.wait_for_function(f"{APP}.selected === 'notes.md'")
    _show_link(page, tmp_path / "notes.md").click()
    expect(page.locator('.toast').filter(has_text=re.compile("not found|不存在|无法打开"))).to_be_visible()
    assert page.evaluate(f"{APP}.selected") == "notes.md"
    assert page.evaluate(f"{APP}.tabs.filter(t => t.path.endsWith('notes.md')).length") == 1


def test_slow_external_lookup_cannot_replace_newer_tree_selection(page, backend_url, auth_token, tmp_path):
    from urllib.parse import parse_qs, urlsplit
    _login(page, backend_url, auth_token)
    target = _create_file(tmp_path, "txt")
    held = []

    def hold_external_stat(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("path") == [str(target)]:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/files/stat?**", hold_external_stat)
    page.evaluate(f"""() => {{
      const app = {APP}, open = app.openByPathToasted.bind(app);
      app.openByPathToasted = async path => {{
        try {{ return await open(path); }}
        finally {{ window.__externalOpenSettled = true; }}
      }};
    }}""")
    _show_link(page, target).click()
    expect(page.locator('.filelist li[data-path="notes.md"]')).to_be_visible()
    page.locator('.filelist li[data-path="notes.md"]').click()
    page.wait_for_function(f"{APP}.selected === 'notes.md' && {APP}.rawText.includes('scratch')")
    assert len(held) == 1
    with page.expect_response(lambda response: parse_qs(urlsplit(response.url).query).get("path") == [str(target)]):
        held[0].continue_()
    page.wait_for_function("window.__externalOpenSettled === true")
    assert page.evaluate(f"{APP}.selected") == "notes.md"
    assert page.evaluate("path => document.querySelector('#app')._x_dataStack[0].tabs.some(t => t.path === path)", str(target)) is False
