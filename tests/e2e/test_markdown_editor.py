"""Actual editor input, save continuity, lazy loading and bounded live preview."""
from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

from .test_files_preview import _login

APP = "document.querySelector('#app')._x_dataStack[0]"


def _open(page, backend_url, auth_token, text="# Editor fixture\n\n中文测试\n", path="editor-fixture.md"):
    _login(page, backend_url, auth_token)
    page.evaluate("""async ({text, path}) => {
        const app = document.querySelector('#app')._x_dataStack[0];
        const response = await fetch('/api/files/write', {method:'PUT',
          headers:{...app.fileHdr(), 'Content-Type':'application/json'},
          body:JSON.stringify({path,content:text})});
        if (!response.ok) throw new Error('fixture write failed');
        await app.openFile({path,name:path});
        await app.toggleEdit();
    }""", {"text": text, "path": path})
    page.wait_for_function("window.__muselab_cm && !" + APP + ".editorLoading")
    page.wait_for_function("!" + APP + ".editorPreviewBusy")


@pytest.mark.parametrize("theme", ["light", "dark", "eyecare"])
def test_editor_theme_contrast_and_real_keyboard_after_view_switch(page, backend_url, auth_token, theme):
    page.add_init_script(f"localStorage.setItem('muselab_theme', '{theme}');")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _open(page, backend_url, auth_token)
    styles = page.evaluate("""() => {
      const cm = document.querySelector('.editor-cm .CodeMirror');
      const button = document.querySelector('.editor-view-switch .active');
      const colors = el => { const c=getComputedStyle(el); return [c.color,c.backgroundColor]; };
      const lum = rgb => {
        const c=rgb.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{
          x/=255; return x<=.04045 ? x/12.92 : ((x+.055)/1.055)**2.4;});
        return c[0]*.2126+c[1]*.7152+c[2]*.0722;
      };
      const contrast = el => { const c=colors(el).map(lum).sort((a,b)=>b-a); return (c[0]+.05)/(c[1]+.05); };
      return {text:contrast(cm),button:contrast(button),font:getComputedStyle(cm).fontSize};
    }""")
    assert styles["text"] >= 4.5
    assert styles["button"] >= 4.5
    assert styles["font"] == "16px"
    page.locator(".editor-view-switch button").nth(0).click()
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+Home")
    page.keyboard.type("abc")
    assert page.evaluate("window.__muselab_cm.getValue().startsWith('abc')")
    page.locator(".editor-view-switch button").nth(1).click()
    page.locator(".CodeMirror").click()
    page.keyboard.press("Control+End")
    page.keyboard.insert_text("中文输入")
    assert page.evaluate("window.__muselab_cm.getValue().endsWith('中文输入')")
    page.keyboard.press("Control+a")
    selection = page.locator(".CodeMirror-selected").first
    assert selection.is_visible()
    assert selection.evaluate("el => getComputedStyle(el).backgroundColor") != "rgba(0, 0, 0, 0)"
    bg = selection.evaluate("el => getComputedStyle(el).backgroundColor")
    assert bg != page.locator(".CodeMirror").evaluate("el => getComputedStyle(el).backgroundColor")
    # Clicking a toolbar button must not make the selected range disappear.
    page.locator(".editor-font-controls button").first.click()
    expect(page.locator(".CodeMirror-selected").first).to_have_css("background-color", bg)
    assert errors == []


@pytest.mark.parametrize("saved", [None, "edit", "preview", "split"])
def test_each_markdown_session_defaults_to_split(page, backend_url, auth_token, saved):
    if saved:
        page.add_init_script(f"localStorage.setItem('muselab_editor_view','{saved}');")
    _open(page, backend_url, auth_token)
    assert page.evaluate(APP + ".editorView") == "split"
    page.locator(".editor-view-switch button").nth(2).click()
    page.evaluate("async () => { const app=" + APP + "; await app.toggleEdit(); await app.toggleEdit(); }")
    page.wait_for_function("window.__muselab_cm")
    assert page.evaluate(APP + ".editorView") == "split"


