"""Native task receipts stay visible and do not imply successful execution."""
from playwright.sync_api import expect

from .test_activity_center import _login


def test_native_cron_interruption_recovery_and_noop_are_visible(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid = page.evaluate("document.querySelector('#app')._x_dataStack[0].currentId")
    state = {"runtime_state": "interrupted"}
    posts = []
    def tasks(route):
        task = {"job_id": "fixture-cron", "cron": "* * * * *", "prompt": "Fixture inspection",
                "recurring": True, "durable": False, "record_saved": True,
                "last_error": "not_executed", "last_status": "failed", **state}
        route.fulfill(json={"tasks": [task], "count": 1,
                            "scheduled_active": state["runtime_state"] == "unconfirmed"})
    def recover(route):
        posts.append(route.request.method)
        state["runtime_state"] = "unconfirmed"
        route.fulfill(json={"status": "recovering"})
    page.route(f"**/api/chat/sessions/{sid}/scheduled-tasks", tasks)
    page.route(f"**/api/chat/sessions/{sid}/scheduled-tasks/recover", recover)
    page.evaluate("""async sid => {
        const app = document.querySelector('#app')._x_dataStack[0];
        app.lang = 'zh'; await app.openNativeScheduledTasks(sid);
    }""", sid)
    expect(page.locator(".sched-native-state")).to_have_text("运行环境已断开")
    expect(page.locator(".sched-native-outcome")).to_have_text("上次触发未获得有效执行结果")
    assert page.evaluate("document.querySelector('#app')._x_dataStack[0].currentSessionScheduledCount()") == 1
    page.get_by_role("button", name="恢复任务会话", exact=True).click()
    expect(page.locator(".sched-native-state")).to_have_text("会话已恢复，任务待确认")
    assert posts == ["POST"]
    assert "CLI 已确认持久化" not in page.locator(".sched-native-meta").inner_text()
    state["runtime_state"] = "missing"
    page.evaluate("document.querySelector('#app')._x_dataStack[0].loadNativeScheduledTasks()")
    # A missing receipt is history: it is neither counted nor listed as a task.
    expect(page.locator(".sched-native-state")).to_have_count(0)
    assert page.evaluate("document.querySelector('#app')._x_dataStack[0].currentSessionScheduledCount()") == 0


def test_scheduler_modal_opens_one_standalone_surface_per_entry(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    sid = page.evaluate("document.querySelector('#app')._x_dataStack[0].currentId")
    task = {"job_id": "fixture-cron", "cron": "* * * * *", "prompt": "Fixture inspection",
            "recurring": True, "durable": False, "record_saved": True, "runtime_state": "active"}
    page.route(f"**/api/chat/sessions/{sid}/scheduled-tasks",
               lambda route: route.fulfill(json={"tasks": [task], "count": 1, "scheduled_active": True}))
    page.evaluate("""sid => {
        const app = document.querySelector('#app')._x_dataStack[0];
        app.lang = 'zh';
        app.tabState[sid].messages.push({role: 'tool_use', name: 'Bash', id: 'bg1',
            task_status: {task_id: 'bg-task-1', state: 'running', summary: 'Fixture background job'}});
    }""", sid)
    page.locator(".topbar-tasks-background").click()
    expect(page.locator("#scheduler-title")).to_have_text("后台任务")
    expect(page.locator(".sched-bg-card")).to_have_count(1)
    expect(page.locator(".sched-native-section")).to_be_hidden()
    expect(page.locator(".sched-input-name")).to_be_hidden()
    assert page.locator(".sched-tab").count() == 0
    page.evaluate("document.querySelector('#app')._x_dataStack[0].closeScheduler()")
    page.locator(".topbar-tasks-native").click()
    expect(page.locator("#scheduler-title")).to_have_text("原生 Cron")
    expect(page.locator(".sched-native-state")).to_be_visible()
    expect(page.locator(".sched-bg-card")).to_be_hidden()
    expect(page.locator(".sched-input-name")).to_be_hidden()
    page.evaluate("document.querySelector('#app')._x_dataStack[0].closeScheduler()")
    page.locator(".topbar-tasks-scheduled").click()
    expect(page.locator("#scheduler-title")).to_have_text("定时任务")
    expect(page.locator(".sched-input-name")).to_be_visible()
    expect(page.locator(".sched-native-section")).to_be_hidden()
    expect(page.locator(".sched-bg-card")).to_be_hidden()
