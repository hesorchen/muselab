"""Popup geometry must follow real anchors across mobile layouts and keyboards."""

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login, _capture_browser_errors, _assert_no_browser_errors


def _geometry(page, anchor, popup):
    return page.evaluate("""([anchor, popup]) => {
      const a = document.querySelector(anchor).getBoundingClientRect();
      const p = document.querySelector(popup).getBoundingClientRect();
      const v = visualViewport;
      return {anchor: a.toJSON(), popup: p.toJSON(),
        viewport: {left: v.offsetLeft, top: v.offsetTop, width: v.width, height: v.height}};
    }""", [anchor, popup])


def _assert_fits(g):
    p, v = g['popup'], g['viewport']
    assert p['width'] > 0 and p['height'] > 0, g
    assert p['left'] >= v['left'] + 7, g
    assert p['right'] <= v['left'] + v['width'] - 7, g
    assert p['top'] >= v['top'] + 7, g
    assert p['bottom'] <= v['top'] + v['height'] - 7, g


def _assert_attached(g):
    a, p = g['anchor'], g['popup']
    gap = min(abs(p['top'] - a['bottom']), abs(a['top'] - p['bottom']))
    assert 3 <= gap <= 9, g
    assert p['right'] >= a['left'] and p['left'] <= a['right'], g


def test_mobile_more_menu_anchors_to_button_and_fits_short_viewport(page, backend_url, auth_token, tmp_path):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({'width': 390, 'height': 844})
    _login(page, backend_url, auth_token)
    _app_eval(page, "app.lang = 'en'; app.mobileTab = 'chat';")
    anchor, popup = '.workbench-more > summary', '.workbench-more-actions'
    page.locator(anchor).click()
    expect(page.locator(popup)).to_be_visible()
    page.wait_for_timeout(150)
    g = _geometry(page, anchor, popup)
    _assert_attached(g)
    _assert_fits(g)
    # Rotate while still open: the scrollable menu must stay beside the anchor.
    page.set_viewport_size({'width': 844, 'height': 260})
    page.wait_for_timeout(150)
    g = _geometry(page, anchor, popup)
    _assert_attached(g)
    _assert_fits(g)
    last = page.locator(popup).get_by_role('button').last
    last.scroll_into_view_if_needed()
    assert last.evaluate("""el => {
      const r = el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2));
    }"""), 'The last menu action must be reachable inside the visible pane'
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(tmp_path / 'more-menu-mobile.png'))
    page.keyboard.press('Escape')
    expect(page.locator(popup)).not_to_be_visible()
    expect(page.locator(anchor)).to_be_focused()
    _assert_no_browser_errors(page, errors)


@pytest.mark.parametrize('surface', ['history', 'editor', 'context'])
def test_popovers_reposition_after_resize(page, backend_url, auth_token, surface):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    _login(page, backend_url, auth_token)
    if surface == 'history':
        anchor, popup = '#history-picker-trigger', '#history-picker-pop'
    elif surface == 'editor':
        _app_eval(page, "await app.openFile({path:'README.md', name:'README.md', type:'file'}, {reveal:true});")
        anchor, popup = '.tab-picker-btn', '.tab-picker-pop'
    else:
        anchor, popup = '.chat-toolbar-ring', '.ctx-breakdown-pop'
    page.locator(anchor).click()
    expect(page.locator(popup)).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    _app_eval(page, "app.mobileTab = arg;", 'preview' if surface == 'editor' else 'chat')
    page.wait_for_timeout(180)
    g = _geometry(page, anchor, popup)
    _assert_attached(g)
    _assert_fits(g)
    _assert_no_browser_errors(page, errors)


