"""History must reach beyond the recent poll window and retain calendar dates."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import (
    _app_eval, _login, _capture_browser_errors, _assert_no_browser_errors,
)


@pytest.mark.parametrize("width", [1440, 390])
def test_history_pages_all_dates_and_opens_outside_recent_window(
    page, backend_url, auth_token, width,
):
    errors = _capture_browser_errors(page)
    page.set_viewport_size({"width": width, "height": 900})
    _login(page, backend_url, auth_token)
    response = page.request.post(
        backend_url + "/api/chat/sessions",
        headers={"X-Auth-Token": auth_token},
        data={"name": "Archived history fixture", "model": "deepseek-v4-pro"},
    )
    assert response.ok
    old_sid = response.json()["id"]
    fixture = _app_eval(page, """
        app.lang = 'zh'; app.mobileTab = 'chat';
        const now = new Date();
        const stamp = days => new Date(now.getFullYear(), now.getMonth(),
          now.getDate() - days, 12).getTime() / 1000;
        const day = t => {const d = new Date(t * 1000); return [d.getFullYear(),
          String(d.getMonth()+1).padStart(2,'0'), String(d.getDate()).padStart(2,'0')].join('-');};
        const row = (id, name, days, pinned=false) => ({id, name,
          cwd:app.currentWorkspacePath(), model:'deepseek-v4-pro',
          permission:'default', updated_at:stamp(days), created_at:stamp(days), pinned});
        const recent = Array.from({length:105}, (_, i) =>
          row('recent-history-' + i, 'Recent fixture ' + i, i < 55 ? 0 : 1));
        const older = row(arg, 'Archived history fixture', 45);
        const year = row('previous-year-history', 'Previous year fixture', 400);
        const pinned = row('pinned-history', 'Pinned history fixture', 400, true);
        const full = [pinned, ...recent, older, year];
        app.sessions = full.slice(0,100);
        return {full, recent:full.slice(0,100), dates:[day(stamp(1)),day(stamp(45)),day(stamp(400))]};
    """, old_sid)
    reads = []

    def sessions(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if "limit" not in query:
            route.continue_()
            return
        limit = int(query["limit"][0])
        reads.append(limit)
        rows = fixture["full"] if limit == 0 else fixture["recent"]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"sessions": rows, "total": len(fixture["full"])}))

    page.route("**/api/chat/sessions?*", sessions)
    page.locator("#history-picker-trigger").click()
    page.wait_for_function("Alpine.$data(document.querySelector('#app'))._sessionHistoryLoaded === true")
    popup = page.locator("#history-picker-pop")
    rows = popup.locator(".session-picker-row")
    more = popup.locator(".session-picker-load-more")
    expect(rows).to_have_count(20)
    assert popup.locator(".session-picker-group-label").all_text_contents() == ["置顶", "今天"]
    more.click()
    expect(rows).to_have_count(40)
    assert reads.count(0) == 1
    _app_eval(page, "await app._pullSessionList(false);")
    assert reads[-1] == 100
    expect(rows).to_have_count(40)
    # Search includes older pages and restarts its own display budget.
    search = popup.locator(".session-picker-search")
    search.fill("Recent fixture")
    page.wait_for_function("Alpine.$data(document.querySelector('#app')).sessionHistoryVisibleCount === 20")
    expect(rows).to_have_count(20)
    more.click()
    expect(rows).to_have_count(40)
    search.fill("Archived history fixture")
    expect(rows).to_have_count(1)
    expect(more).not_to_be_visible()
    search.fill("")
    page.wait_for_function("Alpine.$data(document.querySelector('#app')).sessionHistoryVisibleCount === 20")
    expect(rows).to_have_count(20)
    for count in (40, 60, 80, 100, len(fixture["full"])):
        more.click()
        expect(rows).to_have_count(count)
    expect(more).not_to_be_visible()
    assert popup.locator(".session-picker-group-label").all_text_contents() == ["置顶", "今天", *fixture["dates"]]
    # A new visit starts with exactly one page, even after reaching the end.
    page.keyboard.press("Escape")
    page.locator("#history-picker-trigger").click()
    page.wait_for_function("Alpine.$data(document.querySelector('#app'))._sessionHistoryLoaded === true")
    expect(rows).to_have_count(20)
    for count in (40, 60, 80, 100, len(fixture["full"])):
        more.click()
        expect(rows).to_have_count(count)
    popup.locator(".session-picker-row button").filter(has_text="Archived history fixture").click()
    expect(popup).not_to_be_visible()
    page.wait_for_function("sid => Alpine.$data(document.querySelector('#app')).currentId === sid", arg=old_sid)
    expect(page.locator(f'.chat-tab[data-tid="{old_sid}"]')).to_contain_text("Archived history fixture")
    _assert_no_browser_errors(page, errors)


def test_history_failed_full_load_can_retry(page, backend_url, auth_token):
    errors = _capture_browser_errors(page)
    _login(page, backend_url, auth_token)
    reads = []

    def sessions(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("limit") != ["0"]:
            route.continue_()
            return
        reads.append(0)
        if len(reads) == 1:
            route.fulfill(status=503, content_type="application/json", body='{}')
        else:
            route.fulfill(status=200, content_type="application/json", body='{"sessions": []}')

    page.route("**/api/chat/sessions?*", sessions)
    page.locator("#history-picker-trigger").click()
    retry = page.locator(".session-picker-retry")
    expect(retry).to_be_visible()
    retry.click()
    page.wait_for_function("Alpine.$data(document.querySelector('#app'))._sessionHistoryLoaded === true")
    expect(retry).not_to_be_visible()
    assert len(reads) == 2
    _assert_no_browser_errors(page, errors)
