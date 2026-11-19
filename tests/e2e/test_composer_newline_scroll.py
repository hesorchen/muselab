"""A shortcut newline in a full composer keeps the caret in view.

Shift/Ctrl/Cmd+Enter inserts the line break programmatically. Replacing the
textarea value does not scroll it, so once the composer reached its max
height the new line landed below the fold and the view did not follow.
"""
from __future__ import annotations

import pytest

from .test_chat_render_perf import _app_eval, _login

CARET = """const ta = app.$refs.chatInput;
  const pos = ta.selectionEnd;
  const before = ta.value.slice(0, pos).split("\\n").length;
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  return {scrollable: ta.scrollHeight > ta.clientHeight + 4,
          scrollTop: ta.scrollTop, clientHeight: ta.clientHeight,
          lineHeight, line: before, pos, length: ta.value.length};"""


@pytest.mark.parametrize("where", ["end", "middle"])
@pytest.mark.parametrize("key", ["Shift+Enter", "Control+Enter", "Meta+Enter"])
def test_shortcut_newline_scrolls_composer_to_caret(
    page, backend_url, auth_token, key, where,
):
    page.set_viewport_size({"width": 1440, "height": 900})
    _login(page, backend_url, auth_token)
    lines = [f"line {i:02d} " + "word " * 6 for i in range(30)]
    _app_eval(page, """app._setChatInput(arg);
      app.$nextTick(() => app.autoGrow(app.$refs.chatInput));""", "\n".join(lines))
    composer = page.locator("textarea.chat-input, [x-ref=chatInput]").first
    composer.focus()
    if where == "end":
        _app_eval(page, """const ta = app.$refs.chatInput;
          ta.setSelectionRange(ta.value.length, ta.value.length);
          ta.scrollTop = ta.scrollHeight;""")
    else:
        # Caret at the end of line 20, scrolled so that line is the last
        # visible one: the inserted line falls just below the fold.
        _app_eval(page, """const ta = app.$refs.chatInput;
          const pos = ta.value.split("\\n").slice(0, 21).join("\\n").length;
          ta.setSelectionRange(pos, pos);
          const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
          ta.scrollTop = Math.max(0, 21 * lh - ta.clientHeight + 8);""")
    page.wait_for_timeout(100)
    for _ in range(3):
        page.keyboard.press(key)
    page.wait_for_timeout(200)
    state = _app_eval(page, CARET)
    assert state["scrollable"], state
    padding = 24
    caret_bottom = state["line"] * state["lineHeight"]
    assert caret_bottom <= state["scrollTop"] + state["clientHeight"] + padding, state
    assert caret_bottom - state["lineHeight"] >= state["scrollTop"] - padding, state
