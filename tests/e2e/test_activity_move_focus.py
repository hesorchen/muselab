"""Activity move menu focus across Alpine's deferred visibility change."""

from playwright.sync_api import expect

from .test_settings_save_lifecycle import (
    settings_fixture as settings_fixture,
    settings_frontend_url as settings_frontend_url,
)


def _activity_menu(page):
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      app.lang='en'; app.activity.viewLoaded=true; app.activity.view='groups';
      app.activity.loading=false; app.activity.expanded={};
      app.activity.customGroups=[{id:'research',name:'Research',color:'violet'},
        {id:'delivery',name:'Delivery',color:'green'}];
      app.activity.groupOrder=['research','delivery','__ungrouped__'];
      app.activity.events=[{id:'move-focus-row',session_id:'move-focus-session',
        session_name:'Move focus session',task_summary:'Synthetic activity task',
        workspace:'/tmp/synthetic-activity',workspace_name:'Synthetic',state:'running',
        read:true,started_at:30,finished_at:0,updated_at:30}];
      app.activity.summary={running:1,unread:0,attention:0,
        groups:{review:0,running:1,failed:0,history:0},
        group_unread:{review:0,running:0,failed:0,history:0},workspaces:[]};
      app.activity.show=true;
      await new Promise(resolve => app.$nextTick(resolve));
    }""")
    row = page.locator(".activity-row-wrap").filter(has_text="Move focus session")
    row.hover()
    return row.locator(".activity-row-group"), page.locator(".activity-move-menu")


def _defer_native_show(page, *, hold_retry=False):
    # Keep the actual vendored x-show callback and focus implementation. Release
    # show only once the first real focus attempt and Alpine's rAF both arrive.
    page.evaluate("""holdRetry => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const originalToggle = Element.prototype._x_toggleAndCascadeWithTransitions;
      if (typeof originalToggle !== 'function') throw new Error('Missing Alpine x-show');
      const gate = window.__activityFocusGate = {
        installed:true, armed:true, showHeld:false, showReleased:false,
        attempted:false, firstAttempt:null, retryHeld:false, retryReleased:false,
      };
      let showCallback = null, retryCallback = null;
      const releaseShow = () => {
        if (!gate.attempted || !showCallback || gate.showReleased) return;
        gate.showReleased=true;
        showCallback();
      };
      Element.prototype._x_toggleAndCascadeWithTransitions = function(el,value,show,hide,...args) {
        if (el.matches('.activity-move-layer') && value && gate.armed) {
          gate.armed=false;
          return originalToggle.call(this,el,value,() => {
            gate.showHeld=true; showCallback=show; releaseShow();
          },hide,...args);
        }
        return originalToggle.call(this,el,value,show,hide,...args);
      };
      const originalFocus = app._focusWithoutScroll;
      app._focusWithoutScroll = function(el,...args) {
        const isFirst = el?.matches('.activity-move-menu') && !gate.attempted;
        const attempt = isFirst ? {
          display:getComputedStyle(el.closest('.activity-move-layer')).display,
          focusable:this._focusableElements(el).length,
        } : null;
        const result = originalFocus.call(this,el,...args);
        if (isFirst) {
          gate.attempted=true; gate.firstAttempt={...attempt, success:result};
          releaseShow();
        }
        return result;
      };
      if (holdRetry) {
        const originalAfterPaint = app._afterPaint;
        app._afterPaint = function(callback) {
          if (gate.attempted && !gate.retryHeld && !retryCallback) {
            return originalAfterPaint.call(this,() => {
              gate.retryHeld=true; retryCallback=callback;
            });
          }
          return originalAfterPaint.call(this,callback);
        };
      }
      window.__releaseActivityFocusRetry = () => {
        if (!retryCallback) throw new Error('Native afterPaint callback not held');
        gate.retryReleased=true; retryCallback();
      };
    }""", hold_retry)
    assert page.evaluate("() => window.__activityFocusGate.installed")


def _assert_deferred_show(page):
    page.wait_for_function("() => window.__activityFocusGate.showReleased")
    assert page.evaluate("() => window.__activityFocusGate.firstAttempt") == {
        "display": "none", "focusable": 0, "success": False,
    }


def _release_retry(page):
    # Wait through the actual nextTick/paint boundary after the new surface has
    # taken focus, then execute the old held callback; no timing sleep.
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      await new Promise(resolve => app.$nextTick(resolve));
      window.__releaseActivityFocusRetry();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    assert page.evaluate("() => window.__activityFocusGate.retryReleased")


def test_move_menu_focuses_after_native_show(page, settings_fixture):
    trigger, menu = _activity_menu(page)
    _defer_native_show(page)
    trigger.click()
    _assert_deferred_show(page)
    expect(menu).to_be_visible()
    expect(menu.locator("button")).to_have_count(3)
    expect(menu.locator("button").first).to_be_focused()
    page.keyboard.press("End")
    expect(menu.locator("button").last).to_be_focused()
    page.keyboard.press("Home")
    expect(menu.locator("button").first).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(menu.locator("button").last).to_be_focused()
    assert settings_fixture["errors"] == []


def test_old_move_menu_retry_preserves_reopened_menu_focus(page, settings_fixture):
    trigger, menu = _activity_menu(page)
    _defer_native_show(page, hold_retry=True)
    trigger.click()
    _assert_deferred_show(page)
    page.wait_for_function("() => window.__activityFocusGate.retryHeld")
    expect(menu).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_be_focused()
    trigger.click()
    expect(menu.locator("button").first).to_be_focused()
    page.keyboard.press("End")
    expect(menu.locator("button").last).to_be_focused()
    _release_retry(page)
    expect(menu.locator("button").last).to_be_focused()
    assert settings_fixture["errors"] == []


def test_old_move_menu_retry_preserves_new_modal_focus(page, settings_fixture):
    trigger, menu = _activity_menu(page)
    _defer_native_show(page, hold_retry=True)
    trigger.click()
    _assert_deferred_show(page)
    page.wait_for_function("() => window.__activityFocusGate.retryHeld")
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    page.locator(".activity-modal .modal-close").click()
    expect(menu).to_be_hidden()
    page.locator("button.icon-btn:has(use[href='#i-settings'])").click()
    settings = page.locator(".settings-modal")
    expect(settings).to_be_visible()
    target = settings.locator(".settings-menu-item.active")
    expect(target).to_be_focused()
    _release_retry(page)
    expect(target).to_be_focused()
    expect(menu).to_be_hidden()
    assert settings_fixture["errors"] == []
