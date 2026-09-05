"""Visible feedback regressions: direct sends are not queued; settings stay readable."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect
from .test_chat_render_perf import _app_eval, _login


@pytest.mark.parametrize("width", [1440, 390])
def test_settings_loading_text_is_inside_scroll_clip(page, backend_url, auth_token, width):
    _login(page, backend_url, auth_token)
    page.set_viewport_size({"width": width, "height": 844})
    _app_eval(page, """
      app.setLang('zh');
      window.__settingsReadOriginal=app._settingsRead;
      window.__settingsFixture=await app._settingsRead('/api/settings');
      app._settingsRead=()=>new Promise(resolve=>window.__finishSettingsRead=resolve);
      window.__settingsOpen=app.openSettings('general');
    """)
    loading = page.locator('.settings-content > [role="status"]').first
    expect(loading).to_be_visible()
    geometry = loading.evaluate("""el=>{
      const range=document.createRange();range.selectNodeContents(el);
      const text=range.getBoundingClientRect(),pane=el.parentElement.getBoundingClientRect();
      return {textTop:text.top,paneTop:pane.top,textHeight:text.height};
    }""")
    _app_eval(page, """
      window.__finishSettingsRead(window.__settingsFixture);
      await window.__settingsOpen; app._settingsRead=window.__settingsReadOriginal;
    """)
    assert geometry["textHeight"] > 0
    assert geometry["textTop"] >= geometry["paneTop"], geometry


def test_settings_timeout_is_explained_and_retry_recovers(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
      app.setLang('zh');
      const original=window.fetch;
      const oldTimeout=app.SETTINGS_READ_TIMEOUT_MS;
      app.SETTINGS_READ_TIMEOUT_MS=30;
      window.fetch=(url,options)=>{
        if(String(url)==='/api/settings')return new Promise((resolve,reject)=>{
          options.signal.addEventListener('abort',()=>{
            reject(new DOMException('signal is aborted without reason','AbortError'));
          },{once:true});
        });
        return original(url,options);
      };
      try {
        await app.openSettings('general');
        const error=app.settings.error, loading=app.settings.loading;
        window.fetch=original;
        await app.openSettings('general');
        return {error,loading,recovered:!app.settings.error&&!app.settings.loading};
      } finally {window.fetch=original;app.SETTINGS_READ_TIMEOUT_MS=oldTimeout;}
    """)
    assert "超时" in result["error"], result
    assert "abort" not in result["error"].lower()
    assert not result["loading"] and result["recovered"]
