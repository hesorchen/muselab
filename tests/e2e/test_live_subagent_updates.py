"""Exercise the real mux transport while the browser stays on one transcript."""
import socket
import time

import pytest

from .test_chat_render_perf import _app_eval, _capture_browser_errors, _assert_no_browser_errors, _login


@pytest.fixture
def live_mux_server(tmp_path, page):
    import os
    import subprocess
    import sys
    import urllib.request
    from pathlib import Path

    root = tmp_path / "root"
    root.mkdir()
    (root / "README.md").write_text("# Fixture\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "MUSELAB_ROOT": str(root),
        "MUSELAB_SESSIONS_DIR": str(root / "sessions"),
        "MUSELAB_ENV_PATH": str(root / "runtime.env"),
        "XDG_STATE_HOME": str(root / "state"),
        "MUSELAB_TOKEN": "test-token-1234567890abcdef-secure-min-32",
        "MUSELAB_PORT": str(port), "MUSELAB_MODEL": "deepseek-v4-pro",
        "MUSELAB_DEFAULT_MODEL": "deepseek-v4-pro"}
    for key in tuple(env):
        if key.endswith(("_API_KEY", "_AUTH_TOKEN")):
            env[key] = ""
    env["DEEPSEEK_API_KEY"] = "e2e-placeholder-not-a-real-secret"
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.e2e._live_mux_fixture"],
            cwd=Path(__file__).resolve().parents[2], env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                assert proc.poll() is None, log_path.read_text()
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=0.5).close()
                    break
                except Exception:
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)
            yield base
        finally:
            page.close()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.parametrize("width,count", [(1440, 1), (390, 1), (1440, 125)])
def test_real_mux_subagent_burst_keeps_current_transcript_live(
    page, live_mux_server, auth_token, width, count,
):
    page.set_viewport_size({"width": width, "height": 900})
    base = live_mux_server
    errors = _capture_browser_errors(page)
    _login(page, base, auth_token)
    page.wait_for_function("() => document.querySelector('#app')._x_dataStack[0]._chatMuxConnected")
    sid = _app_eval(page, "await app._ensureSessionRegistered(app.currentId); return app.currentId;")

    headers = {"X-Auth-Token": auth_token}
    response = page.request.post(base + "/fixture/begin", headers=headers, data={"sid": sid})
    assert response.ok
    page.wait_for_function("sid => document.querySelector('#app')._x_dataStack[0].tabState[sid]?.es", arg=sid)
    response = page.request.post(base + "/fixture/burst", headers=headers, data={"sid": sid, "count": count})
    assert response.ok
    from playwright.sync_api import expect
    expect(page.locator(".msg-pane:visible")).to_contain_text("LIVE_PARENT_AFTER_AGENTS", timeout=4000)
    assert _app_eval(page, "return app.currentId;") == sid
    state = _app_eval(page, """
        const st=app.tabState[arg];
        return {count:st.messages.length, visible:st.messageRange.visibleEnd,
          tail:st.atBottom, children:st.subagentThreads.length};
    """, sid)
    assert state["children"] == count
    assert state["visible"] == state["count"] and state["tail"] is True
    response = page.request.post(base + "/fixture/continuation", headers=headers, data={"sid": sid})
    assert response.ok
    try:
        for i in range(3):
            expect(page.locator(".msg-pane:visible")).to_contain_text(
                f"LIVE_CONTINUATION_{i}", timeout=4000,
            )
        assert _app_eval(page, "return app.currentId;") == sid
    finally:
        page.request.post(base + "/fixture/release-history", headers=headers)
    # The real indexed JSONL snapshot must agree with live delivery after the
    # completion reconcile, without activating another tab or reloading.
    history = page.request.get(
        base + f"/api/chat/sessions/{sid}?tail=20", headers=headers,
    )
    assert history.ok
    assert [m["text"] for m in history.json()["messages"]
            if m.get("text", "").startswith("LIVE_CONTINUATION_")] == [
                f"LIVE_CONTINUATION_{i}" for i in range(3)]
    page.wait_for_timeout(1000)
    for i in range(3):
        expect(page.locator(".msg-pane:visible")).to_contain_text(f"LIVE_CONTINUATION_{i}")
    assert _app_eval(page, "return app.currentId;") == sid
    # A cold page has no live transcript to fall back to. Earlier final output
    # must still render after later continuations have extended canonical history.
    page.reload()
    expect(page.locator(".msg-pane:visible")).to_contain_text(
        "LIVE_PARENT_AFTER_AGENTS", timeout=10000,
    )
    for i in range(3):
        expect(page.locator(".msg-pane:visible")).to_contain_text(f"LIVE_CONTINUATION_{i}")
    assert _app_eval(page, "return app.currentId;") == sid
    _assert_no_browser_errors(page, errors)
