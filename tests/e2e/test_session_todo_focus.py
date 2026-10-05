"""The real Todo entry acquires focus after Alpine exposes its modal."""

from playwright.sync_api import expect

from .test_settings_save_lifecycle import (
    settings_fixture as settings_fixture,
    settings_frontend_url as settings_frontend_url,
)


def _todo_entry(page):
    page.locator(".settings-modal .modal-close").click()
    expect(page.locator(".settings-modal")).to_be_hidden()
    return page.locator(".session-todo-btn"), page.locator(".session-todo-modal")


def _defer_native_show(page, *, hold_retry=False):
    # Delegate both the original Alpine rAF/show callback and native focus.
    # Expose the modal once that callback and the first focus attempt arrive.
    page.evaluate("""holdRetry => {
      const app = document.querySelector('#app')._x_dataStack[0];
      const originalToggle = Element.prototype._x_toggleAndCascadeWithTransitions;
      if (typeof originalToggle !== 'function') throw new Error('Missing Alpine x-show');
      const gate = window.__todoFocusGate = {
        installed:true, armed:true, showHeld:false, showReleased:false,
        attempted:false, firstAttempt:null, retryReserved:false, retryHeld:false,
        retryReleased:false,
      };
      let showCallback = null, retryCallback = null;
      const releaseShow = () => {
        if (!gate.attempted || !showCallback || gate.showReleased) return;
        gate.showReleased=true; showCallback();
      };
      Element.prototype._x_toggleAndCascadeWithTransitions = function(el,value,show,hide,...args) {
        if (value && gate.armed && el.querySelector(':scope > .session-todo-modal')) {
          gate.armed=false;
          return originalToggle.call(this,el,value,() => {
            gate.showHeld=true; showCallback=show; releaseShow();
          },hide,...args);
        }
        return originalToggle.call(this,el,value,show,hide,...args);
      };
      const originalFocus = app._focusWithoutScroll;
      app._focusWithoutScroll = function(el,...args) {
        const first = el?.matches('.session-todo-modal') && !gate.attempted;
        const before = first ? {
          display:getComputedStyle(el.parentElement).display,
          focusable:this._focusableElements(el).length,
        } : null;
        const result = originalFocus.call(this,el,...args);
        if (first) {
          gate.attempted=true; gate.firstAttempt={...before, success:result};
          releaseShow();
        }
        return result;
      };
      if (holdRetry) {
        const originalAfterPaint = app._afterPaint;
        app._afterPaint = function(callback) {
          if (gate.attempted && !gate.retryReserved) {
            gate.retryReserved=true;
            return originalAfterPaint.call(this,() => {
              gate.retryHeld=true; retryCallback=callback;
            });
          }
          return originalAfterPaint.call(this,callback);
        };
      }
      window.__releaseTodoFocusRetry = () => {
        if (!retryCallback) throw new Error('Native afterPaint callback not held');
        gate.retryReleased=true; retryCallback();
      };
    }""", hold_retry)
    assert page.evaluate("() => window.__todoFocusGate.installed")


def _assert_deferred_show(page):
    page.wait_for_function("() => window.__todoFocusGate.showReleased")
    assert page.evaluate("() => window.__todoFocusGate.firstAttempt") == {
        "display": "none", "focusable": 0, "success": False,
    }


def _release_retry(page):
    page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0];
      await new Promise(resolve => app.$nextTick(resolve));
      window.__releaseTodoFocusRetry();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    assert page.evaluate("() => window.__todoFocusGate.retryReleased")


def _assert_no_errors(page, fixture):
    assert fixture["errors"] == []
    assert page.evaluate("() => window.__settingsUnhandled") == []


def test_todo_entry_focuses_compose_after_native_show(page, settings_fixture):
    entry, modal = _todo_entry(page)
    _defer_native_show(page)
    entry.click()
    _assert_deferred_show(page)
    expect(modal).to_be_visible()
    compose = modal.locator(".session-todo-compose input")
    expect(compose).to_be_focused()
    page.keyboard.type("Synthetic draft")
    expect(compose).to_have_value("Synthetic draft")
    page.keyboard.press("Tab")
    expect(modal.locator(".session-todo-compose button")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(compose).to_be_focused()
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    expect(entry).to_be_focused()
    _assert_no_errors(page, settings_fixture)


def test_old_todo_retry_preserves_reopened_modal_focus_and_draft(page, settings_fixture):
    entry, modal = _todo_entry(page)
    _defer_native_show(page, hold_retry=True)
    entry.click()
    _assert_deferred_show(page)
    page.wait_for_function("() => window.__todoFocusGate.retryHeld")
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    expect(entry).to_be_focused()
    entry.click()
    compose = modal.locator(".session-todo-compose input")
    expect(compose).to_be_focused()
    page.keyboard.type("Reopened draft")
    page.keyboard.press("Tab")
    target = modal.locator(".session-todo-compose button")
    expect(target).to_be_focused()
    _release_retry(page)
    expect(target).to_be_focused()
    expect(compose).to_have_value("Reopened draft")
    _assert_no_errors(page, settings_fixture)


def test_old_todo_retry_preserves_new_modal_focus(page, settings_fixture):
    entry, modal = _todo_entry(page)
    _defer_native_show(page, hold_retry=True)
    entry.click()
    _assert_deferred_show(page)
    page.wait_for_function("() => window.__todoFocusGate.retryHeld")
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    page.locator("button.icon-btn:has(use[href='#i-settings'])").click()
    settings = page.locator(".settings-modal")
    expect(settings).to_be_visible()
    target = settings.locator(".settings-menu-item.active")
    expect(target).to_be_focused()
    _release_retry(page)
    expect(target).to_be_focused()
    expect(modal).to_be_hidden()
    _assert_no_errors(page, settings_fixture)
