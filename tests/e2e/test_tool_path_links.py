"""Click tool paths in the real Alpine transcript and open workspace preview."""
import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import Page, expect


def _login(page, backend_url, auth_token):
    page.goto(backend_url, wait_until='domcontentloaded')
    page.wait_for_selector('.login, .chat-tabs-list', state='visible')
    if page.locator('.login').is_visible():
        page.fill('.login input[type="password"]', auth_token)
        page.keyboard.press('Enter')
    page.wait_for_function('''() => {
        const app = document.querySelector('#app')?._x_dataStack?.[0];
        return app && app.authed && app.appReady && app._sessionsInitialized && app.currentId;
    }''')


@pytest.mark.parametrize("width", [1440, 390])
def test_bash_summary_and_output_paths_open_preview(page: Page, backend_url, auth_token, width):
    page.set_viewport_size({"width": width, "height": 900})
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    _login(page, backend_url, auth_token)
    page.evaluate('''async () => {
        const app = document.querySelector('#app')._x_dataStack[0];
        const response = await fetch('/api/files/write', {
          method: 'PUT', headers: {...app.fileHdr(), 'Content-Type':'application/json'},
          body: JSON.stringify({path:'scripts/check.sh', content:'#!/bin/sh\\necho SCRIPT_PREVIEW_OK\\n'}),
        });
        if (!response.ok) throw new Error('fixture creation failed: '+response.status);
        app.conciseChat = false;
        const sid = app.currentId, st = app._ensureTabState(sid);
        st._loaded = true;
        st.messages = [
          {role:'tool_use', name:'Bash', summary:'bash scripts/check.sh',
           input:{command:'bash scripts/check.sh'}, uuid:'path-use', _k:'path-use'},
          {role:'tool_result', tool_name:'Bash', text:'scripts/check.sh:12:3\\n<script>window.__toolXss = 1</script>',
           preview:'scripts/check.sh:12:3', uuid:'path-result', _k:'path-result'},
        ];
        st.messageRange.visibleEnd = st.messages.length;
        st.messageRange.total = st.messages.length;
        app._activateTabState(sid);
        st.messagesReady = true;
    }''')
    summary = page.locator('.tool .tool-arg .file-link[data-path="scripts/check.sh"]')
    expect(summary).to_be_visible()
    await_url = page.url
    summary.click()
    page.wait_for_function('''() => {
        const app=document.querySelector('#app')._x_dataStack[0];
        return app.selected === 'scripts/check.sh' && app.rawText.includes('SCRIPT_PREVIEW_OK');
    }''')
    assert page.url == await_url
    page.evaluate("document.querySelector('#app')._x_dataStack[0].mobileTab = 'chat'")
    page.locator('.tool-result-head').click()
    output = page.locator('.bash-stdout .file-link[data-path="scripts/check.sh"]')
    expect(output).to_be_visible()
    expect(output).to_have_attribute('data-line', '12')
    output.click()
    assert page.evaluate('window.__toolXss || 0') == 0
    assert not errors


def test_tool_path_renderer_preserves_text_and_workspace_ownership(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = page.evaluate(r'''() => {
        const app = document.querySelector('#app')._x_dataStack[0];
        const original = app.currentWorkspacePath;
        const m = {};
        app.currentWorkspacePath = () => '/workspace';
        try {
          const text = 'bash "scripts/示例 脚本.sh" ./run.sh:9 --output=results/out.txt https://example.org/a.sh user@example.com 1.2.3 <img src=x onerror="window.__toolXss=2">';
          const el=document.createElement('div');
          el.innerHTML=app.toolTextHtml(m,text);
          const paths=[...el.querySelectorAll('.file-link')].map(a=>a.dataset.path);
          const exactText=el.textContent===text;
          const safe=el.querySelector('img')===null;
          const absolute='/workspace/scripts/a.sh';
          const first=app.toolTextHtml(m,absolute);
          app.currentWorkspacePath=()=>'/different';
          const second=app.toolTextHtml(m,absolute);
          return {paths,exactText,safe,wasLink:first.includes('data-path='),isLink:second.includes('data-path=')};
        } finally {app.currentWorkspacePath=original;}
    }''')
    assert result == {
        'paths': ['scripts/示例 脚本.sh', './run.sh', 'results/out.txt'],
        'exactText': True, 'safe': True, 'wasLink': True, 'isLink': False,
    }


def test_read_tool_existing_file_link_still_opens_preview(page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.evaluate('''() => {
        const app=document.querySelector('#app')._x_dataStack[0];
        app.conciseChat=false;
        const sid=app.currentId, st=app._ensureTabState(sid);
        st._loaded=true;
        st.messages=[{role:'tool_use', name:'Read', summary:'notes.md',
          input:{file_path:'notes.md'}, uuid:'read-existing', _k:'read-existing'}];
        st.messageRange.visibleEnd=1;
        st.messageRange.total=1;
        app._activateTabState(sid);
        st.messagesReady=true;
    }''')
    link = page.locator('.tool > a.file-link[data-path="notes.md"]')
    expect(link).to_be_visible()
    link.click()
    page.wait_for_function('''() => {
        const app=document.querySelector('#app')._x_dataStack[0];
        return app.selected==='notes.md' && app.rawText.includes('scratch');
    }''')