def test_save_preserves_instance_cursor_history_and_clean_generation(page, backend_url, auth_token):
    _open(page, backend_url, auth_token, "line\n" * 180)
    result = page.evaluate("""async () => {
      const app = document.querySelector('#app')._x_dataStack[0], cm=window.__muselab_cm;
      cm.setCursor({line:100,ch:2}); cm.replaceSelection('saved'); cm.scrollIntoView(cm.getCursor());
      const cursor=cm.getCursor(), scroll=cm.getScrollInfo().top, undo=cm.historySize().undo;
      let renders=0; app._renderPreviewMd=()=>{renders++;throw new Error('save must not render');};
      await app.saveEdit();
      const saved={same:cm===window.__muselab_cm,editing:app.editing,dirty:app._editorDirty(),
        cursor:cm.getCursor(),scroll:cm.getScrollInfo().top,undo:cm.historySize().undo};
      cm.replaceSelection('new'); const dirty=app._editorDirty(); cm.undo();
      return {saved,cursor,scroll,undo,dirty,cleanAfterUndo:!app._editorDirty(),renders};
    }""")
    assert result["saved"] == {"same": True, "editing": True, "dirty": False,
                                "cursor": result["cursor"], "scroll": result["scroll"], "undo": result["undo"]}
    assert result["dirty"] and result["cleanAfterUndo"]
    assert result["renders"] == 0


def test_pending_save_new_edits_duplicate_and_failure_preserve_content(page, backend_url, auth_token):
    _open(page, backend_url, auth_token, "中文测试")
    result = page.evaluate("""async () => {
      const app=document.querySelector('#app')._x_dataStack[0], cm=window.__muselab_cm;
      const fetchOriginal=window.fetch; let finish, writes=0;
      window.fetch=(url,opts)=>String(url).includes('/api/files/write')
        ? (writes++,new Promise(resolve=>{finish=()=>resolve(new Response('{}',{status:200}));}))
        : fetchOriginal(url,opts);
      const p=app.saveEdit({exitAfterSave:true}); await app.saveEdit({exitAfterSave:true});
      cm.setCursor({line:0,ch:4}); cm.replaceSelection('后续输入');
      finish(); await p;
      const newer={dirty:app._editorDirty(),editing:app.editing,body:cm.getValue(),saved:app.rawText};
      cm.undo(); const cleanAfterUndo=!app._editorDirty();
      window.fetch=(url,opts)=>String(url).includes('/api/files/write')
        ? Promise.resolve(new Response('write failed',{status:500})) : fetchOriginal(url,opts);
      cm.replaceSelection('未保存'); await app.saveEdit({exitAfterSave:true});
      window.fetch=fetchOriginal;
      return {writes,newer,cleanAfterUndo,error:app.editorSaveState,dirty:app._editorDirty(),body:cm.getValue()};
    }""")
    assert result["writes"] == 1
    assert result["newer"] == {"dirty": True, "editing": True, "body": "中文测试后续输入", "saved": "中文测试"}
    assert result["cleanAfterUndo"]
    assert result["error"] == "error" and result["dirty"]
    assert "未保存" in result["body"]


