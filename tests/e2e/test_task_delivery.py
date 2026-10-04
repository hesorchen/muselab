"""Real browser, delivery API, temporary Git/files and public SDK-shaped evidence."""

from pathlib import Path
import subprocess
import sys
import pytest
import time
import uuid

from playwright.sync_api import expect

from .test_chat_render_perf import (
    _app_eval,
    _capture_browser_errors,
    _assert_no_browser_errors,
    _login,
    _route_windowed_session,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Safe checkpoint previews require Linux file notifications"
)


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def delivery_workspace(page, backend_url, auth_token, tmp_path):
    # Each attempt owns its Git baseline, including pytest reruns sharing a server.
    root = tmp_path / "e2e-root-delivery"
    root.mkdir()
    headers = {"X-Auth-Token": auth_token}
    registered = page.request.post(
        f"{backend_url}/api/chat/workspaces", headers=headers, data={"path": str(root)}
    )
    assert registered.ok
    try:
        yield root
    finally:
        _app_eval(page, "await app.switchWorkspace(app.primaryWorkspacePath());")
        removed = page.request.delete(
            f"{backend_url}/api/chat/workspaces", headers=headers, params={"path": str(root)}
        )
        assert removed.ok


def test_delivery_artifact_evidence_diff_and_guarded_preview(
    page, backend_url, auth_token, monkeypatch, tmp_path, delivery_workspace
):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.lang='en';")
    sid = _app_eval(page, "return app.currentId;")
    response = page.request.get(
        f"{backend_url}/api/chat/sessions/{sid}/runtime", headers={"X-Auth-Token": auth_token}
    )
    assert response.ok
    sessions_dir = Path(response.json()["workspace"]) / "sessions"
    assert "e2e-root" in str(sessions_dir)
    root = delivery_workspace
    _app_eval(
        page,
        "await app.fetchSessionWorkspaces(); await app.switchWorkspace(arg);",
        str(root),
    )
    assert _app_eval(page, "return await app._ensureSessionRegistered(app.currentId);")
    sid = _app_eval(page, "return app.currentId;")
    response = page.request.get(
        f"{backend_url}/api/chat/sessions/{sid}/runtime", headers={"X-Auth-Token": auth_token}
    )
    assert response.ok and response.json()["workspace"] == str(root)
    target = root / "result.txt"
    target.write_text("before\n")
    (root / ".gitignore").write_text("*\n!.gitignore\n!result.txt\n")
    _git(root, "init", "--initial-branch=delivery-fixture")
    _git(root, "add", ".gitignore", "result.txt")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture baseline",
    )
    from backend import task_delivery, file_checkpoints
    from claude_agent_sdk import AssistantMessage, UserMessage, ToolUseBlock, ToolResultBlock

    monkeypatch.setattr(task_delivery.sess, "SESS_DIR", sessions_dir)
    monkeypatch.setattr(file_checkpoints.sess, "SESS_DIR", sessions_dir)
    turn, cid, mid = (str(uuid.uuid4()) for _ in range(3))
    task_delivery.begin(sid, turn, root)
    file_checkpoints.begin(sid, turn, root)
    file_checkpoints.record_id(sid, turn, cid)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "after\n"},
        "cwd": str(root),
    }
    for event in ("PreToolUse", "PostToolUse"):
        if event == "PostToolUse":
            target.write_text("after\n")
        task_delivery.record_tool(sid, turn, root, event, payload, "write_fixture")
        file_checkpoints.observe(sid, turn, root, event, payload, "write_fixture")
    message = AssistantMessage(
        content=[
            ToolUseBlock(id="write_fixture", name="Write", input=payload["tool_input"]),
            ToolUseBlock(
                id="bash_fixture",
                name="Bash",
                input={"command": "python -m pytest tests/test_sample.py"},
            ),
        ],
        model="fixture",
    )
    message.uuid = mid
    task_delivery.observe_message(sid, turn, message)
    task_delivery.observe_message(
        sid,
        turn,
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="bash_fixture", content="1 passed", is_error=False)
            ],
            tool_use_result={"stdout": "1 passed", "stderr": "", "interrupted": False},
        ),
    )
    _route_windowed_session(
        page,
        sid,
        [
            {
                "role": "assistant",
                "text": "Canonical command evidence fixture",
                "html": "<p>Canonical command evidence fixture</p>",
                "uuid": mid,
                "ts": time.time(),
            }
        ],
    )
    _app_eval(page, "await app.loadSession(arg);", sid)
    page.locator(".workbench-more > summary").click()
    page.locator(".workbench-more .task-delivery-trigger").click()
    expect(page.locator(".workbench-more")).not_to_have_attribute("open", "")
    assert page.locator(".task-delivery-trigger use").get_attribute("href") != page.locator(".session-todo-btn use").get_attribute("href")
    panel = page.locator(".task-delivery-panel")
    expect(panel).to_be_visible()
    assert panel.evaluate("el => getComputedStyle(el).backgroundColor") == page.evaluate(
        "() => { const el=document.createElement('div');el.style.background='var(--c-bg-1)';document.body.append(el);const c=getComputedStyle(el).backgroundColor;el.remove();return c; }"
    )
    expect(panel.locator(".delivery-environment")).to_contain_text(str(root))
    expect(panel.locator(".delivery-environment")).to_contain_text("delivery-fixture")
    expect(panel.locator(".delivery-command")).to_contain_text("Tool completed")
    expect(panel.locator(".delivery-command")).to_contain_text("exit code unavailable")
    expect(panel.locator(".delivery-diff")).to_contain_text("+after")
    expect(panel.locator(".delivery-diff")).to_contain_text("-before")
    panel.get_by_role("button", name="Inspect evidence", exact=True).click()
    expect(page.locator(f'.msg[data-uuid="{mid}"]')).to_have_class(
        __import__("re").compile(r"msg-highlight")
    )
    page.locator(".workbench-more > summary").click()
    page.locator(".workbench-more .task-delivery-trigger").click()
    panel.get_by_role("button", name="result.txt", exact=True).click()
    expect(panel).not_to_be_visible()
    page.wait_for_function(
        "() => document.querySelector('#app')._x_dataStack[0].selected === 'result.txt'"
    )
    expect(page.locator(".preview-runtime-source")).to_have_count(0)
    page.locator(".terminal-manager-btn").click()
    page.locator(".terminal-create-btn").click()
    page.wait_for_function(
        """() => {
            const app = document.querySelector('#app')._x_dataStack[0];
            return app.activeTerminal()?.status === 'running'
                && app.terminalConnection === 'connected'
                && app._terminalSocket?.readyState === WebSocket.OPEN;
        }"""
    )
    expect(page.locator(".preview-runtime-source")).to_have_count(0)
    terminal = _app_eval(page, "return app.activeTerminal();")
    assert terminal["cwd"] == str(root)
    # Close the fixture shell via its authenticated API; it ran no command.
    closed = page.request.delete(
        f"{backend_url}/api/terminals/{terminal['id']}",
        headers={"X-Auth-Token": auth_token},
        params={"workspace": str(root)},
    )
    assert closed.ok
    page.locator(".workbench-more > summary").click()
    page.locator(".workbench-more .task-delivery-trigger").click()
    panel.get_by_role("button", name="Preview restore scope", exact=True).click()
    preview = panel.locator(".delivery-restore-preview")
    expect(preview).to_contain_text("result.txt")
    expect(preview).to_contain_text("no dry-run")
    expect(preview.locator(".delivery-restore-button")).to_be_disabled()
    # A real external change is detected server-side; no SDK or model runs.
    target.write_text("external editor\n")
    panel.get_by_role("button", name="Preview restore scope", exact=True).click()
    expect(preview).to_contain_text("File changed outside observed tools")
    expect(preview.locator(".delivery-restore-button")).to_be_disabled()
    page.screenshot(path=str(tmp_path / "delivery-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    box = panel.bounding_box()
    assert box and box["width"] <= 390 and box["x"] >= 0
    assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "delivery-mobile.png"), full_page=True)
    _assert_no_browser_errors(page, errors)
