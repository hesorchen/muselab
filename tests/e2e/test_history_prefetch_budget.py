"""Speculative history loads share one slot while foreground loads stay free."""
from .test_chat_render_perf import _app_eval, _login


def test_hover_and_idle_preloads_share_one_slot(page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    result = _app_eval(page, """
        const saved = {load: app._ensureSessionLoaded, tabs: app.workspaceOpenTabIds,
          pane: app.activeSessionPane, prefetch: app._prefetching};
        const started = [], releases = [];
        app._prefetching = {};
        app.activeSessionPane = () => ({streaming: false});
        app.workspaceOpenTabIds = () => ['prefetch-first', 'prefetch-idle'];
        app._ensureSessionLoaded = sid => {
          started.push(sid);
          if (sid === 'foreground-click') return Promise.resolve(true);
          return new Promise(resolve => releases.push(() => {
            app._ensureTabState(sid)._loaded = true;
            resolve(true);
          }));
        };
        try {
          app.prefetchSession('prefetch-first');
          await new Promise(resolve => setTimeout(resolve, 360));
          app._idlePreloadStep();
          app.prefetchSession('prefetch-hover');
          await new Promise(resolve => setTimeout(resolve, 360));
          const background = [...started];
          const foreground = await app._ensureSessionLoaded('foreground-click');
          return {background, foreground, started};
        } finally {
          releases.forEach(release => release());
          await new Promise(resolve => setTimeout(resolve, 0));
          clearTimeout(app._prefetchTimer);
          app._ensureSessionLoaded = saved.load;
          app.workspaceOpenTabIds = saved.tabs;
          app.activeSessionPane = saved.pane;
          app._prefetching = saved.prefetch;
          ['prefetch-first','prefetch-idle','prefetch-hover'].forEach(sid => delete app.tabState[sid]);
        }
    """)
    assert result['background'] == ['prefetch-first']
    assert result['foreground'] is True
    assert result['started'] == ['prefetch-first', 'foreground-click']
