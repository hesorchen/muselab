"""Targeted browser contracts for settings and conversation reliability."""
from __future__ import annotations



from .test_chat_render_perf import _app_eval, _login


def test_absolute_reference_captures_origin_workspace(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      const original=app.fileWorkspacePath;
      try {
        app.fileWorkspacePath=()=>'/workspace/first';
        const quote=app._selectionQuoteSnapshot({source:'preview',path:'notes/test.md',text:'fixture'});
        const mention=app._mentionPath('notes/test.md');
        app.fileWorkspacePath=()=>'/workspace/second';
        return {path:quote.path, mention, prompt:app._composerPromptText('',[quote]),
          absolute:app.absoluteFilePath('/workspace/first/notes/test.md')};
      } finally {app.fileWorkspacePath=original;}
    """)
    assert result["path"] == result["mention"] == result["absolute"] == "/workspace/first/notes/test.md"
    assert "/workspace/first/notes/test.md" in result["prompt"]
    assert "/workspace/second" not in result["prompt"]