def test_large_preview_reuses_unchanged_dom_and_latest_content(page, backend_url, auth_token):
    text = "---\nname: fixture\n---\n\n" + ("## Section\n\n中文正文 **bold** and [reference][ref].\n\n" * 1500) + "\n[ref]: https://example.com/\n"
    _open(page, backend_url, auth_token, text)
    assert page.locator(".editor-preview-block").count() > 3
    assert page.locator(".editor-live-preview a").first.get_attribute("href") == "https://example.com/"
    assert "name: fixture" not in page.locator(".editor-live-preview").inner_text()
    page.evaluate(r"""() => {
      window.firstPreviewBlock=document.querySelector('.editor-preview-block');
      const cm=window.__muselab_cm; cm.setCursor(cm.lineCount()-1,0); cm.replaceSelection('\nlatest-marker\n');
    }""")
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    assert page.evaluate("window.firstPreviewBlock===document.querySelector('.editor-preview-block')")
    assert "latest-marker" in page.locator(".editor-live-preview").text_content()
    page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      window.__muselab_cm.setValue('# old request'); app._renderLivePreview();
      window.__muselab_cm.setValue('# latest request'); app._scheduleLivePreview(0);
    }""")
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    assert page.locator(".editor-live-preview h1").inner_text() == "latest request"


def test_editor_bytes_math_table_and_safe_html(page, backend_url, auth_token):
    _open(page, backend_url, auth_token, "中文测试")
    assert page.evaluate(APP + ".cmStatus.bytes") == 12
    page.evaluate(r"""() => {
      window.__muselab_cm.setValue('# Math\n\n$\\sum_{i=1}^{n} i$\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n<img src=x onerror="window.editorXss=1">');
    }""")
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    assert page.locator(".editor-live-preview .katex").count() > 0
    assert page.locator(".editor-live-preview table").count() == 1
    assert page.locator(".editor-live-preview [onerror]").count() == 0
    assert page.evaluate("window.editorXss || 0") == 0


def test_preload_waits_for_markdown_mode_before_mount(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/cm/mode/markdown/markdown.min.js*", lambda route: (time.sleep(0.5), route.continue_()))
    page.evaluate("""async () => {
      const app=document.querySelector('#app')._x_dataStack[0];
      await app.openFile({path:'notes.md',name:'notes.md'});
      app._loadCodemirror();
      const timer=setInterval(()=>{if(window.CodeMirror){clearInterval(timer);app.toggleEdit();}},5);
    }""")
    page.wait_for_function("window.__muselab_cm && !" + APP + ".editorLoading")
    assert page.evaluate("window.__muselab_cm.getMode().name") == "markdown"


def test_loader_failure_fallback_retry_preserves_typed_text(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/cm/mode/markdown/markdown.min.js*", lambda route: route.abort())
    page.evaluate("async () => { const app=" + APP + "; await app.openFile({path:'notes.md',name:'notes.md'}); await app.toggleEdit(); }")
    page.locator(".editor-fallback").fill("fallback 中文 draft")
    page.unroute("**/cm/mode/markdown/markdown.min.js*")
    page.evaluate(APP + ".mountCM()")
    page.wait_for_function("window.__muselab_cm")
    assert page.evaluate("window.__muselab_cm.getValue()") == "fallback 中文 draft"
    assert page.evaluate(APP + "._editorDirty()")


def test_mobile_split_and_font_control_fit_viewport(page, backend_url, auth_token):
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, backend_url, auth_token)
    page.evaluate(APP + ".setMobileTab('preview')")
    boxes = page.evaluate("""() => {
      const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};};
      return {editor:box('.editor-cm'),preview:box('.editor-live-preview'),font:box('.editor-font-controls')};
    }""")
    assert boxes["preview"]["y"] >= boxes["editor"]["y"] + boxes["editor"]["h"] - 1
    assert boxes["editor"]["h"] > 50 and boxes["preview"]["h"] > 50
    assert boxes["font"]["x"] + boxes["font"]["w"] <= 390


@pytest.mark.parametrize("change", ["same", "insert", "failure"])
def test_file_refresh_button_preserves_visible_anchor(page, backend_url, auth_token, change):
    _login(page, backend_url, auth_token)
    before = page.evaluate("""async change => {
      const app=document.querySelector('#app')._x_dataStack[0];
      app._stopFileEvents();
      const rows=Array.from({length:300},(_,i)=>({path:`file-${String(i).padStart(4,'0')}.md`,
        name:`file-${String(i).padStart(4,'0')}.md`,is_dir:false,size:1,mtime:1}));
      let calls=0; const realFetch=window.fetch;
      window.fetch=(url,opts)=>{
        if (String(url).includes('/api/files/bootstrap')) {
          calls++;
          if (calls>1 && change==='failure') return Promise.resolve(new Response('',{status:500}));
          const entries=calls>1 && change==='insert'
            ? [{path:'aaa.md',name:'aaa.md',is_dir:false,size:1,mtime:1},...rows] : rows;
          return Promise.resolve(new Response(JSON.stringify({entries,cursor:1}),{status:200}));
        }
        if (String(url).includes('/api/files/list')) return Promise.resolve(new Response('',{status:500}));
        return realFetch(url,opts);
      };
      await app.reloadTree(); await app.$nextTick();
      const list=app._fileTreeList();
      list.scrollTop=app._fileTreeRowHeight()*100+7;
      app._syncFileTreeViewport(list); await app.$nextTick();
      window.anchorPath=app.visible[100].path;
      const row=list.querySelector(`li[data-path="${window.anchorPath}"]`);
      const before={top:list.scrollTop,y:row.getBoundingClientRect().top};
      const realReload=app.reloadTree;
      app.reloadTree=async (...args)=>{try{return await realReload.apply(app,args);}finally{window.refreshFinished=true;}};
      return before;
    }""", change)
    page.get_by_role("button", name="Refresh file tree", exact=True).click()
    page.wait_for_function("window.refreshFinished")
    after = page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0], list=app._fileTreeList();
      return {top:list.scrollTop,y:list.querySelector(`li[data-path="${window.anchorPath}"]`).getBoundingClientRect().top};
    }""")
    assert abs(after["y"] - before["y"]) <= 1
    if change != "insert":
        assert abs(after["top"] - before["top"]) <= 1


