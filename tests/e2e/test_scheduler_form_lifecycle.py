"""Synthetic scheduler writes keep later drafts and confirmed task state."""
from __future__ import annotations

import copy
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_settings_save_lifecycle import settings_frontend_url as settings_frontend_url


def _task(task_id, name):
    return {"id": task_id, "name": name, "prompt": f"Prompt for {name}",
            "model": "fixture", "enabled": True, "session_mode": "fresh",
            "schedule": {"kind": "daily", "hour": 9, "minute": 0}}


@pytest.fixture
def scheduler_fixture(page, settings_frontend_url):
    page.set_default_timeout(5000)
    state = {"tasks": [_task("task-a", "Task A"), _task("task-b", "Task B")],
             "writes": [], "reads": [], "hold_read": False, "read_status": 200,
             "requests": [], "errors": []}
    page.on("pageerror", lambda error: state["errors"].append(str(error)))

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        state["requests"].append((request.method, path))
        if path == "/api/scheduler/tasks" and request.method == "GET":
            if state["hold_read"]:
                state["hold_read"] = False
                state["reads"].append((route, copy.deepcopy(state["tasks"])))
            elif state["read_status"] != 200:
                route.fulfill(status=state["read_status"], json={"detail": "Synthetic refresh failure"})
            else:
                route.fulfill(json={"tasks": copy.deepcopy(state["tasks"]), "unread_count": 0})
        elif path.startswith("/api/scheduler/tasks/") and request.method in {"PATCH", "DELETE"}:
            state["writes"].append(route)
        elif path == "/api/scheduler/history":
            route.fulfill(json={"history": [], "unread_count": 0})
        elif path == "/api/meta":
            route.fulfill(json={"asset_version": "__MUSELAB_ASSET_VERSION__"})
        else:
            # Static-only server: every application API stays synthetic,
            # including settings, native Cron projections and optional reads.
            route.fulfill(json={})

    page.route("**/api/**", handle)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(settings_frontend_url, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('#app')?._x_dataStack?.[0]")
    page.evaluate("""() => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.token='scheduler-synthetic-token-min-32chars';
      app.authed=true; app.appReady=true; app.model='fixture';
      app.availableModels=[{model:'fixture',label:'Fixture',group:'Fixture'}];
      window.__schedulerSettled={save:0,delete:0,load:0};
      window.__schedulerUnhandled=[];
      for (const [method,key] of [['createSchedTask','save'],['deleteSchedTask','delete'],
                                  ['loadSchedulerTasks','load']]) {
        const original=app[method];
        app[method]=function(...args) {
          const promise=original.apply(this,args);
          promise.then(()=>window.__schedulerSettled[key]++,()=>window.__schedulerSettled[key]++);
          return promise;
        };
      }
      window.addEventListener('unhandledrejection',event=>{
        window.__schedulerUnhandled.push(String(event.reason?.message||event.reason));
      });
    }""")
    _open(page)
    expect(_row(page, "Task A")).to_be_visible()
    yield state
    for route in state["writes"] + [route for route, _ in state["reads"]]:
        try:
            route.abort()
        except Exception:
            pass


def _open(page):
    page.locator(".workbench-more:visible > summary").click()
    page.get_by_role("button", name="Scheduled tasks", exact=True).click()
    expect(page.locator(".sched-modal")).to_be_visible()


def _row(page, name):
    return page.locator(".sched-task-card").filter(
        has=page.locator(".sched-row-name > span:first-child", has_text=name))


def _edit(page, name):
    _row(page, name).locator('button[title="Edit"]').click()
    expect(page.locator(".sched-input-name")).to_have_value(name)


def _save(page):
    return page.locator(".sched-create-foot .btn-primary")


def _wait_count(page, values, count):
    deadline = time.monotonic() + 5
    while len(values) < count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(values) >= count


def _finish_write(page, state, *, status=200):
    route = state["writes"].pop(0)
    method = route.request.method
    task_id = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
    key = "delete" if method == "DELETE" else "save"
    settled = page.evaluate("key=>window.__schedulerSettled[key]", key)
    task = next(task for task in state["tasks"] if task["id"] == task_id)
    if status == 200:
        if method == "DELETE":
            state["tasks"] = [item for item in state["tasks"] if item["id"] != task_id]
        else:
            task.update(route.request.post_data_json)
        route.fulfill(json={"ok": True} if method == "DELETE" else copy.deepcopy(task))
    else:
        route.fulfill(status=status, json={"detail": "Synthetic write rejection"})
    page.wait_for_function("([key,count])=>window.__schedulerSettled[key]>count", arg=[key, settled])
    return task


def test_pending_scheduler_save_preserves_other_task_draft(page, scheduler_fixture):
    _edit(page, "Task A")
    page.locator(".sched-create-prompt").fill("Saved A prompt")
    _save(page).click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    _edit(page, "Task B")
    page.locator(".sched-create-prompt").fill("Unsaved B prompt")
    _save(page).click()
    _wait_count(page, scheduler_fixture["writes"], 2)
    expect(_save(page)).to_be_disabled()
    _finish_write(page, scheduler_fixture)
    expect(page.locator(".sched-input-name")).to_have_value("Task B")
    expect(page.locator(".sched-create-prompt")).to_have_value("Unsaved B prompt")
    expect(_row(page, "Task A").locator(".sched-row-prompt")).to_have_text("Saved A prompt")
    expect(_save(page)).to_be_disabled()  # A's finally cannot release B's Save.
    page.locator(".sched-create-prompt").fill("Later B edit")
    _finish_write(page, scheduler_fixture)
    expect(_save(page)).to_be_enabled()
    expect(page.locator(".sched-create-prompt")).to_have_value("Later B edit")
    expect(_row(page, "Task B").locator(".sched-row-prompt")).to_have_text("Unsaved B prompt")
    _save(page).click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    _finish_write(page, scheduler_fixture)
    expect(page.locator(".sched-input-name")).to_have_value("")
    expect(_row(page, "Task B").locator(".sched-row-prompt")).to_have_text("Later B edit")
    assert scheduler_fixture["errors"] == []
    assert page.evaluate("() => window.__schedulerUnhandled") == []


def test_scheduler_failed_save_retries_without_clearing_later_edits(page, scheduler_fixture):
    _edit(page, "Task A")
    page.locator(".sched-create-prompt").fill("First draft")
    _save(page).click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    _finish_write(page, scheduler_fixture, status=503)
    expect(page.locator(".toast").filter(has_text="Save failed")).to_be_visible()
    expect(page.locator(".sched-create-prompt")).to_have_value("First draft")
    expect(_save(page)).to_be_enabled()
    _save(page).click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    page.locator(".sched-create-prompt").fill("Later unsaved edit")
    scheduler_fixture["read_status"] = 503
    _finish_write(page, scheduler_fixture)
    expect(page.locator(".sched-input-name")).to_have_value("Task A")
    expect(page.locator(".sched-create-prompt")).to_have_value("Later unsaved edit")
    expect(_row(page, "Task A").locator(".sched-row-prompt")).to_have_text("First draft")
    expect(page.locator(".toast").filter(has_text="Saved, but the task list could not refresh")).to_be_visible()
    expect(_save(page)).to_be_enabled()
    assert scheduler_fixture["errors"] == []
    assert page.evaluate("() => window.__schedulerUnhandled") == []


@pytest.mark.parametrize("refresh_status", [200, 503])
def test_confirmed_scheduler_delete_rejects_late_list_and_keeps_other_draft(
    page, scheduler_fixture, refresh_status,
):
    _edit(page, "Task B")
    page.locator(".sched-create-prompt").fill("Unsaved B prompt")
    scheduler_fixture["hold_read"] = True
    page.locator(".sched-modal .modal-close").click()
    _open(page)
    _wait_count(page, scheduler_fixture["reads"], 1)
    _row(page, "Task A").locator('button[title="Delete"]').click()
    page.locator(".confirm-modal .btn-danger").click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    _finish_write(page, scheduler_fixture, status=503)
    expect(_row(page, "Task A")).to_have_count(1)
    expect(page.locator(".sched-create-prompt")).to_have_value("Unsaved B prompt")
    expect(page.locator(".toast").filter(has_text="Delete failed HTTP 503")).to_be_visible()
    _row(page, "Task A").locator('button[title="Delete"]').click()
    page.locator(".confirm-modal .btn-danger").click()
    _wait_count(page, scheduler_fixture["writes"], 1)
    scheduler_fixture["read_status"] = refresh_status
    _finish_write(page, scheduler_fixture)
    # Confirmation belongs to A. B's editor must remain, regardless of the
    # refresh HTTP result or the older GET whose body was captured pre-delete.
    after_delete_ids = page.evaluate("() => document.querySelector('#app')._x_dataStack[0].scheduler.tasks.map(t=>t.id)")
    route, old_tasks = scheduler_fixture["reads"].pop()
    settled = page.evaluate("() => window.__schedulerSettled.load")
    route.fulfill(json={"tasks": old_tasks, "unread_count": 0})
    page.wait_for_function("count=>window.__schedulerSettled.load>count", arg=settled)
    expect(page.locator(".sched-input-name")).to_have_value("Task B")
    expect(page.locator(".sched-create-prompt")).to_have_value("Unsaved B prompt")
    assert "task-a" not in after_delete_ids
    expect(_row(page, "Task A")).to_have_count(0)
    expect(_row(page, "Task B")).to_have_count(1)
    assert scheduler_fixture["errors"] == []
    assert page.evaluate("() => window.__schedulerUnhandled") == []
