"""Cheap source contracts for on-demand preview libraries.

KaTeX and highlight.js used to be prewarmed on every idle boot. They now load
from the render paths that actually need them, so a prose-only session pays
neither download.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
WORKER = (ROOT / "frontend" / "render-worker.js").read_text(encoding="utf-8")


def test_boot_does_not_prewarm_preview_libraries():
    assert "_prewarmPreviewLibs" not in APP
    assert "_loadHljs" not in APP
    assert "_previewLibsPrewarmed" not in APP
    assert "window.hljs" not in APP


def test_katex_loads_from_the_math_render_path_and_retimesets_painted_bubbles():
    start = APP.index("    _scheduleMathRerender() {")
    end = APP.index("\n    _rerenderMathMessages()", start)
    schedule = APP[start:end]
    assert "this._loadKatex()" in schedule
    assert "this._rerenderMathMessages()" in schedule
    # The trigger lives in mdRender (every final chat bubble / preview render).
    assert "this._scheduleMathRerender();" in APP
    # Re-rendering a bubble replaces its DOM, discarding hljs classes.
    assert "this._scheduleMathHighlight();" in APP
    # Every open tab is swept: background preload can paint math before the
    # library lands and would otherwise keep raw delimiters on tab switch.
    rerender_start = APP.index("    _rerenderMathMessages() {")
    rerender_end = APP.index("\n    _linkifyFilePaths", rerender_start)
    assert "this.tabState" in APP[rerender_start:rerender_end]


def test_render_worker_loads_hljs_and_language_packs_on_demand():
    assert 'importScripts("vendor/highlight.min.js"' in WORKER
    assert "const HLJS_LANG_PACKS" in WORKER
    assert 'importScripts("vendor/hljs-langs/"' in WORKER
    assert "ensureHljsLanguage(data.language)" in WORKER
    # importScripts paths are allowlisted, never built from raw markdown text.
    assert "HLJS_LANG_PACKS[String(language).toLowerCase()]" in WORKER