def test_preview_worker_failure_is_safe_and_retryable(page, backend_url, auth_token):
    page.add_init_script("window.Worker = undefined;")
    _open(page, backend_url, auth_token, "# Safe fallback\n\n<script>window.editorXss=1</script>")
    assert page.locator(".editor-live-preview pre").inner_text().startswith("# Safe fallback")
    assert page.evaluate("window.editorXss || 0") == 0
    assert page.evaluate(APP + ".editorPreviewError")


def test_leaving_editor_during_cold_load_does_not_mount_stale_instance(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    page.route("**/cm/mode/markdown/markdown.min.js*", lambda route: (time.sleep(0.3), route.continue_()))
    page.evaluate("""async () => {
      const app=document.querySelector('#app')._x_dataStack[0];
      await app.openFile({path:'notes.md',name:'notes.md'}); await app.toggleEdit();
      setTimeout(()=>app.toggleEdit(),10);
      await app._loadCodemirror(); await new Promise(resolve=>setTimeout(resolve,100));
    }""")
    assert not page.evaluate(APP + ".editing")
    assert page.locator(".editor-cm .CodeMirror").count() == 0
    assert page.evaluate("window.__muselab_cm === null")


@pytest.mark.parametrize("mode", ["read", "split", "preview", "mobile"])
def test_outline_follows_rendered_headings_and_navigates(page, backend_url, auth_token, mode):
    if mode == "mobile":
        page.set_viewport_size({"width": 390, "height": 844})
    text = "# Top **title**\n\n```md\n# Not a heading\n```\n\n" + ("Body paragraph.\n\n" * 80) + "## Repeated\n\n" + ("More body.\n\n" * 80) + "### Repeated\n"
    _open(page, backend_url, auth_token, text)
    if mode == "read":
        page.evaluate(APP + ".toggleEdit()")
    elif mode == "preview":
        page.evaluate(APP + ".setEditorView('preview')")
    elif mode == "mobile":
        page.evaluate(APP + ".setMobileTab('preview')")
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    page.locator(".markdown-outline-toggle").click()
    items = page.locator(".markdown-outline-items button")
    page.wait_for_function(APP + ".markdownOutline.length === 3")
    assert items.all_text_contents() == ["Top title", "Repeated", "Repeated"]
    if mode == "split":
        # The unchanged read preview may not trigger a renderedMd watcher.
        # Keeping the outline open across edit/read still needs new DOM targets.
        page.evaluate(APP + ".toggleEdit()")
        page.wait_for_function("!" + APP + ".editing")
    items.nth(2).click()
    position = page.evaluate("""() => {
      const app=document.querySelector('#app')._x_dataStack[0];
      const root=app.editing ? app.$refs.editorPreview : app.$refs.markdownPreview;
      const scroller=app.editing ? root.parentElement : app.$refs.previewBody;
      const heading=root.querySelector('h3').getBoundingClientRect(), box=scroller.getBoundingClientRect();
      return {top:scroller.scrollTop,visible:heading.top>=box.top-1 && heading.top<box.bottom,
        overflow:document.documentElement.scrollWidth>window.innerWidth};
    }""")
    assert position["top"] > 0 and position["visible"]
    assert not position["overflow"]
    if mode == "split":
        page.evaluate(APP + ".toggleEdit()")
        page.wait_for_function("window.__muselab_cm && !" + APP + ".editorPreviewBusy")
    if mode != "read":
        page.evaluate("window.__muselab_cm.setValue('# Updated title\\n\\n## Next section')")
        page.wait_for_function(APP + ".markdownOutline[0]?.text === 'Updated title'")
        assert items.all_text_contents() == ["Updated title", "Next section"]
    page.locator(".markdown-outline-items button").first.focus()
    page.keyboard.press("Escape")
    expect(page.locator("#markdown-outline")).to_be_hidden()
    assert page.locator(".markdown-outline-toggle").evaluate("el => el === document.activeElement")


def test_save_button_returns_to_updated_preview_and_shortcut_keeps_editing(page, backend_url, auth_token):
    _open(page, backend_url, auth_token)
    page.evaluate("window.__muselab_cm.setValue('# Saved heading')")
    page.locator('.CodeMirror').click()
    page.keyboard.press('Control+s')
    page.wait_for_function(APP + ".editorSaveState === 'saved'")
    assert page.evaluate(APP + '.editing')
    page.evaluate("window.__muselab_cm.setValue('# Final heading')")
    page.locator('button[x-show="previewSurface===\'file\' && editing"]').click()
    page.wait_for_function("!" + APP + ".editing && !window.__muselab_cm")
    page.wait_for_function("document.querySelector('[x-ref=markdownPreview] h1')?.textContent === 'Final heading'")
    disk = page.evaluate("""async () => {
      const app=document.querySelector('#app')._x_dataStack[0];
      return await (await fetch('/api/files/read?path=editor-fixture.md',{headers:app.fileHdr()})).text();
    }""")
    assert disk == '# Final heading'


def test_outline_clears_on_file_switch_and_empty_document(page, backend_url, auth_token):
    _open(page, backend_url, auth_token)
    page.locator('.markdown-outline-toggle').click()
    page.wait_for_function(APP + '.markdownOutline.length === 1')
    page.evaluate("window.__muselab_cm.setValue('No headings here')")
    page.wait_for_function('!' + APP + '.editorPreviewBusy && ' + APP + '.markdownOutline.length === 0')
    assert page.locator('.markdown-outline-items p').is_visible()
    page.evaluate("async () => {const app=" + APP + "; await app.saveEdit({exitAfterSave:true}); await app.openFile({path:'notes.md',name:'notes.md'});}")
    expect(page.locator('#markdown-outline')).to_be_hidden()



def test_cold_editor_core_can_arrive_after_fifteen_seconds(page, backend_url, auth_token):
    page.route("**/cm/codemirror.min.js*", lambda route: (time.sleep(16), route.continue_()))
    _open(page, backend_url, auth_token)
    assert page.evaluate("window.__muselab_cm.getMode().name") == "markdown"
    assert not page.evaluate(APP + ".editorError")
    page.locator('.CodeMirror').click()
    page.keyboard.press('Control+End')
    page.keyboard.insert_text('slow network input')
    assert page.evaluate("window.__muselab_cm.getValue().endsWith('slow network input')")


def test_cold_preview_download_does_not_consume_parse_budget(page, backend_url, auth_token):
    delayed = []

    def slow_worker(route):
        if not delayed:
            delayed.append(True)
            time.sleep(6)
        route.continue_()

    page.route("**/render-worker.js*", slow_worker)
    _open(page, backend_url, auth_token, '# Slow transport\n\nStill rendered')
    expect(page.locator('.editor-live-preview h1')).to_have_text('Slow transport')
    assert not page.evaluate(APP + '.editorPreviewError')


def test_worker_cpu_stall_still_falls_back_without_blocking_editor(page, backend_url, auth_token):
    page.route("**/render-worker.js*", lambda route: route.fulfill(
        content_type='text/javascript',
        body='self.onmessage = () => { self.postMessage({parsing:true}); while (true) {} };',
    ))
    _open(page, backend_url, auth_token, '# Safe CPU fallback')
    expect(page.locator('.editor-live-preview pre')).to_contain_text('# Safe CPU fallback')
    assert page.evaluate(APP + '.editorPreviewError')
    page.locator('.CodeMirror').click()
    page.keyboard.press('Control+End')
    page.keyboard.type('responsive')
    assert page.evaluate("window.__muselab_cm.getValue().endsWith('responsive')")


def test_preview_keeps_position_when_visible_block_is_replaced(page, backend_url, auth_token):
    # Many short paragraphs fit one parser chunk but occupy a tall viewport.
    _open(page, backend_url, auth_token, "# Long document\n\n" + "Paragraph.\n\n" * 350)
    page.evaluate("""() => {
      const cm=window.__muselab_cm, p=document.querySelector('.editor-live-preview');
      cm.setCursor({line:350,ch:3}); cm.focus();
      p.scrollTop=(p.scrollHeight-p.clientHeight)*0.55;
    }""")
    page.wait_for_timeout(150)
    page.evaluate("""() => {
      const cm=window.__muselab_cm;
      cm.setCursor(cm.coordsChar({left:80,top:cm.getScrollInfo().top+80},'local'));
    }""")
    before = page.locator('.editor-live-preview').evaluate('p=>p.scrollTop')
    assert before > 1000
    page.keyboard.insert_text('changed')
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    page.wait_for_timeout(150)
    after = page.locator('.editor-live-preview').evaluate('p=>p.scrollTop')
    assert abs(after-before) < 40
    assert 'changed' in page.locator('.editor-live-preview').text_content()


def test_split_scroll_sync_is_bidirectional_and_survives_view_changes(page, backend_url, auth_token):
    _open(page, backend_url, auth_token, "# Scroll fixture\n\n" + "## Heading\n\nParagraph.\n\n" * 250)
    measure = """() => {
      const c=window.__muselab_cm.getScrollInfo(), p=document.querySelector('.editor-live-preview');
      return {editor:c.top/(c.height-c.clientHeight),preview:p.scrollTop/(p.scrollHeight-p.clientHeight)};
    }"""
    page.evaluate("const c=window.__muselab_cm,s=c.getScrollInfo();c.scrollTo(null,(s.height-s.clientHeight)*0.6)")
    page.wait_for_function("() => {const c=window.__muselab_cm.getScrollInfo(),p=document.querySelector('.editor-live-preview');return Math.abs(c.top/(c.height-c.clientHeight)-p.scrollTop/(p.scrollHeight-p.clientHeight))<0.025}")
    page.locator('.editor-live-preview').evaluate('p=>p.scrollTop=(p.scrollHeight-p.clientHeight)*0.3')
    page.wait_for_function("() => {const c=window.__muselab_cm.getScrollInfo();return Math.abs(c.top/(c.height-c.clientHeight)-0.3)<0.025}")
    page.wait_for_timeout(200)
    positions=page.evaluate(measure)
    assert abs(positions['editor']-positions['preview']) < 0.025
    page.locator('.editor-view-switch button').nth(0).click()
    page.evaluate("const c=window.__muselab_cm,s=c.getScrollInfo();c.scrollTo(null,(s.height-s.clientHeight)*0.8)")
    page.locator('.editor-view-switch button').nth(1).click()
    page.wait_for_function("!" + APP + ".editorPreviewBusy")
    page.wait_for_function("() => {const c=window.__muselab_cm.getScrollInfo(),p=document.querySelector('.editor-live-preview');return Math.abs(c.top/(c.height-c.clientHeight)-p.scrollTop/(p.scrollHeight-p.clientHeight))<0.025}")
