"""Rejected runtime settings must not return when their tab is reactivated."""
from __future__ import annotations

import copy
import time

import pytest
from playwright.sync_api import expect

from .test_session_controls_owner import (
    _choose,
    _finish,
    _select,
    _tab,
    control_fixture as control_fixture,
    settings_frontend_url as settings_frontend_url,
)


@pytest.fixture
def runtime_fixture(page, control_fixture):
    control_fixture["sessions"][1]["effort"] = "low"
    page.evaluate("""sessions => {
      const app=document.querySelector('#app')._x_dataStack[0];
      app.sessions=sessions;
      app.availableModels=app.availableModels.map(model=>({
        ...model, supports_effort:true,
        effort_levels:['auto','low','medium','high'], supports_fast:true,
      }));
      for (const [field,method] of [
        ['effort','onEffortChange'], ['service_tier','onServiceTierChange'],
      ]) {
        window.__settingSettled[field]=0;
        const change=app[method];
        app[method]=function(...args) {
          const promise=change.apply(this,args);
          promise.then(()=>window.__settingSettled[field]++,()=>window.__settingSettled[field]++);
          return promise;
        };
      }
    }""", copy.deepcopy(control_fixture["sessions"]))
    return control_fixture


def _runtime_control(page, field):
    return _select(page, field) if field == "effort" else page.locator(".chat-toolbar-fast")


def _choose_runtime(page, state, field, value):
    if field == "effort":
        return _choose(page, state, field, value)
    count = len(state["pending"])
    button = _runtime_control(page, field)
    expect(button).to_be_visible()
    expect(button).to_be_enabled()
    button.click()
    deadline = time.monotonic() + 5
    while len(state["pending"]) == count and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(state["pending"]) == count + 1
    route = state["pending"][-1]
    assert route.request.post_data_json[field] == value
    return route


def _expect_runtime_value(page, field, value):
    control = _runtime_control(page, field)
    if field == "effort":
        expect(control).to_have_value(value)
    else:
        if value == "fast":
            expect(control).to_have_attribute("aria-pressed", "true")
        else:
            expect(control).not_to_have_attribute("aria-pressed", "true")


@pytest.mark.parametrize("field", ["effort", "service_tier"], ids=["effort", "fast"])
@pytest.mark.parametrize("status", [503, 200], ids=["rejected", "confirmed"])
def test_late_runtime_result_retains_owner_and_other_tab_draft(page, runtime_fixture, field, status):
    original = "auto" if field == "effort" else ""
    chosen_a = "high" if field == "effort" else "fast"
    chosen_b = "medium" if field == "effort" else "fast"
    first = _choose_runtime(page, runtime_fixture, field, chosen_a)
    _tab(page, "control-b").click()
    expect(_tab(page, "control-b")).to_have_class("chat-tab active")
    second = _choose_runtime(page, runtime_fixture, field, chosen_b)
    page.locator(".chat-input-textarea").fill("B typed during runtime save")

    _finish(page, runtime_fixture, field, first, status)
    expect(_tab(page, "control-b")).to_have_class("chat-tab active")
    _expect_runtime_value(page, field, chosen_b)
    expect(_runtime_control(page, field)).to_have_attribute("aria-busy", "true")
    expect(page.locator(".chat-input-textarea")).to_have_value("B typed during runtime save")
    _finish(page, runtime_fixture, field, second, 200)
    _expect_runtime_value(page, field, chosen_b)
    assert _runtime_control(page, field).get_attribute("aria-busy") in (None, "false")

    _tab(page, "control-a").click()
    expected_a = chosen_a if status == 200 else original
    _expect_runtime_value(page, field, expected_a)
    expect(page.locator(".chat-input-textarea")).to_have_value("Retained A chat draft")
    if status != 200:
        marker = "_effortExpected" if field == "effort" else "_serviceTierExpected"
        assert page.evaluate("key=>document.querySelector('#app')._x_dataStack[0].tabState['control-a'][key]===null", marker)
    page.locator(".chat-tab-history > button").click()
    page.wait_for_function("() => { const app=document.querySelector('#app')._x_dataStack[0]; return app.sessionPickerOpen && !app.sessionHistoryLoading; }")
    _expect_runtime_value(page, field, expected_a)
    assert page.evaluate("field=>document.querySelector('#app')._x_dataStack[0].sessions.find(s=>s.id==='control-a')[field]", field) == expected_a
    page.locator(".chat-tab-history > button").click()
    _tab(page, "control-b").click()
    _expect_runtime_value(page, field, chosen_b)
    expect(page.locator(".chat-input-textarea")).to_have_value("B typed during runtime save")
    assert runtime_fixture["errors"] == []
    assert page.evaluate("() => window.__settingUnhandled") == []
