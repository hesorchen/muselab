"""Browser selection must survive preview scrolling and span multiple screens."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright is required")
from playwright.sync_api import expect  # noqa: E402

from .test_files_preview import _login  # noqa: E402


@pytest.mark.parametrize("extension", ["md", "txt"])
def test_mouse_selection_survives_scroll_and_extends_across_screens(
    page, backend_url, auth_token, extension,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _login(page, backend_url, auth_token)
    text = "\n\n".join(
        f"Selection row {i:03d}: continuous text for reading and copying."
        for i in range(160)
    )
    page.evaluate("""async ({text, path}) => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const response = await fetch('/api/files/write', {method: 'PUT',
        headers: {...app.fileHdr(), 'Content-Type': 'application/json'},
        body: JSON.stringify({path, content: text})});
      if (!response.ok) throw new Error('fixture write failed');
      await app.openFile({path, name: path});
    }""", {"text": text, "path": f"selection-scroll.{extension}"})
    host = page.locator(".pane.preview .markdown:visible" if extension == "md"
                        else ".pane.preview pre.text")
    expect(host).to_be_visible()
    page.wait_for_timeout(400)  # Preview transition, restoration and highlighting settle.
    body = page.locator(".pane.preview .preview-body")
    # Measure an actual text glyph so mouse events exercise native selection.
    point = host.evaluate("""el => {
      const node = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode();
      const range = document.createRange();
      range.setStart(node, 0); range.setEnd(node, 1);
      const rect = range.getBoundingClientRect();
      return {x: rect.left + 1, y: rect.top + rect.height / 2};
    }""")
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    page.mouse.move(point["x"] + 180, point["y"], steps=12)
    page.mouse.up()
    expect(page.locator(".preview-selection-actions")).to_be_visible()
    selected = page.evaluate("window.getSelection().toString()")
    assert selected.startswith("Selection row 000:")
    page.evaluate("window.selectionAnchor = window.getSelection().anchorNode")

    viewport = body.bounding_box()
    page.mouse.move(viewport["x"] + viewport["width"] / 2,
                    viewport["y"] + viewport["height"] / 2)
    page.mouse.wheel(0, 900)
    page.wait_for_function("document.querySelector('.preview-body').scrollTop > 800")
    expect(page.locator(".preview-selection-actions")).to_be_hidden()
    assert page.evaluate("window.getSelection().toString()") == selected
    assert page.evaluate("window.getSelection().anchorNode === window.selectionAnchor")
    page.keyboard.press("Control+c")
    assert page.evaluate("navigator.clipboard.readText()") == selected

    # Return to the start, then keep the button held as the wheel crosses
    # several viewport heights. The initial anchor and every intervening row
    # must remain selected, including text outside the viewport.
    page.mouse.wheel(0, -2000)
    page.wait_for_function("document.querySelector('.preview-body').scrollTop === 0")
    # Click the margin to clear the old selection before a new drag starts.
    page.mouse.click(viewport["x"] + 3, viewport["y"] + 10)
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    page.mouse.move(point["x"] + 180, point["y"], steps=12)
    expect(page.locator(".preview-selection-actions")).to_be_visible()
    page.evaluate("window.selectionAnchor = window.getSelection().anchorNode")
    for _ in range(3):
        previous_top = body.evaluate("el => el.scrollTop")
        page.mouse.wheel(0, 700)
        page.wait_for_function("top => document.querySelector('.preview-body').scrollTop > top + 600",
                               arg=previous_top)
        page.mouse.move(point["x"] + 200, viewport["y"] + viewport["height"] - 45,
                        steps=8)
        assert page.evaluate("window.getSelection().anchorNode === window.selectionAnchor")
    page.mouse.up()
    selection = page.evaluate("window.getSelection().toString()")
    assert selection.startswith("Selection row 000:")
    for i in range(1, 25):
        assert f"Selection row {i:03d}:" in selection
    assert body.evaluate("el => el.scrollTop") > 2 * viewport["height"]
    page.keyboard.press("Control+c")
    assert page.evaluate("navigator.clipboard.readText()") == selection
