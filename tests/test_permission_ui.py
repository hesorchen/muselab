"""Static contract tests for per-session Claude SDK permission controls."""
from pathlib import Path
import re
from typing import get_args

from claude_agent_sdk.types import PermissionMode


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_session_permission_is_separate_from_new_session_default():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'defaultPermission: "bypassPermissions"' in app
    assert 'permission: this.defaultPermission || "bypassPermissions"' in app
    assert 'this.permission = s.permission || "default"' not in app
    assert 'this.defaultPermission = newDefaultPerm' in app
    assert 'this.permission = newDefaultPerm' not in app
    assert 'this.model = newDefaultModel' not in app
    assert '["_permissionExpected", "_permissionPatchTail", "permission"' in app


def test_permission_selector_matches_installed_sdk_modes():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    start = html.index("<!-- Permission mode -->")
    end = html.index("<!-- Reasoning effort.", start)
    toolbar = html[start:end]
    rendered = set(re.findall(r'<option value="([^"]+)">', toolbar))

    assert rendered == set(get_args(PermissionMode))
