"""Browser contracts for non-blocking outbox review records."""
from __future__ import annotations


import pytest

from .test_chat_render_perf import _login


@pytest.mark.parametrize("width", [1440, 1920])
def test_file_header_actions_align_to_right_of_wide_header(page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 1000})
    _login(page, backend_url, auth_token)
    geometry = page.locator(".files-head-workspace").evaluate("""workspace => {
      const header=workspace.parentElement;
      header.style.width='560px';
      const action=header.querySelector('.files-theme-toggle');
      const h=header.getBoundingClientRect(), a=action.getBoundingClientRect();
      return {gap:h.right-a.right, topGap:Math.abs(h.top+h.height/2-a.top-a.height/2)};
    }""")
    assert 8 <= geometry["gap"] <= 18
    assert geometry["topGap"] <= 1