def test_history_popup_stays_in_offset_keyboard_viewport_and_updates_content(page, backend_url, auth_token):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({'width': 390, 'height': 844})
    _login(page, backend_url, auth_token)
    page.locator('#history-picker-trigger').click()
    popup = page.locator('#history-picker-pop')
    expect(popup).to_be_visible()
    page.evaluate("""() => {
      Object.defineProperties(visualViewport, {
        height: {configurable:true, value:260}, offsetTop: {configurable:true, value:24},
      });
      visualViewport.dispatchEvent(new Event('resize'));
      visualViewport.dispatchEvent(new Event('scroll'));
    }""")
    page.wait_for_timeout(150)
    _assert_fits(_geometry(page, '#history-picker-trigger', '#history-picker-pop'))
    # A search/rename may change the panel after it has opened.
    page.locator('.session-picker-search').fill('missing fixture result')
    page.wait_for_timeout(450)
    _assert_fits(_geometry(page, '#history-picker-trigger', '#history-picker-pop'))
    page.evaluate("""() => {
      delete visualViewport.height; delete visualViewport.offsetTop;
      visualViewport.dispatchEvent(new Event('resize'));
    }""")
    page.wait_for_timeout(150)
    _assert_attached(_geometry(page, '#history-picker-trigger', '#history-picker-pop'))
    page.keyboard.press('Escape')
    expect(popup).not_to_be_visible()
    expect(page.locator('#history-picker-trigger')).to_be_focused()
    _assert_no_browser_errors(page, errors)


def _pull_file_tree(page, distance, end='touchend'):
    page.locator('.filelist').evaluate("""(el, {distance, end}) => {
      el.scrollTop = 0;
      const fire = (name, touches) => {
        const event = new Event(name, {bubbles:true, cancelable:true});
        Object.defineProperty(event, 'touches', {value:touches});
        el.dispatchEvent(event);
      };
      fire('touchstart', [{clientY:200}]);
      fire('touchmove', [{clientY:200+distance}]);
      fire(end, []);
    }""", {'distance': distance, 'end': end})


def test_pull_refresh_reuses_file_button_spinner_without_extra_overlay(browser, backend_url, auth_token, tmp_path):
    context = browser.new_context(viewport={'width':390, 'height':844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    errors = _capture_browser_errors(page)
    try:
        _login(page, backend_url, auth_token)
        _app_eval(page, "app.mobileTab = 'files'; await app.reloadTree();")
        refresh = page.get_by_role('button', name='Refresh file tree', exact=True)
        expect(refresh).to_be_enabled()
        expect(page.locator('.ptr-indicator')).to_have_count(0)
        pending = []
        page.route('**/api/files/changes*', lambda route: pending.append(route))
        page.route('**/api/files/bootstrap*', lambda route: pending.append(route))
        _pull_file_tree(page, 60)
        page.wait_for_timeout(100)
        assert not pending
        _pull_file_tree(page, 160, 'touchcancel')
        page.wait_for_timeout(100)
        assert not pending
        _pull_file_tree(page, 160)
        expect(refresh).to_be_disabled()
        expect(refresh).to_have_attribute('aria-busy', 'true')
        expect(refresh.locator('svg')).to_have_class('spinning')
        expect(page.locator('.ptr-indicator')).to_have_count(0)
        _pull_file_tree(page, 160)
        page.wait_for_timeout(150)
        assert len(pending) == 1
        page.screenshot(path=str(tmp_path / 'file-refresh-mobile.png'))
        route = pending.pop(0)
        route.fulfill(response=route.fetch())
        expect(refresh).to_be_enabled()
        expect(refresh).not_to_have_attribute('aria-busy', 'true')
        expect(refresh.locator('svg')).not_to_have_class('spinning')
        # The ordinary button uses the same request and loading feedback.
        refresh.click()
        expect(refresh).to_be_disabled()
        page.wait_for_timeout(100)
        assert len(pending) == 1
        route = pending.pop(0)
        route.fulfill(response=route.fetch())
        expect(refresh).to_be_enabled()
        _assert_no_browser_errors(page, errors)
    finally:
        context.close()
