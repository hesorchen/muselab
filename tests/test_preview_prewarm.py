"""Cheap source contract for deferred preview-library prewarming."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preview_prewarm_waits_for_each_job_before_scheduling_next_idle():
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    start = app.index("    _prewarmPreviewLibs() {")
    end = app.index("\n    async _loadHljs()", start)
    prewarm = app[start:end]

    settled = "Promise.resolve().then(job).catch(() => {}).finally(() => {"
    schedule_next = "if (queue.length) idle(drain);"
    assert settled in prewarm
    assert prewarm.index(settled) < prewarm.index(schedule_next)
    assert "Promise.resolve().then(job).catch(() => {});" not in prewarm
