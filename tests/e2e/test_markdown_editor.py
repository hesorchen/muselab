"""Actual editor input, save continuity, lazy loading and bounded live preview."""
from __future__ import annotations

import time

import pytest

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
      const p=app.saveEdit(); await app.saveEdit();
      cm.setCursor({line:0,ch:4}); cm.replaceSelection('后续输入');
      finish(); await p;
      const newer={dirty:app._editorDirty(),editing:app.editing,body:cm.getValue(),saved:app.rawText};
      cm.undo(); const cleanAfterUndo=!app._editorDirty();
      window.fetch=(url,opts)=>String(url).includes('/api/files/write')
        ? Promise.resolve(new Response('write failed',{status:500})) : fetchOriginal(url,opts);
      cm.replaceSelection('未保存'); await app.saveEdit();
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
