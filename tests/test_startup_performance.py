"""Source contracts for keeping cold-start work off the critical path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def _method(name: str, next_name: str) -> str:
    start = APP.index(f"    {name}(")
    end = APP.index(f"\n    {next_name}(", start)
    return APP[start:end]


def test_boot_dispatches_context_before_noncritical_startup_work():
    boot = _method("async _bootApp", "_startLiveConnections")

    context = "const contextReady = Promise.resolve(this.fetchContextInfo());"
    assert context in boot
    assert boot.index(context) < boot.index("this.loadRoot()")
    assert boot.index(context) < boot.index("this._startLiveConnections(")
    assert "this.fetchStats();" not in boot
    assert "this.loadTrash();" not in boot
    assert "this._checkInterruptedTurns();" not in boot
    assert "await contextReady;" in boot


def test_ready_defers_decorative_snapshots_off_the_critical_path():
    deferred = _method("_scheduleDeferredBootWork", "_markReady")
    ready = _method("_markReady", "_agoLabel")

    for call in (
        "this.fetchActivity()",
        "this.fetchStats()",
        "this.loadTrash()",
        "this._checkInterruptedTurns()",
    ):
        assert call in deferred
    # No vendor bundle is injected from the boot path any more: KaTeX and
    # hljs load from the render that needs them.
    assert "_prewarmPreviewLibs" not in deferred
    assert "this._loadKatex()" not in deferred
    assert 'window.requestIdleCallback(run, { timeout: 1500 })' in deferred
    assert "requestAnimationFrame(() => requestAnimationFrame(schedule))" in deferred
    assert "this._scheduleDeferredBootWork();" in ready


def test_dynamic_modules_and_lazy_assets_share_versioned_urls():
    assert (
        '<link rel="modulepreload" '
        'href="/static/modules/persistent-cache.mjs" />'
    ) in INDEX
    assert "_staticAssetUrl(path)" in APP
    assert (
        'this._staticAssetUrl("/static/modules/persistent-cache.mjs")'
        in APP
    )
    assert (
        'this._staticAssetUrl("/static/modules/file-capabilities.mjs")'
        in APP
    )
    assert "const src = this._staticAssetUrl(path);" in APP
    assert 'this._staticAssetUrl("/static/vendor/mermaid.min.js")' in APP
