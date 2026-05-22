// ==========================================================================
// Global error capture — runs before anything else so we catch errors that
// happen during boot too (Alpine x-init, vendor scripts, etc).
//
// Why this exists: errors thrown inside Alpine expressions are re-thrown
// through alpine.dev.js:443 (handleError → setTimeout(throw)) which masks
// the real call site. Native fetch failures in Safari surface as the
// opaque "TypeError: Load failed" with no stack pointing at OUR code.
// Catching at window-level gives us the original reason + stack + the
// request URL (when it's a TypeError from fetch we can grab the last
// in-flight URL via the patched fetch below).
//
// Errors are mirrored to:
//   1. console.error (for desktop devtools)
//   2. window.__museErrors__ ring buffer (for "paste this array" diagnosis)
//   3. POST /api/log/client-error (for iOS Safari / no-devtools cases —
//      lands in uvicorn stderr / systemd journal / docker logs)
//
// Dedup: same (message + filename + lineno) within 10s is dropped, so a
// loop that throws every frame doesn't DoS the log endpoint.
// ==========================================================================
(function installErrorCapture() {
  if (typeof window === "undefined") return;
  if (window.__museErrorCaptureInstalled) return;
  window.__museErrorCaptureInstalled = true;

  const RING_MAX = 50;
  const DEDUP_WINDOW_MS = 10_000;
  const ring = window.__museErrors__ = [];
  const seen = new Map(); // sig -> last-ts

  // Last fetch URL/method, captured by the wrapper below. Safari's
  // "TypeError: Load failed" has no info about which request died;
  // pairing the error with the most recent fetch is a strong hint.
  window.__museLastFetch__ = null;

  function _sig(rec) {
    return [rec.kind, rec.message || "", rec.filename || "", rec.lineno || ""].join("|");
  }

  function _report(rec) {
    const sig = _sig(rec);
    const now = Date.now();
    const last = seen.get(sig) || 0;
    if (now - last < DEDUP_WINDOW_MS) return;
    seen.set(sig, now);

    rec.ts = new Date(now).toISOString();
    rec.ua = navigator.userAgent;
    rec.url = location.href;
    rec.lastFetch = window.__museLastFetch__;

    ring.push(rec);
    if (ring.length > RING_MAX) ring.shift();

    try { console.error("[muse-capture]", rec); } catch (_) { /* noop */ }

    // sendBeacon is fire-and-forget, survives page-unload, and Safari
    // supports it. Falls back to fetch+keepalive if sendBeacon refuses
    // the blob (rare; some Safari versions reject non-form blobs).
    try {
      const body = JSON.stringify(rec);
      const ok = navigator.sendBeacon &&
                 navigator.sendBeacon("/api/log/client-error",
                                      new Blob([body], { type: "application/json" }));
      if (!ok) {
        fetch("/api/log/client-error", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body, keepalive: true,
        }).catch(() => { /* swallow — don't recurse into the handler */ });
      }
    } catch (_) { /* never let logging break the app */ }
  }

  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    _report({
      kind: "unhandledrejection",
      message: (r && (r.message || String(r))) || "(no reason)",
      name: r && r.name,
      stack: r && r.stack,
      filename: "", lineno: 0, colno: 0,
    });
  });

  window.addEventListener("error", (ev) => {
    // ev.error is null for cross-origin script errors and for resource-
    // load failures (<img> / <script> 404). For resource failures we
    // still want a record — ev.target gives the element.
    if (!ev.error && ev.target && ev.target !== window) {
      const t = ev.target;
      _report({
        kind: "resource",
        message: "resource load failed",
        tagName: t.tagName,
        src: t.src || t.href || "",
        filename: "", lineno: 0, colno: 0,
      });
      return;
    }
    const err = ev.error;
    _report({
      kind: "error",
      message: ev.message || (err && err.message) || "(no message)",
      name: err && err.name,
      stack: err && err.stack,
      filename: ev.filename || "",
      lineno: ev.lineno || 0,
      colno: ev.colno || 0,
    });
  }, true /* capture phase — catches <script> load failures too */);

  // Wrap fetch to remember the last URL/method. When the unhandled
  // rejection later fires "Load failed" with no body, we can correlate.
  // Keep the wrapper minimal — no retries, no body capture, just a tag.
  const _origFetch = window.fetch;
  if (typeof _origFetch === "function") {
    window.fetch = function (input, init) {
      try {
        const url = (typeof input === "string") ? input
                  : (input && input.url) ? input.url : String(input);
        const method = (init && init.method) || (input && input.method) || "GET";
        window.__museLastFetch__ = { url, method, t: Date.now() };
      } catch (_) { /* noop */ }
      return _origFetch.apply(this, arguments);
    };
  }
})();


// ==========================================================================
// i18n — dictionary is loaded by /static/i18n/index.js (plain <script> tag in
// index.html, before app.js). Kept out of this file so the file stays focused
// on app logic and editing translations doesn't require diffing 470 lines.
// Falls back to an empty dict if the i18n script failed to load — t() then
// returns the key, making the breakage visible instead of silently broken.
// ==========================================================================
const STRINGS = (typeof window !== "undefined" && window.MUSELAB_STRINGS)
                  || { zh: {}, en: {} };


// Static UI data lives in /static/data/constants.js (loaded by index.html
// before this file). These aliases keep the existing references in this file
// working without code changes elsewhere.
const ACCENT_PRESETS = window.MUSELAB_ACCENT_PRESETS || [];
const EDITABLE_EXT = window.MUSELAB_EDITABLE_EXT || new Set();

function portal() {
  return {
    // ===== auth =====
    authed: false, tokenInput: "", token: "", loginErr: "",
    // App-readiness layers:
    //   appReady=false → full-screen splash (initial load / hard refresh).
    //     Cleared once contextInfo + sessions list have arrived OR after
    //     a hard 8s timeout (avoid splashing forever on backend dead).
    //   connState: 'ok' | 'reconnecting' | 'reconnected'
    //     drives the slim top banner shown when fetches start failing
    //     (backend restarted, network blipped). 'reconnected' briefly
    //     flashes green then auto-clears.
    appReady: false,
    splashHint: "",          // "still warming up..." appears after 3s
    connState: "ok",
    _connFails: 0,
    _connHeartbeat: null,
    _splashHintTimer: null,
    _splashHardTimeout: null,

    // True on mouse/trackpad devices, false on touch. Used to gate
    // HTML5 draggable=true on chat tabs: on iOS/Android, draggable
    // elements eat the first tap (treats it as a drag-prep gesture),
    // forcing the user to tap twice just to switch tabs.
    isPointerDevice: typeof window !== "undefined"
                       && window.matchMedia
                       && window.matchMedia("(hover: hover)").matches,

    // ===== file tree =====
    visible: [], expanded: new Set(), childCache: {},
    selected: "",
    dragOver: "",
    searchQ: "", searchMode: false, searching: false,
    searchHits: [], searchTruncated: false,
    grepHits: [], grepTruncated: false,

    // ===== preview =====
    // Drag-and-drop visual state for the preview pane.
    previewDragHover: false,
    // Document-level "OS file drag in progress" flag. Set true when the
    // user starts dragging files from Finder / Files / Explorer over
    // any part of the page; reset on drop or when the drag leaves the
    // window. Drives the preview-pane's drop overlay so it can intercept
    // the drop even when an HTML preview iframe or an image element is
    // covering the preview-body (those swallow dragover events from
    // their parent — without a global flag the overlay never appears
    // and the drop falls through to the browser, which opens the file
    // as a navigation away from muselab). See init() for the listeners.
    osFileDragging: false,
    _dragCounter: 0,
    // Right-click context menu on a preview tab. { path, x, y } when open.
    previewTabCtxMenu: null,
    // Image lightbox (click chat-bubble image to enlarge).
    lightbox: { show: false, src: "" },
    // Whether the "Open files" sidebar section is collapsed. Persists to
    // localStorage via savePrefs/loadPrefs so the user's preference sticks.
    openFilesCollapsed: false,
    // User-override height (px). null = auto: 1 file → 1 row, 2 → 2, ...,
    // capped at 5 rows + header. Once the user drags the splitter we set a
    // concrete pixel value and stop auto-fitting.
    openFilesHeight: null,
    previewMode: "", rawText: "", renderedMd: "", previewLang: "plaintext",
    // xlsx preview state. previewMode==='xlsx' uses xlsxSheets (array of
    // {name, rows, rows_truncated, cols_truncated}). xlsxActive picks the
    // sheet tab. xlsxLimits carries the server's row/col caps for the UI
    // hint when truncation happens.
    xlsxSheets: [], xlsxActive: "", xlsxLimits: null, xlsxSheetsTruncated: false,
    // CSV preview — paginated, one window at a time. csvData holds the
    // backend response for the current page; csvOffset advances by limit
    // when the user pages forward.
    csvPath: "", csvData: null, csvOffset: 0, csvLimit: 200, csvLoading: false,
    // Bumped whenever an assistant tool_use edits a file. Used as a cache
    // buster on iframe / read URLs so the preview reflects the new content
    // without the user needing to manually refresh the page.
    previewVersion: 0,
    // Compact orchestration: _compacting marks the window where the CLI is
    // busy summarising history; user messages typed during that window queue
    // up and dispatch when compact finishes.
    _compacting: false,
    _compactQueue: [],
    // SDK get_context_usage() breakdown popup. Shows per-category token
    // counts (system prompt / tools / memory files / messages / mcp / skills)
    // so the user can see which slice is using their context window.
    ctxBreakdown: { show: false, loading: false, data: null, error: "" },
    // Per-category expansion state inside the breakdown popup. Keyed by
    // category name from SDK; only categories that map to a sub-list
    // (memoryFiles / mcpTools / agents) actually expand.
    ctxExpanded: {},
    editing: false, editText: "",
    cmStatus: { line: 1, col: 1, sel: 0, lines: 0, chars: 0, mode: "plaintext", dirty: false },
    tabs: [],   // open file tabs: [{path, name}]

    // ===== chat =====
    sessions: [], currentId: "",
    // True while loadSession(currentId) is in flight. UI uses this to swap
    // the brand-empty placeholder for a shimmer skeleton, so users don't
    // see "Muse · Calliope / empty chat" for the second a big session
    // takes to fetch.
    messagesLoading: false,

    // ===== scheduled tasks (bell drawer) =====
    // Daily-fire prompts that dispatch into a dedicated muselab session.
    // The bell icon in the top bar shows unread_count from the server;
    // opening the drawer ack-zeros it. Drafts and tasks both live here
    // so the modal stays self-contained.
    scheduler: {
      show: false,
      tasks: [],
      history: [],
      unreadCount: 0,
      loading: false,
      // Inline-create form state. Polymorphic — only the fields
      // matching `kind` get sent to the backend. Reset by _resetSchedDraft.
      // editingId: when non-null, saveSchedTask() PATCHes that task
      // instead of POSTing a new one. The same form serves both flows.
      draft: {
        editingId: null,
        name: "", prompt: "", model: "",
        kind: "daily",         // daily / weekly / monthly / once
        hour: 9, minute: 0,
        weekdays: [1, 2, 3, 4, 5],  // weekly: Mon-Fri default
        day: 1,                // monthly day-of-month
        onceDate: "",          // once: "YYYY-MM-DD"
      },
    },
    // Per-task "run-now" inflight flag — disables retry / send buttons
    // until the LLM call returns and history is reloaded. Keyed by task id.
    schedRunning: {},
    // Notification prefs — purely client-side. Vibration is a foreground-
    // only nicety (when muselab is open, vibrate on unread count tick up).
    // pushEnabled is a UI hint mirroring the actual SW push subscription
    // state; the subscribe flow itself lives in pushSubscribe().
    notifyPrefs: { vibrate: false, push: false },
    // Last-seen unread, used to detect a tick-up and trigger vibration.
    _lastSeenUnread: 0,

    // ===== command palette (Cmd/Ctrl+K) =====
    // Single fuzzy-search dropdown across: quick actions, open sessions,
    // and any file under the archive root (via /api/files/search). Action
    // items have a `run` closure; selecting an item fires it and closes
    // the palette.
    palette: {
      show: false,
      query: "",
      activeIndex: 0,
      fileResults: [],   // populated by _fetchPaletteFiles() (server-side)
      fileQuery: "",     // last query the server fetch ran for
      fileLoading: false,
      // Cross-session full-text message search — populated by
      // _fetchPaletteMessages() against /api/chat/search. Lets the
      // palette double as a "find anything I ever said" jump tool.
      messageResults: [],
      messageQuery: "",
      messageLoading: false,
    },
    // Tab strip — VS Code-Claude style. `openTabIds` is the visible order; each
    // entry is a session id (also present in `sessions`). currentId is the
    // active tab. Tabs can be opened from the session picker, closed via × on
    // the tab, or created by the "+ new" button.
    openTabIds: [],
    renamingTabId: "",   // session id whose name is currently being inline-edited
    renameDraft: "",     // current value of the inline rename <input>
    tabCtxMenu: null,    // {id, x, y} for the right-click tab menu, or null
                          // (kept separate from the file-tree's `ctxMenu` which
                          //  has a different shape — overlapping names crash
                          //  Alpine when one side reads .show on the other's null)
    // Per-tab runtime state. Keyed by session id. Each entry is a "snapshot"
    // of {messages, sessionUsage, streaming, es, streamingModel, ...} that the
    // active tab mirrors into root state (this.messages, this.es, etc.) via
    // _activateTabState(). Background tabs' stream callbacks write to their
    // own tabState[sid] so switching away doesn't lose / mis-route events.
    // The active tab's `this.messages` and `tabState[currentId].messages` are
    // the SAME array reference — we mutate in place, never replace.
    tabState: {},
    messages: [],
    model: "claude-sonnet-4-6",
    permission: "bypassPermissions",
    // Reasoning effort override for the current session — "" means let the
    // SDK pick adaptively (the existing default). Persisted on the session
    // via PATCH so each tab keeps its own setting across reloads.
    effort: "",
    // Always render thinking blocks. Toggle removed 2026-05-19 — adaptive
    // thinking was causing invisible mid-reply stalls when hidden; now we
    // always enable thinking on the backend AND always display it.
    // showThinking removed 2026-05-20 — thinking display is unconditional
    // now (see comment in chat.py near ThinkingConfigEnabled). The state
    // hung around forwarded to the backend purely for API compat; muselab
    // is the only client, so the dead duo got cleared.
    input: "", streaming: false, es: null,
    // 锁定当前在跑的那条请求用的模型——dropdown 切到别的，pending bubble 不能跟着变。
    streamingModel: "",
    // Elapsed seconds since send() — pending bubble shows it after 1s so the
    // user knows the system isn't stuck.
    streamElapsed: 0,
    _streamTimer: null,
    _streamStartedAt: 0,
    pendingImages: [],    // [{id, mime, preview (data URL), uploading, error}]
    pendingDocs: [],      // [{id, name, kind: 'pdf'|'text', uploading, error}]
    // Flipped true inside sendMessage when the user clicks send while an
    // attachment upload is still in flight. Disables the send button so a
    // double-click can't enqueue two sends. Auto-resets when the wait
    // resolves (usually < 1 s; 30 s hard timeout).
    _sendWaitingForUpload: false,
    dragHover: false,

    // What Muse can see — populated from /api/chat/context-info on login.
    // Drives the onboarding hints (claude_md chip, "drop a doc here" cards).
    contextInfo: {
      archive_root: "",
      claude_md_exists: false,
      claude_md_lines: 0,
      claude_md_mtime: 0,
      archive_empty: true,
      subdir_present: {},
      has_claude_oauth: false,
      has_anthropic_api: false,
      third_party_configured: [],
      has_any_provider: false,
      // Guard so onboarding chips ("⚠ 未配档案" / "no provider") don't
      // flash to the user while the first contextInfo fetch is still in
      // flight. UI conditions check `_fetched && !X` rather than `!X`.
      _fetched: false,
    },

    // ===== slash commands =====
    slashShow: false,
    slashIdx: 0,
    slashAnchor: -1,      // input position where the leading '/' is
    // Defined in /static/data/constants.js (window.MUSELAB_SLASH_CMDS). Kept
    // as a component property so `this.SLASH_CMDS` references throughout the
    // file keep working without changes.
    SLASH_CMDS: window.MUSELAB_SLASH_CMDS || [],
    // Per-session context meter snapshot, updated on every SSE `done` event
    sessionUsage: { input_tokens: 0, output_tokens: 0,
                     cache_read_tokens: 0, cache_creation_tokens: 0,
                     context_limit: 0, context_used: 0, context_used_pct: 0 },
    stats: { total_cost_usd: 0, total_messages: 0, total_input_tokens: 0,
              total_output_tokens: 0, total_cache_read_tokens: 0,
              total_cache_creation_tokens: 0, cache_hit_pct: 0,
              budget_usd: 0, budget_used_pct: 0 },
    mcp: { configured: false, servers: [] },
    availableModels: [],   // from /api/chat/providers
    atBottom: true,
    theme: "dark",
    accent: "#6093ff",
    ACCENT_PRESETS,

    // ===== i18n =====
    lang: "zh",
    STRINGS,

    // ===== Muse mascot =====
    // 九缪斯（Nine Muses of Greek mythology）。视觉仍是抽象几何，名字承载典故：
    // 每个缪斯对应一种艺术 / 学科，几何形象选有意义关联的（如 Urania 天文 → orbit 行星）。
    MASCOTS: [
      { id: "hex",      greek: "Calliope",    zhName: "卡利俄佩",       domain: { zh: "史诗", en: "Epic poetry" } },
      { id: "bars",     greek: "Clio",        zhName: "克利俄",         domain: { zh: "历史", en: "History" } },
      { id: "lens",     greek: "Erato",       zhName: "厄拉托",         domain: { zh: "情诗", en: "Love poetry" } },
      { id: "wave",     greek: "Euterpe",     zhName: "欧忒耳佩",       domain: { zh: "音乐", en: "Music" } },
      { id: "crescent", greek: "Melpomene",   zhName: "墨尔波墨涅",     domain: { zh: "悲剧", en: "Tragedy" } },
      { id: "halo",     greek: "Polyhymnia",  zhName: "波吕许谟尼亚",   domain: { zh: "圣诗", en: "Sacred hymns" } },
      { id: "trio",     greek: "Terpsichore", zhName: "忒耳普西科瑞",   domain: { zh: "舞蹈", en: "Dance" } },
      { id: "spark",    greek: "Thalia",      zhName: "塔利亚",         domain: { zh: "喜剧", en: "Comedy" } },
      { id: "orbit",    greek: "Urania",      zhName: "乌拉尼亚",       domain: { zh: "天文", en: "Astronomy" } },
    ],
    mascotIdx: 0,
    mascotGreet: false,

    leftOpen: true,
    rightOpen: true,
    leftWidth: 280,
    rightWidth: 440,
    showHidden: false,

    // Mobile: viewport < 900px collapses the 3 panes into a single visible
    // tab. Default "chat" since that's the primary action; auto-switches to
    // "preview" when user opens a file, and "chat" when they @-mention one.
    mobileTab: "chat",
    // Mobile-only: when user scrolls down in preview, hide top header + tab
    // bar + bottom-nav for immersive reading. onPreviewScroll updates this
    // based on direction + threshold. Reset on mobileTab change away from
    // preview (see init() $watch).
    previewImmersive: false,
    _lastPreviewScrollTop: 0,
    // Sidebar "message outline" — opens a collapsible list of user prompts
    // in the current session so you can jump back to a question in a long
    // conversation. Toggled by the outline button in the files pane header.
    msgOutlineOpen: false,

    // ===== @ mention =====
    mentionShow: false, mentionResults: [], mentionIdx: 0, mentionAnchor: -1,

    // ===== toast / modal / ctx menu =====
    toasts: [], _toastId: 0,
    modal: { show: false, title: "", body: "", input: null, confirm: null, cancel: null, okText: "", cancelText: "", danger: false },
    ctxMenu: { show: false, x: 0, y: 0, node: null },

    // ===== settings =====
    // Keyboard cheat-sheet modal — toggled by `?` keypress outside any
    // input. Discoverability tool: muselab has 10+ shortcuts and no one
    // reads the README.
    cheatSheet: { show: false },

    settings: {
      show: false,
      providers: [],
      draftKeys: {},
      draftDefaults: { model: "", permission: "" },
      draftParams: { thinking_budget: 4000, max_turns: 0,
                       notify_scheduled: true, notify_normal: true },
      // MCP server list (loaded from /api/settings/mcp)
      mcpServers: [],
      mcpExamples: [],
      mcpDraft: { show: false, name: "", command: "", argsStr: "" },
      skills: [],         // discovered skill list (read-only browse)
      skillFilter: "",    // free-text filter (name / description / source)
      probeResults: {},   // env_key -> {ok, text} from last "Test" click
      // Versions + upgrade — populated by loadVersions(), set by runUpgrade()
      versions: null,
      versionsLoading: false,
      upgradeRunning: false,
      upgradeResult: null,
      // Mobile-only: iOS-style 2-level menu state. null = top-level
      // menu list shown; "lang" / "provider" / ... = that section's
      // detail page shown. Desktop ignores this entirely (every
      // section is always rendered, the menu list is CSS-hidden).
      activePage: null,
    },
    // Cost dashboard state — lazily loaded when the user opens the
    // Settings → Cost section. Lives outside `settings` so it survives
    // settings modal close/open without refetching unless user clicks
    // refresh.
    cost: {
      loading: false,
      data: null,        // null = never loaded; object = last response
      windowDays: 30,    // 7 / 14 / 30 / 60 / 90, picked from dropdown
    },

    // Reactive viewport flag — used by Settings mobile menu to decide
    // whether to honor activePage (mobile) or render everything
    // (desktop). matchMedia listener in init() flips this on rotate /
    // resize so the same DOM works for both modes without reload.
    isWideScreen: typeof window !== "undefined"
                    && window.matchMedia
                    && window.matchMedia("(min-width: 721px)").matches,

    // Session picker dropdown open state (replaces native <select> so each
    // row can have an inline delete button).
    sessionPickerOpen: false,
    // Inline rename inside a picker row. Keeps the keyboard popup tied to
    // the original user click (iOS Safari requires synchronous focus()).
    renamingPickerSid: "",
    pickerRenameDraft: "",

    // Per-provider help hints rendered under the API-key input. Anthropic
    // gets the most because it has two valid paths (Pro OAuth or API key);
    // others are just a link to where to get the key.
    PROVIDER_HELP: {
      ANTHROPIC_API_KEY: {
        url: "https://console.anthropic.com/settings/keys",
        zh: "两种路径：(1) Pro/Max 订阅 → 终端跑 `claude login`（免费配额，无需在此填）；(2) API 按量付费 → 去 console.anthropic.com 拿 key 填这里。两个都配 → CLI 自动用 Pro，不会重复扣费。",
        en: "Two paths: (1) Pro/Max subscription → run `claude login` in terminal (free quota, leave this blank); (2) pay-per-use API → grab a key from console.anthropic.com and paste it here. With both configured, CLI prefers Pro automatically — you won't double-bill.",
      },
      DEEPSEEK_API_KEY: {
        url: "https://platform.deepseek.com/api_keys",
        zh: "去 platform.deepseek.com 控制台创建 API key（注册送 5 元额度）。",
        en: "Create an API key at platform.deepseek.com (free trial credit on signup).",
      },
      ZHIPUAI_API_KEY: {
        url: "https://open.bigmodel.cn/usercenter/apikeys",
        zh: "去 open.bigmodel.cn 控制台创建 API key。注意是国内站，不是 zhipuai.com.cn。",
        en: "Create an API key at open.bigmodel.cn (China mainland site).",
      },
      MINIMAX_API_KEY: {
        url: "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        zh: "去 platform.minimaxi.com（国内站）创建 API key。注意是 minimaxi.com 不是 minimax.io（后者是海外站，用同 key 401）。",
        en: "Create an API key at platform.minimaxi.com (the .com - .io is overseas and rejects the same key).",
      },
    },

    _pendingExpanded: null,

    // ===== init =====
    onGlobalKeyDown(ev) {
      // ---- Command palette ----
      // Cmd/Ctrl+K from anywhere opens it. While open, palette owns
      // ↑/↓/Enter; everything else falls through to the input.
      if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        this.openPalette();
        return;
      }
      if (this.palette.show) {
        if (ev.key === "ArrowDown") { ev.preventDefault(); this.paletteMove(1); return; }
        if (ev.key === "ArrowUp")   { ev.preventDefault(); this.paletteMove(-1); return; }
        if (ev.key === "Enter")     { ev.preventDefault(); this.onPaletteEnter(); return; }
        if (ev.key === "Escape")    { ev.preventDefault(); this.closePalette(); return; }
        // Don't return — let typed chars reach the palette input naturally.
      }
      // Ctrl/Cmd+S → 保存（编辑模式下）；Esc → 关 modal/menu/停止流式
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
        if (this.editing && this.selected) {
          ev.preventDefault();
          this.saveEdit();
        }
        return;
      }
      // Chat-tab keybindings (Ctrl/Cmd as the modifier; Mac users get Cmd):
      //   Ctrl+T          new chat tab
      //   Ctrl+W          close current chat tab
      //   Ctrl+Tab        next tab    (Shift+Tab = previous)
      //   Ctrl+1..9       jump to Nth tab
      // We hijack Ctrl+T and Ctrl+W from the browser. The user is inside a
      // single-page web app — we own these. (Mobile Safari ignores them.)
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey) {
        if (ev.key === "t" || ev.key === "T") {
          ev.preventDefault();
          this.newSession();
          return;
        }
        if (ev.key === "w" || ev.key === "W") {
          if (this.currentId) {
            ev.preventDefault();
            this.closeChatTab(this.currentId);
          }
          return;
        }
        if (ev.key === "Tab") {
          if (!this.openTabIds.length) return;
          ev.preventDefault();
          const cur = Math.max(0, this.openTabIds.indexOf(this.currentId));
          const next = ev.shiftKey
            ? (cur - 1 + this.openTabIds.length) % this.openTabIds.length
            : (cur + 1) % this.openTabIds.length;
          this.activateTab(this.openTabIds[next]);
          return;
        }
        // Ctrl+1..9
        if (/^[1-9]$/.test(ev.key)) {
          const i = parseInt(ev.key, 10) - 1;
          if (i < this.openTabIds.length) {
            ev.preventDefault();
            this.activateTab(this.openTabIds[i]);
          }
          return;
        }
      }
      if (ev.key === "Escape") {
        if (this.cheatSheet.show) { this.cheatSheet.show = false; return; }
        if (this.mentionShow) { this.mentionShow = false; return; }
        if (this.ctxMenu.show) { this.ctxMenu.show = false; return; }
        if (this.tabCtxMenu) { this.closeTabMenu(); return; }
        if (this.settings.show) { this.settings.show = false; return; }
        if (this.modal.show && this.modal.cancel) { this.modal.cancel(); return; }
        if (this.editing) { this.editing = false; return; }   // 退出编辑
        if (this.streaming) { this.stop(); return; }          // 停止流式
      }
      // `?` shows keyboard cheat-sheet — only when nothing has focus
      // (we don't want to swallow it inside the chat textarea or any
      // settings input). Don't fire on modifiers; Shift+/ alone = `?`.
      if (ev.key === "?" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        const ae = document.activeElement;
        const tag = ae && ae.tagName;
        const inField = tag === "INPUT" || tag === "TEXTAREA"
                        || (ae && ae.isContentEditable);
        if (!inField) {
          ev.preventDefault();
          this.cheatSheet.show = true;
        }
      }
    },

    init() {
      // 全局快捷键（绑在 document，避免每个 textarea 单独处理）
      document.addEventListener("keydown", e => this.onGlobalKeyDown(e));
      // 一次性迁移旧 localStorage key（portal_* → muselab_*），让现有用户无感升级
      for (const [oldK, newK] of [
        ["portal_token", "muselab_token"],
        ["portal_prefs", "muselab_prefs"],
        ["portal_theme", "muselab_theme"],
        ["portal_chat", "muselab_chat"],
      ]) {
        const v = localStorage.getItem(oldK);
        if (v != null && localStorage.getItem(newK) == null) {
          localStorage.setItem(newK, v);
        }
        localStorage.removeItem(oldK);
      }
      this.initTheme();
      this.initLang();
      this.initMascot();
      this.configureMarked();
      this._initMobileKeyboardWatch();
      // PTR (pull-to-refresh) helper exposed for x-init use on scroll
      // containers. Mobile only — no-op on devices without touch.
      // ============================================================
      // Auto-scroll the chat-tabs strip to the active tab whenever
      // currentId changes — covers newSession, openTab from history
      // picker, slash /resume, etc. without requiring each entry point
      // to remember to call _scrollTabIntoView.
      this.$watch("currentId", (tid) => {
        if (!tid) return;
        this.$nextTick(() => this._scrollTabIntoView(tid));
      });
      // Leaving preview tab on mobile cancels immersive mode so the next
      // tab is rendered with its bars visible. Also persists the choice so
      // closing + reopening the PWA lands the user back on the same tab —
      // not always "preview" (which is what the previewSelected restore
      // would otherwise force).
      this.$watch("mobileTab", (t) => {
        if (t !== "preview" && this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this.savePrefs();
      });

      // ============================================================
      // Document-level OS-file-drag detection
      // ------------------------------------------------------------
      // Tracks whether the user is currently dragging a file from the
      // OS into the muselab window. Drives `osFileDragging` so the
      // preview pane's drop overlay can render with pointer-events:auto
      // and intercept the drop even when an HTML preview iframe is
      // covering the preview-body. (Iframes / cross-origin embeds
      // swallow drag events; without this overlay-on-top approach the
      // drop falls through to the browser default — "open this file"
      // — and navigates away from muselab.)
      //
      // dragenter/dragleave fire many times per drag (once per child
      // element entered) so we count depth instead of trusting a single
      // event. dragend on the source doesn't fire for OS drags (the
      // source is in a different process), so we reset on drop too.
      //
      // The `types.includes('Files')` check filters OUT internal drags
      // (tree reorder, tab reorder) which use 'text/plain' / custom
      // mime types — those drags don't carry real File objects and
      // shouldn't make the upload overlay flash.
      // ============================================================
      const _hasFileType = (dt) => {
        if (!dt || !dt.types) return false;
        // DataTransferItemList is iterable in modern browsers; older
        // Safari returned a plain Array-like with .length only.
        try {
          for (const t of dt.types) if (t === "Files") return true;
        } catch (_e) {
          for (let i = 0; i < dt.types.length; i++) {
            if (dt.types[i] === "Files") return true;
          }
        }
        return false;
      };
      document.addEventListener("dragenter", (e) => {
        if (!_hasFileType(e.dataTransfer)) return;
        this._dragCounter++;
        if (this._dragCounter === 1) this.osFileDragging = true;
      });
      document.addEventListener("dragleave", (e) => {
        // Some browsers (Firefox) don't expose types on dragleave; we
        // decrement unconditionally because every leave matches an
        // earlier enter and the counter floor at 0 prevents drift.
        if (this._dragCounter > 0) this._dragCounter--;
        if (this._dragCounter === 0) this.osFileDragging = false;
      });
      // Required for drop to fire: dragover MUST be preventDefault'd at
      // some level. The preview overlay does this when visible, but for
      // areas of the page that aren't drop targets we also need a
      // no-op handler — otherwise the browser's default "open file as
      // navigation" kicks in if the user releases over chrome (toolbar
      // / sidebar). Without this every aborted drag could navigate away.
      document.addEventListener("dragover", (e) => {
        if (!_hasFileType(e.dataTransfer)) return;
        e.preventDefault();
      });
      document.addEventListener("drop", (e) => {
        // If the drop wasn't handled by an explicit zone (preview /
        // chat input), suppress browser default and reset state.
        if (_hasFileType(e.dataTransfer)) e.preventDefault();
        this._dragCounter = 0;
        this.osFileDragging = false;
      });

      // Listen for SW → page messages. The service worker posts
      // `muselab/notification-clicked` when the user taps a push
      // banner; we ack the unread badge immediately so they don't
      // have to open the bell drawer to clear it.
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.addEventListener("message", (ev) => {
          const t = ev && ev.data && ev.data.type;
          if (t === "muselab/notification-clicked") {
            if (this.scheduler && this.scheduler.unreadCount > 0) {
              this.ackSchedulerUnread();
            }
          }
        });
      }
      // Keep isWideScreen reactive across rotate / window resize so
      // Settings's mobile 2-level menu logic works without a reload.
      if (window.matchMedia) {
        const mq = window.matchMedia("(min-width: 721px)");
        const handler = e => { this.isWideScreen = e.matches; };
        // addEventListener is the modern API; older Safari needs
        // addListener (deprecated but still supported). Try both.
        if (mq.addEventListener) mq.addEventListener("change", handler);
        else if (mq.addListener) mq.addListener(handler);
      }
      // Per-session-load seed so the inspire prompts feel fresh each
      // time the user lands on the empty chat screen, rather than
      // always showing the same first 5. shuffleInspirePrompts() bumps
      // this on demand for "give me another batch".
      this._inspireSeed = Math.floor(Math.random() * 1e9);
      // Welcome-card visibility — Alpine-reactive so dismissWelcome()
      // immediately re-renders the chat-body. localStorage flag persists
      // dismissal across reloads / PWA reopens.
      this._welcomeDismissed = localStorage.getItem("muselab_welcome_dismissed") === "1";
      // Vibration / push prefs come from localStorage (per-device) so a
      // shared muselab between a desktop + phone keeps independent
      // settings — your phone can vibrate; the desktop tab silently
      // updates the bell badge.
      this.loadNotifyPrefs();
      // Global error capture — when alpine's "Cannot read properties of
      // undefined (reading 'after')" fires we want the FULL story (msg,
      // file, line, stack) printed in one block so the user can copy it
      // in a single paste. The minified alpine stack is useless on its
      // own; pair this with the dev (unminified) bundle for real names.
      window.addEventListener("error", (ev) => {
        if (!ev || !ev.error) return;
        const msg = ev.error.message || String(ev.error);
        if (!msg.includes("after")) return;
        console.group("%c[muselab DEBUG] alpine .after error", "color: #f87171; font-weight: bold");
        console.error("message:", msg);
        console.error("file:", ev.filename, "line:", ev.lineno, "col:", ev.colno);
        console.error("stack:", ev.error.stack);
        console.error("currentId:", this.currentId,
                      "messages.length:", (this.messages || []).length,
                      "sessions.length:", (this.sessions || []).length,
                      "previewTabCtxMenu:", JSON.stringify(this.previewTabCtxMenu),
                      "ctxBreakdown:", JSON.stringify(this.ctxBreakdown));
        console.groupEnd();
      });
      this.$watch("editing", v => v ? this.mountCM() : this.unmountCM());
      this.$watch("rightOpen", v => { if (v) this.greetMascot(this.t("toast.muse_back")); });
      // 编辑模式下切换文件时，重新挂载 CM 加载新文件内容
      this.$watch("selected", () => { if (this.editing) { this.unmountCM(); this.mountCM(); } });
      // 注意：之前这里挂过 `$watch("model", ...)` 自动 toast「模型已切」。
      // 但 dropdown 的 x-model 是 onchange 之前就把 this.model 写新值——
      // watch 会比 onModelChange() 的 confirm modal 先 fire，让用户看到"已
      // 切换"toast 之后才弹"是否新建会话？"。删掉 watch，让 onModelChange()
      // 作为唯一的视觉反馈源（成功 PATCH / 成功新建后才 toast）。
      const t = localStorage.getItem("muselab_token");
      if (t) {
        this.token = t; this.authed = true;
        this._bootApp();
      } else {
        // No token saved → skip splash, jump straight to login.
        this.appReady = true;
      }
    },

    // Mobile keyboard handling. iOS Safari (and some Android browsers) overlay
    // the virtual keyboard ABOVE the layout instead of resizing it — without
    // intervention, the chat-input + bottom tab bar end up hidden behind the
    // keyboard and the user can't tap "send". We watch visualViewport for
    // height changes and (a) flag the body so CSS can hide the bottom tab
    // bar, (b) expose --kb-inset so the chat-input can lift above the
    // keyboard. The viewport meta `interactive-widget=resizes-content` does
    // this natively on modern browsers; this is the fallback path.
    // Tap-on-chat-input handler. Two things matter:
    //   1. mark body.kb-open so CSS hides the bottom tab bar (legacy behaviour)
    //   2. scroll the textarea into view AFTER the keyboard's resize animation
    //      finishes. iOS Safari fires the visualViewport `resize` event
    //      ~250-350 ms after focus on PWA standalone; calling scrollIntoView
    //      before that resize is harmless (target is already visible per the
    //      pre-keyboard layout) but the resize then re-hides it. The deferred
    //      call lands the textarea above the keyboard reliably.
    //
    // `scroll-margin-bottom: 16px` on the textarea (see styles.css) leaves a
    // breathing-room gap so the input isn't flush against the keyboard top.
    onChatInputFocus(ev) {
      document.body.classList.add("kb-open");
      const ta = ev && ev.target;
      if (!ta || typeof ta.scrollIntoView !== "function") return;
      // Two pings: one fast (covers fast keyboards / Android), one slow
      // (waits out iOS PWA's lazy resize). Idempotent — second call is a
      // no-op if the input is already in view.
      const lift = () => {
        try { ta.scrollIntoView({ block: "end", behavior: "smooth" }); }
        catch (_) { try { ta.scrollIntoView(false); } catch (__) {} }
      };
      setTimeout(lift, 50);
      setTimeout(lift, 400);
    },

    // Triple-click (or any 3+ rapid click) on the chat input selects all
    // text. Browsers natively give us:
    //   single-click → place cursor
    //   double-click → select word
    //   triple-click → select paragraph (for <textarea> this is one line)
    // None of those select the WHOLE composed message, which is what the
    // user usually wants when re-prompting ("oh let me just retype this"
    // or "wrong tab, retry on the right model"). Listening to event.detail
    // (the consecutive-click counter that resets after ~500ms idle) is
    // the cleanest cross-platform path — no manual debounce timer state
    // to maintain, no conflict with the OS double-click word selection.
    onChatInputClick(ev) {
      const ta = ev && ev.target;
      if (!ta) return;
      // detail counts consecutive clicks: 1 / 2 / 3 / ... Browser resets
      // after a short idle window. We trigger on >= 3 so double-click's
      // word selection still works normally.
      if (ev.detail >= 3 && ta.value && ta.value.length > 0) {
        // Default for triple-click on textarea is "select current line".
        // Override with full select — preventDefault stops the partial
        // selection from racing the explicit select() call.
        ev.preventDefault();
        ta.select();
      }
    },

    _initMobileKeyboardWatch() {
      if (!window.visualViewport) return;
      const vv = window.visualViewport;
      const update = () => {
        const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        // Anything > 80px likely means the on-screen keyboard is up (small
        // values are OS-chrome / address-bar transitions, not the keyboard).
        const kbOpen = inset > 80;
        const wasOpen = document.body.classList.contains("kb-open");
        if (kbOpen) {
          document.body.classList.add("kb-open");
          document.documentElement.style.setProperty("--kb-inset", inset + "px");
        } else {
          document.body.classList.remove("kb-open");
          document.documentElement.style.setProperty("--kb-inset", "0px");
        }
        // Keyboard open/close shrinks/grows chat-body. If the user was
        // already at the bottom, the new viewport leaves the latest
        // message stranded mid-screen — re-pin to bottom. Use rAF so
        // the browser has done layout pass for the new height. We pass
        // force=false because scrollToBottom honors `atBottom`, so a
        // user mid-history won't get yanked.
        if (kbOpen !== wasOpen) {
          requestAnimationFrame(() => this.scrollToBottom(false));
        }
      };
      vv.addEventListener("resize", update);
      vv.addEventListener("scroll", update);
      update();
    },

    // Attach iOS-style pull-to-refresh to a scrollable element. Mobile
    // only — skips immediately on devices with no touch (matchMedia
    // `pointer: coarse` would also wrap iPad pencil; we gate on
    // `hover: hover` instead, which is true for mouse / trackpad).
    //
    // Usage: <ul x-init="_attachPTR($el, () => reloadX())">. The
    // helper inserts an indicator element above the scroller, listens
    // to touchstart/move/end, applies a damped translateY while the
    // user is pulling, and calls onRefresh() when released past 60px.
    // Indicator stays visible during refresh, snaps back when the
    // promise resolves (so the user sees progress).
    _attachPTR(el, onRefresh) {
      if (!el || typeof onRefresh !== "function") return;
      if (window.matchMedia && window.matchMedia("(hover: hover)").matches) return;
      // Insert indicator just above the scroller (inside the same flex
      // parent so layout doesn't shift). pointer-events:none — pulling
      // the indicator itself shouldn't intercept the user's gesture.
      const ind = document.createElement("div");
      ind.className = "ptr-indicator";
      ind.innerHTML = "<span class='ptr-icon'>↓</span><span class='ptr-text'></span>";
      const txt = ind.querySelector(".ptr-text");
      const icon = ind.querySelector(".ptr-icon");
      el.parentElement.insertBefore(ind, el);
      const THRESHOLD = 60;
      let startY = 0, currentY = 0, pulling = false, refreshing = false;
      const setLabel = (state) => {
        const zh = this.lang === "zh";
        if (state === "pull")    txt.textContent = zh ? "下拉刷新" : "Pull to refresh";
        else if (state === "release") txt.textContent = zh ? "释放刷新" : "Release to refresh";
        else if (state === "loading") txt.textContent = zh ? "刷新中…" : "Refreshing…";
      };
      el.addEventListener("touchstart", (e) => {
        if (refreshing) return;
        if (el.scrollTop > 0) return;
        startY = e.touches[0].clientY;
        currentY = startY;
        pulling = true;
      }, { passive: true });
      el.addEventListener("touchmove", (e) => {
        if (!pulling || refreshing) return;
        currentY = e.touches[0].clientY;
        const dy = currentY - startY;
        if (dy <= 0) {
          ind.style.transform = "";
          ind.style.opacity = "0";
          return;
        }
        // Prevent page-level overscroll while the user is actively
        // pulling — without this, iOS Safari bounces the whole page.
        // Only block when we're genuinely pulling (dy > a few px).
        if (dy > 4 && el.scrollTop === 0 && e.cancelable) e.preventDefault();
        const damped = Math.min(dy * 0.5, 90);
        ind.style.transform = `translateY(${damped}px)`;
        ind.style.opacity = String(Math.min(1, damped / 40));
        icon.style.transform = damped >= THRESHOLD ? "rotate(180deg)" : "";
        setLabel(damped >= THRESHOLD ? "release" : "pull");
      }, { passive: false });
      el.addEventListener("touchend", async () => {
        if (!pulling || refreshing) return;
        pulling = false;
        const dy = currentY - startY;
        if (dy * 0.5 >= THRESHOLD) {
          refreshing = true;
          ind.style.transform = `translateY(50px)`;
          ind.style.opacity = "1";
          icon.style.transform = "";
          ind.classList.add("ptr-spinning");
          setLabel("loading");
          try { await onRefresh(); }
          catch (e) { /* swallow — the refresh fn's own toast handles err */ }
          finally {
            ind.classList.remove("ptr-spinning");
            ind.style.transform = "";
            ind.style.opacity = "0";
            refreshing = false;
          }
        } else {
          ind.style.transform = "";
          ind.style.opacity = "0";
        }
      }, { passive: true });
      el.addEventListener("touchcancel", () => {
        pulling = false;
        if (!refreshing) {
          ind.style.transform = "";
          ind.style.opacity = "0";
        }
      });
    },

    // First-load splash + initial fetch sequence. Sets appReady=true once
    // contextInfo + sessions both come back, OR after 8s hard timeout (so
    // a dead backend doesn't leave the user on a splash forever — we surface
    // the issue via the reconnect banner instead).
    async _bootApp() {
      // Splash hint after 3s ("still warming up...")
      this._splashHintTimer = setTimeout(() => {
        this.splashHint = this.t("splash.slow");
      }, 3000);
      // Hard timeout — if 8s pass without a successful fetch, drop splash
      // and let the reconnect banner take over.
      this._splashHardTimeout = setTimeout(() => {
        if (!this.appReady) {
          this.appReady = true;
          this.connState = "reconnecting";
        }
      }, 8000);

      this.loadPrefs();
      this.loadRoot();
      this.initSessions();
      this.fetchStats();
      // Surface any in-flight turns that were cut short by a previous
      // process death (OOM kill / power loss / manual restart mid-stream).
      // Fire-and-forget — purely informational, doesn't block boot. Backend
      // returns [] when nothing was interrupted (the common case).
      this._checkInterruptedTurns();
      // First-run hint — surface key shortcuts so the user doesn't have to
      // hunt for them. Flagged in localStorage so it only fires once. Short
      // delay lets the splash clear first.
      if (!localStorage.getItem("muselab_seen_help")) {
        setTimeout(() => {
          this.toast(
            this.lang === "zh"
              ? "Tip：⌘K 命令面板 · / 斜杠命令 · @ 引用文件 · ↑ 回滚上一条"
              : "Tip: ⌘K command palette · / slash commands · @ to reference files · ↑ to recall last message",
            "info", 7000);
          localStorage.setItem("muselab_seen_help", "1");
        }, 1500);
      }
      // Same preview-file restore that login() does — covers the
      // already-authed boot path (page refresh with saved token).
      if (this._pendingPreviewSelected) {
        const path = this._pendingPreviewSelected;
        this._pendingPreviewSelected = null;
        this.openFile({ path, name: path.split("/").pop() })
            .catch(() => { /* file gone — silent */ });
      }
      // Restore mobile tab choice AFTER openFile (which would otherwise
      // force-switch us to "preview" on mobile every reopen). $nextTick
      // ensures the openFile-induced mobileTab="preview" assignment
      // settles before we override it back to the user's actual last tab.
      // Desktop ignores mobileTab entirely so this is a no-op there.
      if (this._pendingMobileTab) {
        const wantTab = this._pendingMobileTab;
        this._pendingMobileTab = null;
        this.$nextTick(() => {
          if (window.innerWidth <= 900) this.mobileTab = wantTab;
        });
      }
      // Block readiness on context-info (the most important one for the
      // onboarding cards). Others come along in parallel.
      try {
        await this.fetchContextInfo();
        this._markReady();
      } catch (e) {
        // Will retry via heartbeat
      }
      this._startHeartbeat();
    },

    _markReady() {
      if (this.appReady) return;
      this.appReady = true;
      clearTimeout(this._splashHintTimer);
      clearTimeout(this._splashHardTimeout);
      this.splashHint = "";
    },

    // Friendly "5 min ago" / "刚刚" / "3 h ago" formatter for the
    // interrupted-turn toast. Only used here; no need to factor out.
    _agoLabel(ts) {
      if (!ts) return this.lang === "zh" ? "未知时间" : "unknown";
      const diff = Date.now() / 1000 - ts;
      if (diff < 60) return this.lang === "zh" ? "刚刚" : "just now";
      if (diff < 3600) {
        const m = Math.round(diff / 60);
        return this.lang === "zh" ? `${m} 分钟前` : `${m}m ago`;
      }
      if (diff < 86400) {
        const h = Math.round(diff / 3600);
        return this.lang === "zh" ? `${h} 小时前` : `${h}h ago`;
      }
      const d = Math.round(diff / 86400);
      return this.lang === "zh" ? `${d} 天前` : `${d}d ago`;
    },

    // Toast any turns the previous muselab process left in-flight at the
    // moment it died. Backend persists `sessions/active_turns/<sid>.json`
    // on turn start and deletes it on clean completion — anything left
    // over after restart is an interrupted turn.
    //
    // We do NOT auto-resume. Auto-resume would burn tokens on conversations
    // the user has already moved past, and bypasses their own judgment of
    // whether the prompt is worth rephrasing. Frontend just surfaces the
    // sid + preview; user decides.
    //
    // We dismiss on the backend immediately after toasting (regardless of
    // whether the user clicks the action). The point is "tell the user
    // once" — if they let the toast fade, they've still been notified, and
    // re-nagging on every restart would be annoying.
    async _checkInterruptedTurns() {
      let resp;
      try {
        resp = await this.api("/api/chat/interrupted-turns");
      } catch (e) {
        return;   // network / auth issue — heartbeat will retry boot
      }
      if (!resp.ok || !resp.data || !Array.isArray(resp.data.turns)) return;
      const turns = resp.data.turns;
      if (!turns.length) return;
      for (const turn of turns) {
        const ago = this._agoLabel(turn.started_at);
        const preview = (turn.preview || "").trim();
        const truncated = preview.length > 60
          ? preview.slice(0, 59) + "…"
          : preview || (this.lang === "zh" ? "(空消息)" : "(empty prompt)");
        const msg = this.lang === "zh"
          ? `上次对话被中断（${ago}）：${truncated}`
          : `Last turn interrupted (${ago}): ${truncated}`;
        this.toast(msg, "warn", 0, {
          label: this.lang === "zh" ? "打开" : "Open",
          onClick: () => { this.openTab(turn.sid).catch(() => {}); },
        });
        // Mark dismissed on backend — see method docstring for rationale.
        fetch(`/api/chat/interrupted-turns/${turn.sid}/dismiss`, {
          method: "POST", headers: this.hdr(),
        }).catch(() => { /* best-effort */ });
      }
    },

    // 10s heartbeat — pings /api/meta. If 2 consecutive fails, flag reconnecting;
    // when one comes back, flash "reconnected" then auto-clear.
    _startHeartbeat() {
      if (this._connHeartbeat) clearInterval(this._connHeartbeat);
      this._connHeartbeat = setInterval(() => this._pingHealth(), 10_000);
    },
    async _pingHealth() {
      try {
        const r = await fetch("/api/meta", { headers: this.hdr() });
        if (!r.ok) throw new Error("status " + r.status);
        // Healthy
        if (this.connState === "reconnecting") {
          this.connState = "reconnected";
          // After a 1.5s flash of green, drop back to silent ok.
          setTimeout(() => {
            if (this.connState === "reconnected") this.connState = "ok";
          }, 1500);
          // Also refresh sessions / context — they may be stale post-restart
          this.refreshSessions();
          this.fetchContextInfo();
        } else {
          this.connState = "ok";
        }
        this._connFails = 0;
        // Refresh scheduler unread count — cheap (single JSON, no auth
        // round-trip beyond what /api/meta already costs) and keeps the
        // bell badge live without forcing the user to open the drawer.
        this.fetchSchedulerUnread();
      } catch (e) {
        this._connFails++;
        if (this._connFails >= 2) this.connState = "reconnecting";
        // Splash → if we never managed to ready up, force ready so user sees
        // the banner (otherwise they stare at splash with no feedback).
        if (!this.appReady) this._markReady();
      }
    },

    _cm: null,
    cmMode(path) {
      if (!path) return "text/plain";
      const ext = path.split(".").pop().toLowerCase();
      const map = {
        md: "markdown", markdown: "markdown",
        py: "python",
        js: "javascript", mjs: "javascript", jsx: "javascript",
        ts: "text/typescript", tsx: "text/typescript",
        json: "application/json",
        html: "htmlmixed", htm: "htmlmixed",
        xml: "xml", svg: "xml",
        css: "css", scss: "css", less: "css",
        yaml: "yaml", yml: "yaml",
        sh: "shell", bash: "shell", zsh: "shell",
        go: "go",
        rs: "rust",
        c: "text/x-csrc", h: "text/x-csrc",
        cpp: "text/x-c++src", hpp: "text/x-c++src",
        java: "text/x-java",
      };
      return map[ext] || "text/plain";
    },
    mountCM() {
      this.$nextTick(() => {
        if (!window.CodeMirror) { console.warn("[muselab] CodeMirror not loaded"); return; }
        const host = this.$refs.cmHost;
        if (!host) { console.warn("[muselab] no cmHost ref"); return; }
        host.innerHTML = "";
        const modeStr = this.cmMode(this.selected);
        try {
          const cm = window.CodeMirror(host, {
            value: String(this.editText || ""),
            mode: modeStr,
            lineNumbers: true,
            lineWrapping: true,
            tabSize: 2,
            indentUnit: 2,
            theme: this.theme === "light" ? "default" : "material-darker",
            // Ctrl/Cmd+S inside the editor → save. Without this, on some
            // browsers the browser's own "save page" dialog can fire even
            // when the document-level keydown handler exists, because
            // CodeMirror's contenteditable subtree captures the event
            // first. Hooking it here is the most defensive spot.
            extraKeys: {
              "Ctrl-S": () => { this.saveEdit(); },
              "Cmd-S":  () => { this.saveEdit(); },
            },
          });
          // Initial status
          this.cmStatus = {
            line: 1, col: 1, sel: 0,
            lines: cm.lineCount(),
            chars: cm.getValue().length,
            mode: this.shortMode(modeStr),
            dirty: false,
          };
          const updateStatus = () => {
            const c = cm.getCursor();
            const sel = cm.getSelection().length;
            this.cmStatus = {
              line: c.line + 1, col: c.ch + 1, sel,
              lines: cm.lineCount(),
              chars: cm.getValue().length,
              mode: this.shortMode(modeStr),
              dirty: cm.getValue() !== String(this.rawText || ""),
            };
          };
          cm.on("change", () => { this.editText = cm.getValue(); updateStatus(); });
          cm.on("cursorActivity", updateStatus);
          window.__muselab_cm = cm;
          setTimeout(() => { cm.refresh(); updateStatus(); }, 50);
        } catch (e) {
          console.error("[muselab] CodeMirror init failed:", e);
          this.toast(
            (this.lang === "zh" ? "编辑器初始化失败：" : "Editor init failed: ")
              + e.message, "error", 6000);
          host.innerHTML = '<textarea style="width:100%;height:100%;padding:14px;background:var(--c-bg-0);color:var(--c-fg-0);border:0;font:13px ui-monospace,monospace;resize:none"></textarea>';
          const ta = host.querySelector("textarea");
          ta.value = this.editText;
          ta.addEventListener("input", () => { this.editText = ta.value; });
        }
      });
    },
    shortMode(mode) {
      // CM 内部 mode 名标准化成显示用短名
      if (!mode) return "text";
      if (mode === "text/plain") return "text";
      if (mode === "htmlmixed") return "html";
      if (mode.includes("/")) return mode.split("/").pop().replace(/^x-/, "");
      return mode;
    },
    unmountCM() {
      const host = this.$refs.cmHost;
      if (host) host.innerHTML = "";
      window.__muselab_cm = null;
    },

    initTheme() {
      const saved = localStorage.getItem("muselab_theme");
      if (saved === "light" || saved === "dark") {
        this.theme = saved;
      } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
        this.theme = "light";
      }
      const savedAccent = localStorage.getItem("muselab_accent");
      if (savedAccent) this.accent = savedAccent;
      this.applyTheme();
      this.applyAccent();
      // Reactive system-theme tracking: when the OS flips between light/dark
      // and the user hasn't explicitly overridden, follow along. Once they
      // toggle muselab's theme manually (writes muselab_theme), this listener
      // becomes a no-op for them.
      if (window.matchMedia) {
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        const onSysFlip = (ev) => {
          if (localStorage.getItem("muselab_theme")) return;
          this.theme = ev.matches ? "dark" : "light";
          this.applyTheme();
        };
        if (mq.addEventListener) mq.addEventListener("change", onSysFlip);
        else if (mq.addListener) mq.addListener(onSysFlip);   // legacy Safari
      }
    },
    applyTheme() {
      document.documentElement.setAttribute("data-theme", this.theme);
      const link = document.getElementById("hljs-theme");
      if (link) {
        link.href = this.theme === "light"
          ? "/static/vendor/highlight-theme-light.css"
          : "/static/vendor/highlight-theme.css";
      }
    },
    applyAccent() {
      // 主色 + 派生色（hover / soft 半透明 / 文字色用浅化 mix 实现）
      const r = document.documentElement.style;
      r.setProperty("--c-accent", this.accent);
      r.setProperty("--c-accent-hover", this._shade(this.accent, this.theme === "light" ? -15 : 12));
      r.setProperty("--c-accent-soft", this._withAlpha(this.accent, this.theme === "light" ? 0.10 : 0.14));
      r.setProperty("--c-accent-fg", this.theme === "light"
        ? this._shade(this.accent, -25)
        : this._shade(this.accent, 25));
    },
    setAccent(color) {
      this.accent = color;
      localStorage.setItem("muselab_accent", color);
      this.applyAccent();
      if (this.MASCOTS) this.applyFavicon();  // favicon 跟主题色同步
    },

    // ===== i18n =====
    initLang() {
      const saved = localStorage.getItem("muselab_lang");
      if (saved === "zh" || saved === "en") this.lang = saved;
      else this.lang = (navigator.language || "zh").toLowerCase().startsWith("en") ? "en" : "zh";
      document.documentElement.lang = this.lang;
    },
    setLang(lang) {
      if (lang !== "zh" && lang !== "en") return;
      this.lang = lang;
      localStorage.setItem("muselab_lang", lang);
      document.documentElement.lang = lang;
      this.toast(this.t("toast.lang_switched"), "success", 1500);
    },
    // t("key.path", {var: "x"}) — 简单变量插值；缺 key 时回退到 key 本身（方便发现遗漏）
    t(key, vars) {
      const table = STRINGS[this.lang] || STRINGS.zh;
      let s = table[key];
      if (s == null) s = (STRINGS.zh[key] != null ? STRINGS.zh[key] : key);
      if (vars) {
        for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
      }
      return s;
    },

    // ===== Muse mascot =====
    initMascot() {
      // User-pinned mascot (set by cycleMascot) wins over the time-based
      // default. Without persistence, a user who clicked through to e.g.
      // Urania saw it reset on every reload — the daily rotation logic
      // ignored their explicit choice.
      const pinned = localStorage.getItem("muselab_mascot_idx");
      if (pinned !== null) {
        const i = parseInt(pinned, 10);
        if (Number.isInteger(i) && i >= 0 && i < this.MASCOTS.length) {
          this.mascotIdx = i;
          this.applyFavicon();
          setTimeout(() => this.greetMascot(this.mascotLabel()), 400);
          return;
        }
      }
      // First time ever (no pinned value): pick by today's hash, AND
      // immediately persist so it stays put. Otherwise every page load
      // would re-roll mascot every hour (date+hour seed) — which is
      // what "I want my pick to stay" complaints are really about.
      const seed = new Date().toISOString().slice(0, 13);
      let h = 5381;
      for (let i = 0; i < seed.length; i++) h = ((h << 5) + h + seed.charCodeAt(i)) | 0;
      this.mascotIdx = Math.abs(h) % this.MASCOTS.length;
      try { localStorage.setItem("muselab_mascot_idx", String(this.mascotIdx)); } catch {}
      this.applyFavicon();
      setTimeout(() => this.greetMascot(this.mascotLabel()), 400);
    },
    mascot() { return this.MASCOTS[this.mascotIdx]; },
    mascotHref() { return "#m-" + this.mascot().id; },
    // Short label shown inside the user-side message avatar. Muselab has no
    // identity layer (single-user, token-auth), so we just stamp "我" / "U"
    // by language. Cheap, requires no extra SVG asset.
    userAvatarText() { return this.lang === "zh" ? "我" : "U"; },
    // 显示文案：英文界面 "Muse · Urania · Astronomy"；中文界面 "Muse · 乌拉尼亚 · 天文"（保留希腊名作 hint）
    mascotLabel() {
      const m = this.mascot();
      if (this.lang === "zh") return `Muse · ${m.zhName}（${m.greek}）· ${m.domain.zh}`;
      return `Muse · ${m.greek} · ${m.domain.en}`;
    },
    mascotShortLabel() {
      const m = this.mascot();
      return this.lang === "zh" ? `${m.zhName} · ${m.domain.zh}` : `${m.greek} · ${m.domain.en}`;
    },
    cycleMascot() {
      this.mascotIdx = (this.mascotIdx + 1) % this.MASCOTS.length;
      // Pin the choice — initMascot reads this on next load.
      try { localStorage.setItem("muselab_mascot_idx", String(this.mascotIdx)); } catch {}
      this.applyFavicon();
      this.greetMascot(this.mascotLabel());
    },
    // 把当前 mascot 渲染成 data:image/svg+xml favicon，跟着主题色走
    applyFavicon() {
      const id = this.mascot().id;
      // 重新声明每个 mascot 的 SVG body —— defs 在 document 里通过 <use> 引用，但 favicon
      // data URL 是独立文档，必须把图形内嵌。集中在这里维护成 lookup。
      const SHAPES = {
        hex:      '<path d="M12 3 L20 7.5 L20 16.5 L12 21 L4 16.5 L4 7.5 Z"/>',
        bars:     '<line x1="4" y1="7" x2="20" y2="7"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="10" y1="17" x2="14" y2="17"/>',
        lens:     '<circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/>',
        wave:     '<circle cx="12" cy="12" r="9"/><path d="M5 12 Q 8.5 6 12 12 T 19 12"/>',
        crescent: '<path d="M16 3 A 9 9 0 1 0 16 21 A 7 7 0 1 1 16 3 Z"/>',
        halo:     '<circle cx="12" cy="14" r="5"/><path d="M5 8 A 7 4 0 0 1 19 8"/>',
        trio:     '<circle cx="12" cy="6" r="2" fill="currentColor"/><circle cx="6" cy="17" r="2" fill="currentColor"/><circle cx="18" cy="17" r="2" fill="currentColor"/>',
        spark:    '<line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/><circle cx="12" cy="12" r="2" fill="currentColor"/>',
        orbit:    '<circle cx="11" cy="13" r="5"/><circle cx="18.5" cy="6" r="1.6" fill="currentColor"/>',
      };
      const color = this.accent || "#6093ff";
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color:${color}">${SHAPES[id] || SHAPES.orbit}</svg>`;
      const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
      let link = document.querySelector('link[rel="icon"]');
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        document.head.appendChild(link);
      }
      link.type = "image/svg+xml";
      link.href = url;
    },
    greetMascot(msg) {
      // 去重：同一条 msg 在 1.5s 内重复调用只 toast 一次（Alpine $watch 在某些场景会双触发，
      // 比如 rightOpen 既被 loadPrefs 写又被点击 toggle 时的 render 顺序）。
      const now = Date.now();
      if (msg && this._lastGreetMsg === msg && now - this._lastGreetAt < 1500) {
        return;
      }
      this._lastGreetMsg = msg;
      this._lastGreetAt = now;
      this.mascotGreet = true;
      if (msg) this.toast(msg, "info", 1400);
      clearTimeout(this._mascotT);
      this._mascotT = setTimeout(() => { this.mascotGreet = false; }, 900);
    },
    toggleTheme() {
      this.theme = this.theme === "light" ? "dark" : "light";
      this.applyTheme();
      this.applyAccent();   // 派生色对深浅敏感，重算
      localStorage.setItem("muselab_theme", this.theme);
      if (window.__muselab_cm) window.__muselab_cm.setOption("theme", this.theme === "light" ? "default" : "material-darker");
    },

    // 色彩小工具
    _withAlpha(hex, alpha) {
      const { r, g, b } = this._hex2rgb(hex);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    },
    _shade(hex, percent) {
      // percent 正数变亮，负数变暗，幅度 0-100
      const { r, g, b } = this._hex2rgb(hex);
      const adj = v => Math.max(0, Math.min(255, Math.round(v + (255 - v) * percent / 100) - (percent < 0 ? Math.round(v * -percent / 100) : 0)));
      const a = (v) => percent >= 0 ? Math.round(v + (255 - v) * percent / 100) : Math.round(v * (1 + percent / 100));
      const cap = v => Math.max(0, Math.min(255, v));
      return "#" + [cap(a(r)), cap(a(g)), cap(a(b))].map(x => x.toString(16).padStart(2, "0")).join("");
    },
    _hex2rgb(hex) {
      const h = hex.replace("#", "");
      const v = h.length === 3 ? h.split("").map(c => c + c).join("") : h;
      return { r: parseInt(v.slice(0, 2), 16), g: parseInt(v.slice(2, 4), 16), b: parseInt(v.slice(4, 6), 16) };
    },

    configureMarked() {
      // marked v13 removed the `highlight` option; we post-process rendered HTML
      // via highlightCode() instead. Nothing to configure here for now.
    },

    // Render markdown -> sanitized HTML. All markdown rendering MUST go through
    // here; passing raw `marked.parse(...)` to x-html opens XSS via untrusted
    // file content / Claude responses containing <script>, on*, javascript: etc.
    // ===== attachment helpers (images + docs) =====
    // Classify a file by mime/extension to decide which preview chip to show
    // and (cosmetically) what kind label to display. Server has the
    // authoritative classification — we just guess for the chip.
    _classifyFile(file) {
      const m = (file.type || "").toLowerCase();
      const name = (file.name || "").toLowerCase();
      if (m.startsWith("image/")) return "image";
      if (m === "application/pdf" || name.endsWith(".pdf")) return "pdf";
      const textMimes = ["text/", "application/json", "application/xml",
                          "application/yaml", "application/x-yaml",
                          "application/toml"];
      if (textMimes.some(p => m.startsWith(p) || m === p)) return "text";
      const textExts = [".md", ".markdown", ".txt", ".csv", ".json", ".yaml",
                         ".yml", ".toml", ".py", ".sh", ".js", ".ts", ".tsx",
                         ".jsx", ".html", ".css", ".xml", ".log", ".ini",
                         ".conf", ".cfg", ".rs", ".go", ".java", ".c", ".h",
                         ".cpp", ".hpp", ".rb", ".php", ".swift", ".kt",
                         ".sql"];
      if (textExts.some(ext => name.endsWith(ext))) return "text";
      // Spreadsheets — backend converts them to CSV-style text via
      // openpyxl. Use "text" kind for the chip since that's what they
      // become downstream; the backend echoes the same.
      const xlsxExts = [".xlsx", ".xlsm", ".xltx", ".xltm"];
      if (xlsxExts.some(ext => name.endsWith(ext))) return "text";
      // Anything else: still try to upload — the backend has the final say
      // and will reject with a clear error if it can't be handled. Better
      // UX than silently filtering files out client-side and confusing the
      // user (the "I uploaded but nothing happened" bug).
      return "text";
    },
    // Client-side image compression. Re-encode large photos to JPEG
    // capped at 1600px on the long edge, quality 0.85. Cuts a typical
    // 4 MB iPhone photo down to ~300 KB and removes the upload-stall
    // pain on 4G / slow Wi-Fi. Skips:
    //   - non-images (PDFs etc.)
    //   - GIF (would lose animation)
    //   - SVG (vector — re-rasterising is destructive)
    //   - tiny files (< 256 KB — overhead > savings)
    // Falls back to the original file if any step fails (older Safari
    // rejecting createImageBitmap options, HEIC the browser can't
    // decode, OOM on huge canvases, etc.) — better a slow upload than
    // none. The returned File preserves the original base name with a
    // .jpg extension so the backend's name-based classification still
    // sees an image.
    async _maybeCompressImage(file) {
      if (!file || !file.type || !file.type.startsWith("image/")) return file;
      // GIF: animation would be lost re-encoding to a still frame.
      // SVG: vector — re-encoding to raster destroys the whole point.
      if (file.type === "image/gif" || file.type === "image/svg+xml") return file;
      // Compression tuned 2026-05-21 → re-tuned same day after user
      // reported "图片发送很慢 还发不出去". The WebP encode path was the
      // likely culprit:
      //   - iOS Safari's canvas.toBlob("image/webp") is supported in
      //     iOS 14+ but is significantly slower than JPEG on the same
      //     canvas (especially on older iPhone hardware) — by the time
      //     the encode finishes the user has already tapped send and is
      //     staring at a frozen UI.
      //   - When WebP encode falls back (PNG / null), we ran JPEG too,
      //     doubling total encode time.
      //   - Failure modes between WebP and JPEG diverge, making errors
      //     harder to debug.
      // Rolling back to JPEG-only — simpler, faster, universally
      // supported. We keep the smaller dimension / quality from the
      // previous attempt because those translate directly to upload
      // bytes (the main bottleneck on 4G).
      const COMPRESS_THRESHOLD = 256 * 1024;
      if (file.size < COMPRESS_THRESHOLD) return file;
      const MAX_DIM = 1280;     // pixel count down ~36% vs 1600
      const QUALITY = 0.72;     // file size down ~30-40% vs 0.85
      try {
        let bitmap;
        try {
          bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
        } catch (_) {
          // Older Safari rejects unknown options — retry without.
          bitmap = await createImageBitmap(file);
        }
        const w0 = bitmap.width, h0 = bitmap.height;
        const ratio = Math.min(1, MAX_DIM / Math.max(w0, h0));
        const w = Math.max(1, Math.round(w0 * ratio));
        const h = Math.max(1, Math.round(h0 * ratio));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d", { alpha: false });
        // JPEG has no alpha — flatten transparent originals onto white
        // so a PNG with cutout becomes a sensible JPEG instead of black.
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, w, h);
        ctx.drawImage(bitmap, 0, 0, w, h);
        if (bitmap.close) bitmap.close();
        const blob = await new Promise(res =>
          canvas.toBlob(res, "image/jpeg", QUALITY));
        if (!blob || blob.size >= file.size) return file;   // not worth it
        const base = (file.name || "image").replace(/\.[^.]+$/, "");
        return new File([blob], base + ".jpg",
                          { type: "image/jpeg", lastModified: Date.now() });
      } catch (e) {
        console.warn("[muselab] image compression failed, sending original:", e);
        return file;
      }
    },
    async _attachFile(file) {
      if (file.size > 10 * 1024 * 1024) {
        this.toast(this.t("img.too_big"), "warn", 2500);
        return;
      }
      const kind = this._classifyFile(file);
      if (kind === "unknown") {
        this.toast(this.t("attach.bad_type") + ": " + file.name, "warn", 3500);
        return;
      }

      // Diagnostic timing — when uploads feel slow the user has no way to
      // tell if it's compression CPU on the phone or network throughput.
      // We split the timeline into "compress" / "data-url" / "upload" and
      // attach the totals to a console.log + the chip's title attribute.
      // Cheap (just performance.now() calls) and only kept user-visible
      // via a debug toast when an upload exceeds 3 s (likely-feeble feedback
      // surface; cleaner mechanism than scattering prompts).
      const t0 = performance.now();
      const origSize = file.size;
      let tCompressEnd = t0;

      let entry;
      if (kind === "image") {
        // Compress BEFORE generating the preview + upload — both reuse
        // the smaller file. Preview as base64 of the compressed image
        // also avoids holding a 4 MB data URL in memory per pending img.
        file = await this._maybeCompressImage(file);
        tCompressEnd = performance.now();
        const preview = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result);
          fr.onerror = rej;
          fr.readAsDataURL(file);
        });
        const raw = { id: null, mime: file.type, preview,
                       uploading: true, error: false };
        this.pendingImages.push(raw);
        // Alpine v3 wraps each pushed item in a Proxy. The local `raw`
        // reference still points at the original (non-proxied) object;
        // mutating raw.uploading bypasses the Proxy's set trap and
        // doesn't fire reactivity → the chip's `:class="{uploading:...}"`
        // binding never re-evaluates and the progress bar slides forever
        // even after the upload completes. Bug observed 2026-05-21.
        // Pull the proxied version back out of the array so subsequent
        // mutations go through Alpine's reactive layer.
        entry = this.pendingImages[this.pendingImages.length - 1];
      } else {
        const raw = { id: null, name: file.name, kind,
                       uploading: true, error: false };
        this.pendingDocs.push(raw);
        // Same Alpine-proxy gotcha as above — must use the proxied
        // reference for entry.uploading = false to actually trigger UI.
        entry = this.pendingDocs[this.pendingDocs.length - 1];
      }

      const tUploadStart = performance.now();
      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch("/api/chat/upload-image", {
          method: "POST", headers: this.hdr(), body: fd,
        });
        if (!r.ok) {
          entry.error = true; entry.uploading = false;
          // Include HTTP status in the toast so "image upload failed: 413
          // file too large" or ":400 unsupported file type" is visible to
          // the user instead of just "upload failed". Helps a lot when
          // diagnosing why a particular photo won't go through.
          let body = "";
          try { body = await r.text(); } catch (_) {}
          this.toast(`${this.t("img.upload_failed")} (HTTP ${r.status})${body ? ": " + body : ""}`,
                      "error", 5000);
          return;
        }
        const d = await r.json();
        entry.id = d.id; entry.uploading = false;
        // Server's classification wins for kind label.
        if (d.kind && entry.kind) entry.kind = d.kind;
        // === Diagnostic timing report ===
        const tEnd = performance.now();
        const compressMs = Math.round(tCompressEnd - t0);
        const networkMs = Math.round(tEnd - tUploadStart);
        const totalMs = Math.round(tEnd - t0);
        const finalKB = Math.round(file.size / 1024);
        const origKB = Math.round(origSize / 1024);
        // Console line is always emitted (only visible if devtools open).
        console.log(
          `[muselab][upload] ${file.name || "(unnamed)"} ` +
          `orig=${origKB}KB → final=${finalKB}KB · ` +
          `compress=${compressMs}ms · network=${networkMs}ms · total=${totalMs}ms`
        );
        // Visible toast ONLY when upload felt slow (>3 s). On phone this
        // turns "huh, that was slow" into "ah, network was 4.2 s — my Wi-Fi
        // is bad", actionable instead of mysterious. Below threshold we
        // stay silent so normal fast uploads don't add chrome.
        if (totalMs > 3000) {
          this.toast(
            this.lang === "zh"
              ? `📤 上传 ${totalMs}ms (压缩 ${compressMs}ms · 网络 ${networkMs}ms · ${finalKB}KB)`
              : `📤 Upload ${totalMs}ms (compress ${compressMs}ms · network ${networkMs}ms · ${finalKB}KB)`,
            "info", 4000);
        }
      } catch (e) {
        entry.error = true; entry.uploading = false;
        // Network-level failure (TypeError: Failed to fetch / NetworkError /
        // AbortError). Surface the error name so user sees whether it's
        // "lost connection" vs "request aborted" vs "CORS rejected". Most
        // common on mobile is intermittent 4G drops.
        const reason = (e && (e.name + (e.message ? ": " + e.message : "")))
                         || "network error";
        this.toast(`${this.t("img.upload_failed")} — ${reason}`, "error", 5000);
      }
    },
    async onAttachPicked(ev) {
      const files = Array.from(ev.target.files || []);
      ev.target.value = "";
      for (const f of files) await this._attachFile(f);
    },
    async onAttachDrop(ev) {
      const files = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []);
      for (const f of files) await this._attachFile(f);
    },
    async onImagePaste(ev) {
      // Only handle pasted image data; let normal text paste through.
      const items = (ev.clipboardData && ev.clipboardData.items) || [];
      const files = [];
      for (const it of items) {
        if (it.kind === "file") {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        ev.preventDefault();
        for (const f of files) await this._attachFile(f);
      }
    },
    removePendingImage(i) { this.pendingImages.splice(i, 1); },
    removePendingDoc(i) { this.pendingDocs.splice(i, 1); },

    // Alias for use in inline x-html (shorter name reads better in markup).
    renderMd(text) { return this.mdRender(text); },
    // Friendly label for a model id — falls back to the raw id if not in catalog.
    // Used by the bubble badge so old messages keep showing their original model
    // (deepseek / glm / claude variants) instead of just the long id.
    modelLabel(id) {
      if (!id) return "";
      const meta = (this.availableModels || []).find(m => m.model === id);
      return meta ? meta.label : id;
    },
    // Format a millis-since-epoch timestamp as "HH:MM" in the user's
    // local timezone. Used by the per-turn footer to show when a
    // muse reply finished. Returns "" for falsy input so x-show can
    // gate on m.ts directly. 24h clock by design.
    fmtHM(ts) {
      if (!ts) return "";
      const d = new Date(ts);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return hh + ":" + mm;
    },
    // Filter messages for the sidebar outline. Returns only user prompts
    // (skipping the auto-injected compact summaries) — they're what the
    // user remembers asking, so they make the best jump targets.
    outlineMessages() {
      return (this.messages || []).filter(
        m => m && m.role === "user" && !m._is_compact_summary);
    },
    // Outline click → scroll the chat to that user msg + flash highlight.
    // .msg[data-uuid] is rendered for every message (see chat template);
    // on mobile we also switch to the chat tab so the jump is visible.
    _scrollToUserMsg(m) {
      const uuid = m && m.uuid;
      if (!uuid) return;
      if (window.innerWidth <= 900) this.mobileTab = "chat";
      this.$nextTick(() => {
        const el = document.querySelector(
          `.msg[data-uuid="${CSS.escape(uuid)}"]`);
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("msg-highlight");
        setTimeout(() => el.classList.remove("msg-highlight"), 2400);
      });
    },
    // Short preview text for an outline row — first line, trimmed.
    outlineText(m) {
      const t = (m && (m.text || m.body || "")) || "";
      const oneLine = t.replace(/\s+/g, " ").trim();
      return oneLine.slice(0, 60) || (this.lang === "zh" ? "（无文本）" : "(no text)");
    },
    // Manual toggle — used by the floating button. Necessary fallback for
    // iframe / pdf / img preview where onPreviewScroll can't fire because
    // scroll events don't cross the sandbox boundary.
    toggleImmersive() {
      this.previewImmersive = !this.previewImmersive;
      document.body.classList.toggle("preview-immersive",
                                     this.previewImmersive);
    },
    // Mobile preview scroll — toggle immersive mode (hide top header + tab
    // bar + bottom-nav) based on scroll direction. Desktop: no-op.
    onPreviewScroll(el) {
      if (!el || window.innerWidth > 900) return;
      if (this.mobileTab !== "preview") return;
      const cur = el.scrollTop;
      const last = this._lastPreviewScrollTop || 0;
      const delta = cur - last;
      // Near the top → always show (so the user can reach the file name /
      // tab bar without scrolling back up first).
      if (cur < 50) {
        if (this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
        return;
      }
      // Scrolling down ≥30px → hide bars.
      if (delta > 30) {
        if (!this.previewImmersive) {
          this.previewImmersive = true;
          document.body.classList.add("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
      // Scrolling up ≥10px → show bars (lighter threshold so it feels
      // responsive when the user wants navigation back).
      } else if (delta < -10) {
        if (this.previewImmersive) {
          this.previewImmersive = false;
          document.body.classList.remove("preview-immersive");
        }
        this._lastPreviewScrollTop = cur;
      }
      // Sub-threshold deltas: don't update last — let movement accumulate.
    },
    // Footer time: today → HH:MM, otherwise YYYYMMDD HH:MM. Used in turn-footer
    // so old conversations don't lose date context once you scroll up.
    fmtTurnTime(ts) {
      if (!ts) return "";
      const d = new Date(ts);
      const now = new Date();
      const sameDay = d.getFullYear() === now.getFullYear()
                       && d.getMonth() === now.getMonth()
                       && d.getDate() === now.getDate();
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      if (sameDay) return hh + ":" + mm;
      const Y = d.getFullYear();
      const M = String(d.getMonth() + 1).padStart(2, "0");
      const D = String(d.getDate()).padStart(2, "0");
      return `${Y}${M}${D} ${hh}:${mm}`;
    },
    // True when index i in messages[] is the tail of a turn — i.e. it
    // is muse-side AND the next message is either nonexistent or
    // user-role. Used by the per-turn footer (index.html) to decide
    // which message gets the footer rendered underneath it. Cheap O(1)
    // lookup; Alpine re-evaluates it per render which is fine since
    // it's a few comparisons.
    isTurnTail(i) {
      const arr = this.messages;
      if (!arr || i < 0 || i >= arr.length) return false;
      const m = arr[i];
      if (!m || m.role === "user") return false;
      const next = arr[i + 1];
      return !next || next.role === "user";
    },
    // Normalize a model-emitted path into something openByPathToasted can hand
    // to /api/files/list. Handles three things the model commonly does wrong:
    //   - absolute path under ROOT  →  strip ROOT prefix
    //   - "~/..." path              →  return "" (we don't know HOME-vs-ROOT)
    //   - path prefixed by ROOT's basename (e.g. "claude_space/health/x.md"
    //     when ROOT itself is /home/u/claude_space) → strip the duplicate
    // Returns "" for paths we can't safely open (would 403 / 404 on backend).
    _normalizeArchivePath(p) {
      if (!p) return "";
      const root = (this.contextInfo && this.contextInfo.archive_root) || "";
      if (p.startsWith("/")) {
        if (root && (p === root || p.startsWith(root + "/"))) {
          return p.slice(root.length).replace(/^\/+/, "");
        }
        return "";
      }
      if (p.startsWith("~/")) return "";
      // Model often writes "claude_space/foo/bar.md" thinking the archive root
      // is the parent of claude_space. Strip the basename of ROOT if it's the
      // first segment and stripping leaves a non-empty remainder.
      if (root) {
        const base = root.split("/").pop();
        if (base && p.startsWith(base + "/")) {
          return p.slice(base.length + 1);
        }
      }
      return p;
    },

    // For file-centric tools (Read / Edit / Write / NotebookEdit), extract the
    // file path from the tool input so the bubble can render it as a clickable
    // .file-link instead of plain summary text. Returns "" when the tool is
    // not file-centric or the path is empty/system-path we wouldn't open.
    toolFilePath(m) {
      if (!m || m.role !== "tool_use") return "";
      const FILE_TOOLS = new Set(["Read", "Edit", "Write", "NotebookEdit", "MultiEdit"]);
      if (!FILE_TOOLS.has(m.name)) return "";
      const inp = m.input || {};
      const path = inp.file_path || inp.notebook_path || "";
      return this._normalizeArchivePath(path);
    },
    // Render mcp__<server>__<tool> nicely: drop the mcp__ prefix, replace __ with " · "
    renderToolName(name) {
      if (!name) return "";
      if (name.startsWith("mcp__")) {
        return name.slice(5).split("__").join(" · ");
      }
      return name;
    },

    mdRender(text) {
      if (!text) return "";
      // Streaming-friendly preprocess: close any unclosed ``` or ~~~ fenced
      // code blocks before handing to marked. Without this, while the model
      // is mid-codeblock the parser sees `<lang>\n<content>` with no closer
      // and either drops everything past the opening fence or returns an
      // empty render — the user perceives this as "Muse stalled mid-reply,
      // then dumped the rest on completion". Patches a *copy* fed to
      // marked; the source `text` stays the truth. Both fence kinds covered;
      // already-balanced text is untouched.
      let parseInput = text;
      const tripleCount = (text.match(/```/g) || []).length;
      if (tripleCount % 2 === 1) parseInput += "\n```";
      const tildeCount = (text.match(/~~~/g) || []).length;
      if (tildeCount % 2 === 1) parseInput += "\n~~~";
      // marked occasionally throws on partial markdown mid-stream (unclosed
      // fenced block, half-typed table row, etc). Catch and fall through to
      // escaped raw text so the bubble keeps showing SOMETHING instead of
      // briefly clearing while the next chunk arrives.
      let raw;
      try {
        raw = window.marked ? window.marked.parse(parseInput) : parseInput;
      } catch (e) {
        raw = "<pre>" + this.escape(text) + "</pre>";
      }
      if (!window.DOMPurify) return raw;
      const safe = window.DOMPurify.sanitize(raw, {
        USE_PROFILES: { html: true, mathMl: true },          // KaTeX may emit MathML
        FORBID_TAGS: ["style", "iframe", "form", "object", "embed"],
        FORBID_ATTR: ["style", "formaction"],
        ADD_ATTR: ["aria-hidden"],                            // KaTeX uses these
      });
      // If sanitize returned a string MUCH shorter than the input text (e.g.
      // partial code-block syntax tripped the parser and everything past it
      // got stripped), fall back to a plain-pre rendering so we don't display
      // a half-empty bubble mid-stream. Threshold is heuristic — only kicks
      // in on dramatic loss.
      if (safe.length < text.length * 0.25 && text.length > 80) {
        return "<pre>" + this.escape(text) + "</pre>";
      }
      // Math: render $...$ / $$...$$ via KaTeX auto-render. KaTeX runs after
      // DOMPurify (its output is trusted vendor HTML, no need to re-sanitize).
      const tmp = document.createElement("div");
      tmp.innerHTML = safe;
      if (window.renderMathInElement && window.katex) {
        try {
          window.renderMathInElement(tmp, {
            delimiters: [
              { left: "$$", right: "$$", display: true },
              { left: "$",  right: "$",  display: false },
              { left: "\\(", right: "\\)", display: false },
              { left: "\\[", right: "\\]", display: true },
            ],
            throwOnError: false,
            ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          });
        } catch (e) { /* malformed math falls through as plain text */ }
      }
      this._linkifyFilePaths(tmp);
      return tmp.innerHTML;
    },

    // Path-shaped strings inside inline <code> become clickable `.file-link`
    // anchors. Click is handled by chat-body delegation -> openByPathToasted.
    // Aggressive match (anything ending in .ext) — 404 on click degrades to a
    // toast, no need to be conservative here. Skips fenced code (<pre><code>)
    // since those are usually whole-file snippets, not references.
    _linkifyFilePaths(rootEl) {
      // path/with/slashes.ext  OR  bare.ext  (+ optional :line[:col])
      // Ext must START with a letter to avoid eating version strings like 1.2.3.
      // \p{L}\p{N} (unicode flag) so Chinese/Japanese filenames also match.
      const RE = /^([\p{L}\p{N}_@./~+-]+\.[A-Za-z][A-Za-z0-9]{0,9})(?::(\d+))?(?::\d+)?$/u;
      const toRel = (p) => this._normalizeArchivePath(p);
      // 1) inline <code> whose text looks like a path
      const codes = rootEl.querySelectorAll("code");
      for (const code of codes) {
        if (code.closest("pre")) continue;
        if (code.querySelector("a")) continue;
        const raw = (code.textContent || "").trim();
        if (!raw || raw.length > 200) continue;
        const m = raw.match(RE);
        if (!m) continue;
        const path = toRel(m[1]);
        if (!path) continue;
        const a = document.createElement("a");
        a.className = "file-link";
        a.href = "#";
        a.dataset.path = path;
        if (m[2]) a.dataset.line = m[2];
        a.textContent = raw;
        code.textContent = "";
        code.appendChild(a);
        code.classList.add("has-file-link");
      }
      // 2) markdown links — [label](path/to/file.md) — marked renders as <a>.
      // Convert to .file-link if href is relative (no protocol) and matches the
      // path regex; otherwise leave the anchor alone (real URLs stay clickable).
      const anchors = rootEl.querySelectorAll("a[href]");
      for (const a of anchors) {
        if (a.classList.contains("file-link")) continue;
        let href = a.getAttribute("href") || "";
        if (!href || href.startsWith("#")) continue;
        if (/^[a-z]+:/i.test(href)) continue;          // http: / https: / mailto: / etc.
        // marked / the model may URL-encode the href (e.g. Chinese filenames
        // come through as %E8%B5%84...). Decode so the regex + backend list
        // lookup operate on the raw UTF-8 form.
        try { href = decodeURIComponent(href); } catch (_) { /* malformed → leave as-is */ }
        const m = href.match(RE);
        if (!m) continue;
        const path = toRel(m[1]);
        if (!path) continue;
        a.classList.add("file-link");
        a.setAttribute("href", "#");
        a.dataset.path = path;
        if (m[2]) a.dataset.line = m[2];
      }
    },

    // Delegated click handler on the chat body for `.file-link` anchors
    // produced by _linkifyFilePaths or the tool-bubble template.
    onChatClick(ev) {
      const a = ev.target.closest && ev.target.closest("a[href]");
      if (!a) return;
      // .file-link → open via archive preview
      if (a.classList.contains("file-link")) {
        ev.preventDefault();
        const path = a.dataset.path || "";
        if (path) this.openByPathToasted(path);
        return;
      }
      // Safety net: any other anchor with a relative href shouldn't trigger
      // a same-origin navigation (which would unload the SPA). Try to treat
      // it as a file path; fall back to a toast if we can't parse.
      let href = a.getAttribute("href") || "";
      if (!href || href.startsWith("#")) return;
      if (/^[a-z]+:/i.test(href)) return;             // real protocol → let browser handle
      try { href = decodeURIComponent(href); } catch (_) { /* malformed → leave as-is */ }
      ev.preventDefault();
      // Try ROOT-relative normalization first (absolute under ROOT, or ROOT
      // basename duplicated as prefix). If that returns "" (e.g. /etc/passwd),
      // fall back to the raw href minus leading slash so the user at least
      // gets a "not found" toast instead of silent navigation.
      let p = this._normalizeArchivePath(href);
      if (!p) p = href.replace(/^\/+/, "");
      this.openByPathToasted(p);
    },

    // openFile silently no-ops on 404 (designed for tree clicks where the
    // entry came from the API). For chat-link clicks the path comes from
    // model output and may not exist — surface the failure as a toast.
    async openByPathToasted(path) {
      // HEAD-equivalent check via list on the parent dir is fragile (binary
      // files, images etc. don't go through /api/files/read). Just delegate
      // to openFile and let it set previewMode='unsupported' / pdf / img,
      // but pre-check existence with a cheap list on the parent so we can
      // toast cleanly when the path is fabricated.
      const parent = path.includes("/") ? path.split("/").slice(0, -1).join("/") : "";
      const name = path.split("/").pop();
      try {
        const r = await fetch("/api/files/list?path=" + encodeURIComponent(parent),
          { headers: this.hdr() });
        if (!r.ok) {
          this.toast(this.lang === "zh" ? `文件不存在：${path}` : `Not found: ${path}`, "warn");
          return;
        }
        const d = await r.json();
        const hit = (d.entries || []).find(e => e.name === name);
        if (!hit) {
          this.toast(this.lang === "zh" ? `文件不存在：${path}` : `Not found: ${path}`, "warn");
          return;
        }
        if (hit.is_dir) {
          this.toast(this.lang === "zh" ? `这是目录：${path}` : `Is a directory: ${path}`, "warn");
          return;
        }
        await this.openFile({ path, name });
      } catch (e) {
        this.toast(this.lang === "zh" ? `打开失败：${path}` : `Open failed: ${path}`, "warn");
      }
    },

    async login() {
      this.loginErr = "";
      this.token = this.tokenInput.trim();
      try {
        const r = await fetch("/api/files/list?path=", { headers: this.hdr() });
        if (!r.ok) throw new Error("token 错误");
        localStorage.setItem("muselab_token", this.token);
        this.authed = true;
        this.loadPrefs();
        await this.loadRoot();
        await this.initSessions();
        this.fetchStats();
        // Restore the preview file the user was looking at before refresh.
        // openFile is idempotent on tabs[] (won't duplicate); if the file
        // no longer exists we silently no-op (no toast — refresh restoration).
        if (this._pendingPreviewSelected) {
          const path = this._pendingPreviewSelected;
          this._pendingPreviewSelected = null;
          this.openFile({ path, name: path.split("/").pop() })
              .catch(() => { /* file went away — nothing to do */ });
        }
      } catch (e) { this.loginErr = e.message; }
    },

    logout() {
      localStorage.removeItem("muselab_token");
      location.reload();
    },

    hdr() { return { "X-Auth-Token": this.token }; },

    // ===== unified fetch wrapper =====
    // Consolidates the ~60 hand-written fetch calls. Auto-attaches token
    // header, JSON-encodes bodies, decodes JSON / text response, and returns
    // a shape the caller can destructure regardless of success. On non-OK
    // response or network failure, returns { ok: false, status, error } —
    // callers that want auto-toast use api(..., { toastError: true }).
    //
    // Signature:
    //   api(path, opts?)              — GET by default
    //   api(path, { method, json })   — JSON POST/PUT/PATCH with auto serialize
    //   api(path, { method, body })   — raw body (FormData / string / blob)
    //   api(path, { method, query })  — object → ?k=v&... appended to path
    //   api(path, { headers })        — merged on top of token header
    //   api(path, { responseType })   — "json" (default) | "text" | "blob"
    //   api(path, { toastError })     — true → on failure pop an error toast
    async api(path, opts = {}) {
      const method = (opts.method || "GET").toUpperCase();
      const headers = { ...this.hdr(), ...(opts.headers || {}) };
      let body = opts.body;
      if (opts.json !== undefined) {
        headers["Content-Type"] = headers["Content-Type"] || "application/json";
        body = JSON.stringify(opts.json);
      }
      let url = path;
      if (opts.query) {
        const qs = new URLSearchParams(
          Object.entries(opts.query).filter(([_, v]) => v != null && v !== "")
        ).toString();
        if (qs) url += (url.includes("?") ? "&" : "?") + qs;
      }
      let r;
      try {
        r = await fetch(url, { method, headers, body });
      } catch (e) {
        if (opts.toastError) this.toast(
          (this.lang === "zh" ? "网络错误：" : "Network error: ") + e.message,
          "error", 4000);
        return { ok: false, status: 0, error: e.message };
      }
      const rt = opts.responseType || "json";
      let data;
      try {
        if (rt === "json") data = await r.json();
        else if (rt === "text") data = await r.text();
        else if (rt === "blob") data = await r.blob();
        else data = null;
      } catch {
        data = null;
      }
      if (!r.ok) {
        if (opts.toastError) {
          const msg = (data && data.detail) || r.statusText || `HTTP ${r.status}`;
          this.toast(
            (this.lang === "zh" ? "请求失败：" : "Request failed: ") + msg,
            "error", 4000);
        }
        return { ok: false, status: r.status, data, error: (data && data.detail) || r.statusText };
      }
      return { ok: true, status: r.status, data };
    },

    // ===== toast =====
    // `action` is optional: { label: "撤销", onClick: () => {...} } — renders
    // a button inside the toast. Clicking it runs onClick and dismisses.
    // Convenience: bilingual error toast for common "<verb> failed: <body>"
    // patterns. Call sites used to inline `this.toast("保存失败：" + …, "error")`
    // which gave English users a Chinese-only message. Pass the verb key
    // ("save" / "delete" / "rename" / "upload" / "create" / "load") and the
    // raw error body; we render the right prefix for the user's lang.
    errToast(verbKey, body) {
      const zhPrefix = ({
        save: "保存失败：", delete: "删除失败：", rename: "重命名失败：",
        upload: "上传失败：", create: "创建失败：", load: "加载失败：",
        copy: "复制失败：", read: "无法读取文件：", generic: "失败：",
      })[verbKey] || "失败：";
      const enPrefix = ({
        save: "Save failed: ", delete: "Delete failed: ",
        rename: "Rename failed: ", upload: "Upload failed: ",
        create: "Create failed: ", load: "Load failed: ",
        copy: "Copy failed: ", read: "Cannot read file: ",
        generic: "Failed: ",
      })[verbKey] || "Failed: ";
      const prefix = this.lang === "zh" ? zhPrefix : enPrefix;
      this.toast(prefix + (body || ""), "error");
    },
    toast(msg, type = "info", timeout = null, action = null) {
      // Default timeout depends on severity: errors need to stay long
      // enough to read (and copy if needed); info/success can fade fast.
      // Explicit timeout arg always wins (1500ms for "copied" toasts etc).
      if (timeout === null) {
        timeout = type === "error" ? 6000 : 3000;
      }
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type, action });
      if (timeout) setTimeout(() => this.dismissToast(id), timeout);
    },
    dismissToast(id) { this.toasts = this.toasts.filter(t => t.id !== id); },
    runToastAction(t) {
      try { t.action && t.action.onClick && t.action.onClick(); }
      finally { this.dismissToast(t.id); }
    },

    // ===== modal =====
    confirm({ title, body = "", okText, cancelText, danger = false }) {
      title = title || this.t("btn.confirm");
      okText = okText || this.t("btn.confirm");
      cancelText = cancelText || this.t("btn.cancel");
      return new Promise((resolve) => {
        this.modal = {
          show: true, title, body, input: null,
          okText, cancelText, danger,
          confirm: () => { this.modal.show = false; resolve(true); },
          cancel: () => { this.modal.show = false; resolve(false); },
        };
      });
    },
    prompt({ title, body = "", placeholder = "", value = "", okText, cancelText }) {
      title = title || (this.lang === "zh" ? "输入" : "Input");
      okText = okText || this.t("btn.confirm");
      cancelText = cancelText || this.t("btn.cancel");
      return new Promise((resolve) => {
        this.modal = {
          show: true, title, body, input: value,
          okText, cancelText, danger: false,
          confirm: () => { const v = this.modal.input; this.modal.show = false; resolve(v); },
          cancel: () => { this.modal.show = false; resolve(null); },
        };
        this.$nextTick(() => { if (this.$refs.modalInput) this.$refs.modalInput.focus(); });
      });
    },

    // Hard reload escape hatch. The hairline progress / streaming flag /
    // EventSource state are all in-memory; if any of them get wedged (rare
    // but it happens), there's no graceful in-app reset. Reload nukes
    // everything, then loadPrefs restores currentId / openTabIds / preview
    // selection / mobileTab from localStorage, and the SSE auto-reconnect
    // path picks up any still-running backend turn from active sidecar.
    // The toast is more than UX polish: on slow networks the user might
    // tap before the unload finishes, so a visible "正在刷新…" confirms
    // the click registered.
    reloadApp() {
      // Best-effort: persist current state first in case savePrefs hasn't
      // run recently (e.g. user has been streaming for a while and prefs
      // didn't change). Cheap, idempotent.
      try { this.savePrefs(); } catch (_) {}
      this.toast(this.lang === "zh" ? "正在刷新…" : "Reloading…", "info", 1500);
      // Slight delay lets the toast render before the page tears down.
      setTimeout(() => { location.reload(); }, 150);
    },

    // ===== prefs =====
    savePrefs() {
      // Preview-pane state (tabs, selected) persists too so a refresh restores
      // the exact files the user was looking at — matches the chat-tab strip's
      // behavior via openTabIds.
      localStorage.setItem("muselab_prefs", JSON.stringify({
        model: this.model, permission: this.permission,
        currentId: this.currentId,
        openTabIds: this.openTabIds,
        previewTabs: this.tabs.map(t => ({ path: t.path, name: t.name })),
        previewSelected: this.selected,
        expanded: Array.from(this.expanded),
        leftOpen: this.leftOpen, rightOpen: this.rightOpen,
        leftWidth: this.leftWidth, rightWidth: this.rightWidth,
        showHidden: this.showHidden,
        openFilesCollapsed: this.openFilesCollapsed,
        openFilesHeight: this.openFilesHeight,
        // Mobile-only: remember which of the 3 tabs (files / preview / chat)
        // the user was last on so a PWA close+reopen lands them in the right
        // place. Without this, restoring `previewSelected` triggers openFile
        // which auto-switches to "preview" — meaning every reopen dumps the
        // user on the preview tab even if they were chatting before.
        mobileTab: this.mobileTab,
      }));
    },
    loadPrefs() {
      try {
        const p = JSON.parse(localStorage.getItem("muselab_prefs") || "{}");
        if (p.model) this.model = p.model;
        if (p.permission) this.permission = p.permission;
        if (typeof p.leftOpen === "boolean") this.leftOpen = p.leftOpen;
        if (typeof p.rightOpen === "boolean") this.rightOpen = p.rightOpen;
        if (typeof p.leftWidth === "number") this.leftWidth = p.leftWidth;
        if (typeof p.rightWidth === "number") this.rightWidth = p.rightWidth;
        if (typeof p.showHidden === "boolean") this.showHidden = p.showHidden;
        if (p.currentId) this.currentId = p.currentId;
        if (Array.isArray(p.openTabIds)) this.openTabIds = p.openTabIds;
        // Preview tabs — restore the strip; the actual content fetch happens
        // lazily when the user clicks back to one (or via restorePreviewSelected
        // which runs once after login).
        if (Array.isArray(p.previewTabs)) this.tabs = p.previewTabs;
        if (typeof p.previewSelected === "string") this._pendingPreviewSelected = p.previewSelected;
        // Stash the mobile tab choice in a "pending" slot — actually applying
        // it has to wait until after _bootApp's openFile(previewSelected)
        // restoration runs, because openFile force-switches to "preview" on
        // mobile. _bootApp's tail re-applies _pendingMobileTab over that.
        if (typeof p.mobileTab === "string"
            && ["files", "preview", "chat"].includes(p.mobileTab)) {
          this._pendingMobileTab = p.mobileTab;
        }
        if (typeof p.openFilesCollapsed === "boolean") this.openFilesCollapsed = p.openFilesCollapsed;
        // null = auto-fit; only restore an explicit user override.
        if (typeof p.openFilesHeight === "number" && p.openFilesHeight > 60) {
          this.openFilesHeight = p.openFilesHeight;
        } else if (p.openFilesHeight === null) {
          this.openFilesHeight = null;
        }
        this._pendingExpanded = p.expanded || [];
      } catch {}
    },

    async fetchContextInfo() {
      const { ok, data } = await this.api("/api/chat/context-info");
      if (!ok || !data) return;
      data._fetched = true;
      this.contextInfo = data;
      // First successful load: if the user hasn't configured any provider,
      // pop the Settings drawer so they can fix it before trying to chat.
      // _providerCheckDone gate ensures we don't re-pop on heartbeat
      // reconnects or polling refreshes.
      if (!this._providerCheckDone) {
        this._providerCheckDone = true;
        if (!data.has_any_provider && !this.settings.show) {
          this.openSettings();
        }
      }
    },

    async fetchStats() {
      try {
        const r = await fetch("/api/chat/usage", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          this.stats = { ...this.stats, total_cost_usd: d.total_cost_usd, total_messages: d.total_messages };
        }
      } catch {}
      try {
        const r = await fetch("/api/chat/mcp", { headers: this.hdr() });
        if (r.ok) this.mcp = await r.json();
      } catch {}
      try {
        const r = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r.ok) {
          this.availableModels = (await r.json()).models || [];
          this._rebindModelSelect();
        }
      } catch {}
    },

    // Backwards-compat wrapper — call _rebindSelect("model") directly.
    async _rebindModelSelect() { await this._rebindSelect("model"); },

    // Model switch:
    //   - empty session: PATCH model in place (no point in creating an empty fork)
    //   - session with messages: confirm modal "切换模型需要新建会话" —
    //     confirm → create new session with chosen model, jump to it.
    //     cancel  → revert dropdown.
    async onModelChange() {
      const newM = this.model;
      if (!this.currentId) return;
      const cur = this.sessions.find(s => s.id === this.currentId);
      const oldM = cur ? cur.model : "";
      if (newM === oldM) return;

      // Decide empty vs has-messages from the PERSISTED message_count, not
      // this.messages.length. The in-memory array can be temporarily empty
      // during a race (session metadata loaded before messages stream in),
      // which would wrongly take the silent path on a session that actually
      // has history.
      const persistedCount = (cur && typeof cur.message_count === "number")
        ? cur.message_count
        : this.messages.length;

      // Empty session — switch in place (no point creating an empty fork).
      // Still toast so the user gets visual confirmation the switch happened.
      if (persistedCount === 0) {
        try {
          const r = await fetch("/api/chat/sessions/" + this.currentId, {
            method: "PATCH",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ model: newM }),
          });
          if (!r.ok) {
            this.model = oldM;
            this.toast(this.t("slash.failed"), "error");
            return;
          }
          await this.refreshSessions();
          this.savePrefs();
          const label = this.modelLabel(newM);
          this.toast(this.lang === "zh"
            ? `已切到 ${label}（空会话，无需新建）`
            : `Switched to ${label} (empty session, no fork needed)`,
            "success", 1800);
        } catch (e) {
          this.model = oldM;
          this.toast(this.t("slash.failed"), "error");
        }
        return;
      }

      // Session has history — confirm + create new.
      const label = this.modelLabel(newM);
      const ok = await this.confirm({
        title: this.t("model.switch_title"),
        body: this.t("model.switch_body", { label }),
        okText: this.t("model.switch_new"),
      });
      if (!ok) {
        this.model = oldM;     // revert dropdown
        return;
      }
      try {
        const r = await fetch("/api/chat/sessions", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ name: "", model: newM }),
        });
        if (!r.ok) {
          this.model = oldM;
          this.toast(this.t("slash.failed"), "error");
          return;
        }
        const meta = await r.json();
        await this.refreshSessions();
        // Model-fork creates a brand-new session — wire it up as a tab the
        // same way newSession() does (tabState + openTabIds + activate).
        this.currentId = meta.id;
        const newSt = this._ensureTabState(meta.id);
        newSt.messages.length = 0;
        newSt._loaded = true;
        this._activateTabState(meta.id);
        if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
        this._fetchTabUsage(meta.id);
        this.savePrefs();
        this.toast(this.t("model.new_session_ok", { label }), "success", 2000);
      } catch (e) {
        this.model = oldM;
        this.toast(this.t("slash.failed"), "error");
      }
    },

    // Effort changes don't fork or corrupt the transcript (they only affect
    // future-turn budget), so no confirm modal — PATCH in place, toast, done.
    // Backend disconnects the cached client so the next turn rebuilds with
    // the new value.
    async onEffortChange() {
      if (!this.currentId) return;
      const e = this.effort || "";
      try {
        const r = await fetch("/api/chat/sessions/" + this.currentId, {
          method: "PATCH",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ effort: e }),
        });
        if (!r.ok) throw new Error(await r.text());
        const label = this.t("effort." + (e || "auto"));
        this.toast(this.t("effort.changed", { label }), "info", 1800);
        // Mirror into the session list cache so tab-switch sees the right value.
        const cur = this.sessions.find(s => s.id === this.currentId);
        if (cur) cur.effort = e;
      } catch (err) {
        this.toast(this.lang === "zh" ? "切换失败" : "Switch failed", "error");
      }
    },

    modelGroups() {
      const map = {};
      for (const m of this.availableModels) {
        if (!map[m.group]) map[m.group] = { name: m.group, items: [] };
        map[m.group].items.push(m);
      }
      return Object.values(map);
    },

    currentModelLabel() {
      const m = this.availableModels.find(x => x.model === this.model);
      if (m) return m.label;
      // fallback：直接显示 model id
      return this.model || "AI";
    },

    // ===== sessions =====
    // A fresh per-tab state slot. Object refs (messages, sessionUsage) live
    // forever — we mutate in place so Alpine's reactivity stays bound.
    _blankTabState() {
      return {
        messages: [],
        sessionUsage: { input_tokens: 0, output_tokens: 0,
                         cache_read_tokens: 0, cache_creation_tokens: 0,
                         context_limit: 0, context_used: 0, context_used_pct: 0 },
        streaming: false,
        es: null,
        streamingModel: "",
        streamElapsed: 0,
        _streamTimer: null,
        _streamStartedAt: 0,
        _loaded: false,   // set true after first loadSession populates messages
      };
    },
    _ensureTabState(id) {
      if (!this.tabState[id]) this.tabState[id] = this._blankTabState();
      return this.tabState[id];
    },
    // Pull the per-session context meter (input/output tokens, limit, %)
    // from the backend and merge it into tabState[sid].sessionUsage. Limit
    // is model-specific (opus/sonnet/haiku → 200k, others → 128k default),
    // so we MUST refresh whenever the active model could differ from what
    // last produced the cached numbers — that includes brand-new sessions
    // which would otherwise sit at the _blankTabState default of 128k.
    async _fetchTabUsage(sid) {
      if (!sid) return;
      const st = this._ensureTabState(sid);
      // Prefer the model that's currently bound to the session on the server
      // (sessions list metadata); fall back to root this.model if absent.
      const sessMeta = this.sessions.find(s => s.id === sid);
      const model = (sessMeta && sessMeta.model) || this.model || "";
      try {
        const ur = await fetch(`/api/chat/usage/${sid}?model=${encodeURIComponent(model)}`,
                                 { headers: this.hdr() });
        if (!ur.ok) return;
        const u = await ur.json();
        Object.assign(st.sessionUsage, u);
        if (sid === this.currentId) this.sessionUsage = st.sessionUsage;
      } catch (e) { /* non-fatal */ }
    },

    // Mirror this tab's state into root fields so the UI sees it. Object refs
    // (messages, sessionUsage) are shared — mutations from anywhere reflect.
    // Primitives (streaming, es, ...) must be copied; they get re-synced as
    // the active stream progresses.
    _activateTabState(id) {
      const st = this._ensureTabState(id);
      this.messages = st.messages;
      this.sessionUsage = st.sessionUsage;
      this.streaming = st.streaming;
      this.es = st.es;
      this.streamingModel = st.streamingModel;
      this.streamElapsed = st.streamElapsed;
      this._streamTimer = st._streamTimer;
      this._streamStartedAt = st._streamStartedAt;
      // Tab cache may hold an out-of-date sessionUsage (e.g. backend table
      // updated since we last polled). Fire-and-forget a re-fetch so the
      // meter reflects current truth without blocking the UI swap.
      this._fetchTabUsage(id);
    },

    async initSessions() {
      await this.refreshSessions();
      if (!this.sessions.length) {
        const s = await this.newSession();
        this.currentId = s.id;
      } else if (!this.sessions.find(x => x.id === this.currentId)) {
        this.currentId = this.sessions[0].id;
      }
      // Reconcile openTabIds (restored from prefs) with what still exists on
      // the server: drop tabs whose session was deleted, then ensure currentId
      // is in the list. Other tabs are lazy-loaded on first switch.
      const validIds = new Set(this.sessions.map(s => s.id));
      this.openTabIds = (this.openTabIds || []).filter(id => validIds.has(id));
      if (!this.openTabIds.includes(this.currentId)) {
        this.openTabIds.push(this.currentId);
      }
      this._activateTabState(this.currentId);
      const st = this._ensureTabState(this.currentId);
      if (!st._loaded) {
        await this.loadSession(this.currentId);
        st._loaded = true;
      }
      this.savePrefs();
    },
    async refreshSessions() {
      const { ok, data } = await this.api("/api/chat/sessions");
      if (!ok) return;
      const raw = (data && data.sessions) || [];
      // Defensive: drop any entry without a usable id. Alpine x-for :key
      // bindings (session-picker, history popup) use `s.id`; an undefined
      // key crashes alpine morph with "Cannot read properties of undefined
      // (reading 'after')".
      this.sessions = raw.filter(s => s && typeof s.id === "string" && s.id);
      // <select x-model="currentId"> needs a tickle to sync display when
      // sessions populate (same Alpine-x-model-on-dynamic-options race).
      await this._rebindSelect("currentId");
    },

    // Generic select-rebind tickle (model + currentId share this). Flipping
    // to '' then back across two ticks forces Alpine to re-evaluate x-model.
    async _rebindSelect(field) {
      const cur = this[field];
      if (!cur) return;
      await this.$nextTick();
      this[field] = "";
      await this.$nextTick();
      this[field] = cur;
    },
    async newSession() {
      // No longer stops streams in OTHER tabs — each tab has its own ES in
      // tabState[id].es. The new session starts fresh in its own tab.
      // Default name uses the user's BROWSER-LOCAL clock — the backend
      // generated it from datetime.now() which is the VPS's UTC, so users
      // in non-UTC timezones saw "新会话 05-19 08:26" when their wall
      // clock said 16:26. Generating the timestamp client-side fixes that
      // for every user without a server-side timezone config.
      const now = new Date();
      const pad = n => String(n).padStart(2, "0");
      const stamp = `${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
                    `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      const prefix = this.lang === "zh" ? "新会话 " : "New chat ";
      const r = await fetch("/api/chat/sessions", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name: prefix + stamp, model: this.model }),
      });
      const meta = await r.json();
      // Optimistic local update — UI switches the moment the backend
      // confirms the session ID, instead of waiting for two extra HTTP
      // roundtrips. refreshSessions/_fetchTabUsage run in the background
      // and will reconcile when they return (~100-200ms later); their
      // result is consistent with what we just inserted, so the user
      // sees zero flicker. Previously this was three sequential awaits
      // (~300-500ms perceived lag for "new chat" click).
      const existsLocally = this.sessions.some(s => s.id === meta.id);
      if (!existsLocally) {
        this.sessions = [meta, ...this.sessions];
      }
      this.currentId = meta.id;
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      this.savePrefs();
      // Background reconciliation (fire-and-forget).
      this.refreshSessions();
      this._fetchTabUsage(meta.id);
      return meta;
    },

    // Create a curator-mode session and kick off the archive-tidying
    // workflow. Confirms with the user first (this creates a NEW
    // session — they might not want one), then POSTs to
    // /api/sessions/organize, switches to it, and auto-sends the
    // bilingual initial message to start the curator workflow.
    async startOrganize() {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "整理档案" : "Organize archive",
        body: zh
          ? "将新建一个 [整理档案] 会话，让 Muse 扫描并提出建议。每一步都会等你确认才执行。"
          : "Will create a new [Organize] session — Muse scans and proposes changes, waiting for your confirmation at each step.",
        okText: zh ? "开始" : "Start",
      });
      if (!ok) return;
      const r = await fetch("/api/chat/sessions/organize", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ model: this.model }),
      });
      if (!r.ok) {
        this.toast(this.lang === "zh"
          ? "创建失败：" + (await r.text())
          : "Create failed: " + (await r.text()), "error", 4000);
        return;
      }
      const meta = await r.json();
      await this.refreshSessions();
      this.currentId = meta.id;
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      this._fetchTabUsage(meta.id);
      this.savePrefs();
      // Auto-send the curator's initial prompt — the system prompt tells
      // Muse to begin the 5-step workflow on first message.
      const lang = this.lang === "en" ? "en" : "zh";
      const initialMsg = (meta.initial_message && meta.initial_message[lang])
        || meta.initial_message?.zh || "开始";
      this.input = initialMsg;
      // Defer a tick so the new session's tabState is fully wired into
      // Alpine reactivity before send() reads `this.currentId`.
      this.$nextTick(() => { this.send(); });
    },

    // ===== tabs =====
    // Switch to (and if needed open) a tab. Used by the picker dropdown to
    // promote a history session into a tab.
    async openTab(id, makeCurrent = true) {
      if (!this.openTabIds.includes(id)) this.openTabIds.push(id);
      if (makeCurrent && id !== this.currentId) {
        this.currentId = id;
        await this.switchSession();
      }
      this.savePrefs();
    },

    // Close a tab. If it was active, hop to a neighbor; if the strip would be
    // empty, create a fresh session so the user always has somewhere to type.
    // Also closes any in-flight stream for the closed tab and drops its
    // tabState entry — leaving it around would leak EventSources.
    // NOTE: do NOT rename to closeTab — that name is taken by the file-preview
    // tab strip's closer (see line ~2640). JS object literals: later definition
    // wins, so when this was named closeTab, file-preview's overrode ours and
    // every × click in the chat tab strip silently no-op'd.
    async closeChatTab(id, ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const idx = this.openTabIds.indexOf(id);
      if (idx < 0) return;
      const wasActive = this.currentId === id;
      const st = this.tabState[id];
      // Don't offer undo for a tab whose stream is in flight — we'd have to
      // re-attach to a live EventSource which gets hairy fast. Tearing it
      // down and silently swallowing the in-flight reply is the lesser evil
      // (and the user clicked × explicitly).
      const wasStreaming = !!(st && st.streaming);
      if (st) {
        if (st.es) { try { st.es.close(); } catch {} }
        if (st._streamTimer) clearInterval(st._streamTimer);
      }
      this.openTabIds.splice(idx, 1);
      if (wasActive) {
        if (this.openTabIds.length) {
          const nextIdx = Math.min(idx, this.openTabIds.length - 1);
          this.currentId = this.openTabIds[nextIdx];
          await this.switchSession();
        } else {
          await this.newSession();
        }
      }
      if (this.tabState[id]) delete this.tabState[id];
      this.savePrefs();
      // Closing a tab is a cheap, reversible action (session is still in
      // history picker / sidebar). The previous toast-with-undo was noise
      // for every close click; killed by user request.
    },

    // Inline rename — tab name -> <input>. Enter saves, Esc cancels, blur saves.
    startRenameTab(id) {
      const s = this.sessions.find(x => x.id === id);
      if (!s) return;
      this.renamingTabId = id;
      this.renameDraft = s.name || "";
      this.$nextTick(() => {
        // x-show keeps every tab's <input> mounted — scope the selector to
        // THIS tab's data-tid so we focus the right one.
        const el = document.querySelector(
          `.chat-tab-rename-input[data-tid="${CSS.escape(id)}"]`);
        if (el) { el.focus(); el.select(); }
      });
    },
    async commitRenameTab() {
      const id = this.renamingTabId;
      const name = (this.renameDraft || "").trim();
      this.renamingTabId = "";
      const draft = this.renameDraft;
      this.renameDraft = "";
      if (!id || !name) return;
      const cur = this.sessions.find(x => x.id === id);
      if (!cur || cur.name === name) return;
      const r = await fetch("/api/chat/sessions/" + id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) { await this.refreshSessions(); this.toast(this.t("toast.renamed"), "success"); }
      else { this.toast(this.lang === "zh" ? "重命名失败" : "Rename failed", "error"); }
    },
    cancelRenameTab() { this.renamingTabId = ""; this.renameDraft = ""; },

    // Defensive helpers used by inline templates — keep them tiny so Alpine
    // never has to re-parse complex expressions on every reactive tick.
    isTabStreaming(tid) {
      const st = this.tabState[tid];
      return !!(st && st.streaming);
    },
    tabCtxMenuStyle() {
      const m = this.tabCtxMenu;
      return m ? `left:${m.x}px; top:${m.y}px` : "";
    },
    tabTitle(tid) {
      const s = this.sessions.find(x => x.id === tid);
      return s ? (s.name || "") : "";
    },
    // <title> driver — wired via x-effect on the root #app element. Re-runs
    // whenever any read reactive (currentId / sessions / streaming) changes.
    pageTitle() {
      const cur = this.sessions.find(s => s.id === this.currentId);
      const name = (cur && cur.name) || "";
      const prefix = this.streaming ? "● " : "";
      return name ? `${prefix}${name} · muselab` : "muselab — Meet Muse";
    },
    // ===== thinking / tool_result collapse =====
    // Default-collapse historical blocks; the currently-streaming last block
    // stays expanded. User clicks override either way.
    // Storage: top-level reactive _expandedMsgs map (keyed by uuid or _k).
    // Previously stored as `m._userExpanded` on the message object directly,
    // but Alpine v3 doesn't deep-wrap array elements — direct property set
    // didn't trigger re-render. Top-level prop + spread-assign does.
    _expandedMsgs: {},
    _msgKey(i, m) {
      if (!m) return "";
      return m.uuid || m._k || ("m-" + i);
    },
    isMsgExpanded(i, m) {
      if (!m) return true;
      const k = this._msgKey(i, m);
      if (k in this._expandedMsgs) return this._expandedMsgs[k];
      // Default: only the actively-streaming last block is expanded.
      const msgs = this.messages || [];
      return !!this.streaming && i === msgs.length - 1;
    },
    toggleMsgExpanded(m, i) {
      if (!m) return;
      const idx = (i ?? (this.messages || []).indexOf(m));
      const k = this._msgKey(idx, m);
      const cur = this.isMsgExpanded(idx, m);
      // Spread-assign so Alpine sees the replacement and re-evaluates.
      this._expandedMsgs = { ...this._expandedMsgs, [k]: !cur };
    },
    toolResultClass(i, m) {
      let cls = "tool-result";
      if (m && m.is_error) cls += " err";
      if (!this.isMsgExpanded(i, m)) cls += " collapsed";
      // Per-tool class hooks let CSS show terminal / read-gutter / web-card
      // styling only for the relevant result. Falls back to plain text.
      const kind = this.toolResultKind(m);
      if (kind) cls += " kind-" + kind;
      return cls;
    },
    toolResultSummary(m) {
      const text = (m && (m.text || m.preview)) || "";
      const lines = text.split("\n").length;
      const kind = this.toolResultKind(m);
      const suffix = this.lang === "zh" ? " 行输出" : " lines";
      // Bash gets a more useful summary (exit code surfaced) so the user
      // doesn't have to expand to see if the command succeeded.
      if (kind === "bash" && m && m.bash && typeof m.bash.exit_code === "number") {
        const ec = m.bash.exit_code;
        const tag = ec === 0
          ? (this.lang === "zh" ? "✓ 成功" : "✓ ok")
          : (this.lang === "zh" ? `✗ 退出码 ${ec}` : `✗ exit ${ec}`);
        return `${tag} · ${lines}${suffix}`;
      }
      return lines + suffix;
    },
    toolResultKind(m) {
      // Map tool_name → renderer key. Drives both CSS class and the
      // Alpine template that fires inside the expanded body.
      if (!m) return "";
      const name = m.tool_name || "";
      if (name === "Bash") return "bash";
      if (name === "Read") return "read";
      if (name === "WebFetch") return "web";
      if (name === "WebSearch") return "web";
      if (name === "Glob" || name === "Grep") return "search";
      if (name && name.startsWith("mcp__")) return "mcp";
      return "";
    },
    toolResultBodyText(m) {
      // Full body for the expanded view (or the truncation-marker tail).
      // Prefer `text` (50KB cap) over the legacy `preview` (500-char).
      if (!m) return "";
      const body = m.text || m.preview || "";
      if (m.text_truncated) {
        const suffix = this.lang === "zh"
          ? "\n\n…（输出已截断，剩余内容未传到前端）"
          : "\n\n…(output truncated — server did not forward the rest)";
        return body + suffix;
      }
      return body;
    },
    readResultLines(m) {
      // Read tool emits `   1→line one\n   2→line two\n...`. We rebuild a
      // [{n, content}] list so the template can render a line-number gutter
      // without re-splitting every render frame. Falls back to a single
      // synthetic entry when the format doesn't match (vendor wrapper,
      // mocked test, etc.).
      const body = this.toolResultBodyText(m);
      const out = [];
      const re = /^\s*(\d+)→(.*)$/;
      for (const ln of body.split("\n")) {
        const mm = ln.match(re);
        if (mm) {
          out.push({ n: Number(mm[1]), content: mm[2] });
        } else if (ln === "" && out.length) {
          // Trailing blank — Read often ends with an empty marker. Keep
          // it so spacing is faithful to the source file.
          out.push({ n: 0, content: "" });
        } else if (out.length === 0) {
          // Pre-data noise (e.g. "(Reading X lines from file Y)") — render
          // as a header line without a gutter number.
          out.push({ n: 0, content: ln });
        } else {
          // Line that doesn't match the n→ format mid-stream — append to
          // the previous content so wrapped output stays readable.
          out[out.length - 1].content += "\n" + ln;
        }
      }
      return out;
    },
    bashResultText(m) {
      // Prefer the structured parse (stdout/stderr/exit_code separated)
      // when the backend provided it; otherwise fall back to the raw body.
      if (m && m.bash) {
        const stdout = (m.bash.stdout || "").replace(/\n+$/, "");
        const stderr = (m.bash.stderr || "").replace(/\n+$/, "");
        return { stdout, stderr,
                 exit_code: m.bash.exit_code,
                 interrupted: !!m.bash.interrupted };
      }
      return { stdout: this.toolResultBodyText(m), stderr: "",
               exit_code: undefined, interrupted: false };
    },
    // ---- LCS-based line diff for Edit/Write/MultiEdit ----
    //
    // Why we ship our own and not jsdiff: muselab has a no-build-step rule
    // (vendor/ is pre-built minified blobs only). A 40-line LCS is enough
    // for the Edit-tool case (typically <100-line snippets) and avoids the
    // 20KB jsdiff dependency. We cap input length so a pathological
    // 5000-line both-sides snippet doesn't pin the main thread.
    _lineDiff(oldText, newText, capLines = 800) {
      const a = (oldText || "").split("\n");
      const b = (newText || "").split("\n");
      // Trim to cap on each side and prepend a synthetic ellipsis line so
      // the user knows we capped — better than silently dropping context.
      if (a.length > capLines) a.length = capLines;
      if (b.length > capLines) b.length = capLines;
      const m = a.length, n = b.length;
      // Build LCS table. O(m·n) space — fine for capLines² = 640k cells.
      const dp = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
      for (let i = m - 1; i >= 0; i--) {
        for (let j = n - 1; j >= 0; j--) {
          dp[i][j] = a[i] === b[j]
            ? dp[i + 1][j + 1] + 1
            : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
      }
      // Walk it to produce a unified-style op list.
      const ops = [];
      let i = 0, j = 0;
      while (i < m && j < n) {
        if (a[i] === b[j]) {
          ops.push({ op: "ctx", text: a[i] }); i++; j++;
        } else if (dp[i + 1][j] >= dp[i][j + 1]) {
          ops.push({ op: "del", text: a[i] }); i++;
        } else {
          ops.push({ op: "ins", text: b[j] }); j++;
        }
      }
      while (i < m) { ops.push({ op: "del", text: a[i++] }); }
      while (j < n) { ops.push({ op: "ins", text: b[j++] }); }
      return ops;
    },
    editDiffOps(m) {
      // Returns ops for an Edit / Write / MultiEdit tool_use. MultiEdit's
      // `edits` array is flattened into a single op list with a separator
      // op between sub-edits so the template can render section labels.
      if (!m || !m.input) return [];
      const inp = m.input;
      if (m.name === "MultiEdit" && Array.isArray(inp.edits)) {
        const out = [];
        inp.edits.forEach((e, idx) => {
          if (idx > 0) out.push({ op: "sep", text: `--- edit ${idx + 1} ---` });
          const sub = this._lineDiff(e.old_string || "", e.new_string || "");
          out.push(...sub);
        });
        return out;
      }
      if (m.name === "Write") {
        // Write creates / overwrites — show `content` as all-insertions so
        // the user sees what's about to land in the file.
        const body = inp.content || "";
        return body.split("\n").map(t => ({ op: "ins", text: t }));
      }
      // Edit (or fallback)
      return this._lineDiff(inp.old_string || "", inp.new_string || "");
    },
    // ---- error CTA dispatch ----
    errorCtaLabel(kind, cta) {
      if (cta === "open_settings") {
        return this.lang === "zh" ? "打开设置" : "Open Settings";
      }
      if (cta === "switch_model") {
        return this.lang === "zh" ? "换个模型" : "Switch model";
      }
      if (cta === "compact_or_fork") {
        return this.lang === "zh" ? "压缩对话" : "Compact session";
      }
      return this.lang === "zh" ? "重试" : "Retry";
    },
    errorCtaInvoke(m) {
      // m carries _error_kind / _error_cta. Map to the matching action —
      // we deliberately reuse existing methods to avoid duplicate codepaths.
      const cta = m && m._error_cta;
      if (cta === "open_settings") { this.openSettings(); return; }
      if (cta === "switch_model") {
        // Open the model picker if it exists; otherwise just toast hint.
        const pick = document.querySelector(".model-picker, #model-select");
        if (pick) pick.focus();
        this.toast(this.lang === "zh"
          ? "在右上模型下拉里选别的" : "Pick another model from the top dropdown",
          "info", 3500);
        return;
      }
      if (cta === "compact_or_fork") {
        this.runCompact && this.runCompact();
        return;
      }
      // Default "Retry" — reuse the existing failed-message retry path.
      if (m && m.role === "user" && m._failed) {
        this.retryFailedMessage(m);
      }
    },
    thinkingClass(i, m) {
      return this.isMsgExpanded(i, m) ? "thinking" : "thinking collapsed";
    },
    thinkingPreview(m) {
      const text = (m && m.text) || "";
      const firstLine = text.split("\n")[0] || "";
      const trimmed = firstLine.slice(0, 80);
      return trimmed + (text.length > 80 ? "…" : "");
    },
    async _refreshCtxMeter() {
      // Pull SDK ContextUsageResponse via /context-breakdown so the meter
      // shows post-compact (or any other out-of-band) state without waiting
      // for the next stream's 'done' event.
      if (!this.currentId) return;
      const { ok, data } = await this.api(
        `/api/chat/context-breakdown/${this.currentId}`);
      if (!ok || !data) return;
      const used = Math.max(0, Number(data.totalTokens || 0));
      const maxT = Math.max(0, Number(data.maxTokens
                                       || this.sessionUsage.context_limit
                                       || 200000));
      this.sessionUsage = {
        ...this.sessionUsage,
        context_used: used,
        context_limit: maxT,
        context_used_pct: maxT
          ? Math.round(used / maxT * 1000) / 10
          : 0,
      };
    },

    async showCtxBreakdown() {
      if (!this.currentId) return;
      this.ctxBreakdown = { show: true, loading: true, data: null, error: "" };
      this.ctxExpanded = {};
      const { ok, data, error, status } = await this.api(
        `/api/chat/context-breakdown/${this.currentId}`);
      this.ctxBreakdown.loading = false;
      if (ok && data) {
        this.ctxBreakdown.data = data;
      } else {
        // 409 = no live client yet (session hasn't streamed a turn).
        this.ctxBreakdown.error = status === 409
          ? (this.lang === "zh"
              ? "需要先发一条消息才能查 breakdown（SDK 要求 live client）"
              : "Send a message first — SDK breakdown needs a live client")
          : (error || (this.lang === "zh" ? "查询失败" : "Fetch failed"));
      }
    },
    // % of maxTokens used by this category — drives both the stacked bar
    // at the top of the popup and the per-row inline bar.
    ctxCategoryPct(cat) {
      const max = (this.ctxBreakdown.data && this.ctxBreakdown.data.maxTokens) || 0;
      if (!max || !cat || !cat.tokens) return 0;
      return Math.min(100, (cat.tokens / max) * 100);
    },
    // Pick a category color. SDK populates cat.color for known categories;
    // fall back to a stable hash-based hue for everything else so the bar
    // segments stay distinct.
    ctxCategoryColor(cat) {
      if (cat && cat.color) return cat.color;
      const n = (cat && cat.name) || "?";
      let h = 0;
      for (let i = 0; i < n.length; i++) h = (h * 31 + n.charCodeAt(i)) >>> 0;
      return `hsl(${h % 360}, 55%, 55%)`;
    },
    ctxFormatTokens(n) {
      if (!n) return "0";
      if (n >= 1000) return (n / 1000).toFixed(1) + "K";
      return String(n);
    },
    // Map a category name to its detailed sub-list. SDK returns
    // memoryFiles / mcpTools / agents as separate top-level arrays; we
    // surface them under whichever category row carries the same totals.
    // Match is fuzzy (lowercased + stripped of separators) since the SDK
    // labels may localize the category name.
    ctxRowChildren(name) {
      const data = this.ctxBreakdown.data || {};
      const key = String(name || "").toLowerCase().replace(/[\s_-]/g, "");
      if (key.includes("memory")) return data.memoryFiles || [];
      if (key.includes("mcp")) return data.mcpTools || [];
      if (key.includes("agent")) return data.agents || [];
      return [];
    },
    ctxToggleRow(name) {
      if (!this.ctxRowChildren(name).length) return;
      this.ctxExpanded[name] = !this.ctxExpanded[name];
    },

    ctxRingTitle() {
      const u = this.sessionUsage || {};
      const used = u.context_used || 0;
      const limit = u.context_limit || 0;
      const pct = u.context_used_pct || 0;
      if (this._compacting) {
        return this.lang === "zh"
          ? `📦 压缩进行中，已排队 ${this._compactQueue.length} 条`
          : `📦 Compact in progress (${this._compactQueue.length} queued)`;
      }
      if (!limit) return this.lang === "zh" ? "上下文 …" : "Context …";
      const used_s = (used / 1000).toFixed(1) + "K";
      const limit_s = limit >= 1_000_000
        ? (limit / 1_000_000).toFixed(0) + "M"
        : (limit / 1000).toFixed(0) + "K";
      const meta = (this.availableModels || []).find(m => m.model === this.model);
      const modelLabel = meta ? meta.label : this.model;
      const hint = this.lang === "zh"
        ? "（点击压缩 · 右键看拆分）"
        : "(click to compact · right-click for breakdown)";
      return `${used_s} / ${limit_s} (${pct}%) · ${modelLabel}\n${hint}`;
    },
    compactStatusLabel() {
      // Single method instead of an inline template-literal expression in
      // x-text — Alpine error handling for templated attribute expressions
      // is brittle (a thrown evaluation can corrupt reactive state and
      // surface as "Cannot read properties of undefined (reading 'after')"
      // when the next morph/transition runs). Centralising here keeps the
      // expression in real JS where defensive guards are normal.
      if (!this._compacting) {
        try { return this.ctxMeterLabel(); }
        catch { return ""; }
      }
      const q = (this._compactQueue && this._compactQueue.length) || 0;
      if (this.lang === "zh") {
        return q ? `📦 压缩中… 消息队列 ${q}` : "📦 压缩中…";
      }
      return q ? `📦 Compacting… queued ${q}` : "📦 Compacting…";
    },

    activateTab(tid) {
      if (tid === this.currentId) return;
      this.currentId = tid;
      this.switchSession();
      // Scroll the newly-active tab into view — when the strip overflows
      // horizontally (many sessions open), keyboard shortcuts / programmatic
      // activation would otherwise leave the active tab hidden off-screen.
      this.$nextTick(() => this._scrollTabIntoView(tid));
    },
    _scrollTabIntoView(tid) {
      const strip = document.querySelector(".chat-tabs-list");
      if (!strip) return;
      const tab = strip.querySelector(`.chat-tab[data-tid="${tid}"]`)
                  || Array.from(strip.querySelectorAll(".chat-tab"))[
                       this.openTabIds.indexOf(tid)];
      if (!tab) return;
      // `inline: nearest` preserves vertical scroll, only scrolls horizontally
      // if the tab isn't already visible. `block: nearest` likewise vertical.
      tab.scrollIntoView({ inline: "nearest", block: "nearest" });
    },
    onTabAuxClick(ev, tid) {
      // 1 = middle-click — close the tab.
      if (ev.button === 1) this.closeChatTab(tid);
    },

    // Long-press handlers used to live here. Removed: they ate mobile taps
    // (touchstart→timer→touchend→cleared, but the synthetic click after a
    // long-press window collided with @click.outside on the menu and
    // sometimes blocked legitimate taps on history rows). Mobile users get
    // the same actions via the inline ⋮ kebab button on each tab / row.
    onChatTabsWheel(ev) {
      // Horizontal scroll the tab strip via the vertical wheel — like editors do.
      if (ev.deltaY !== 0) ev.currentTarget.scrollLeft += ev.deltaY;
    },

    // ===== drag-to-reorder chat tabs (desktop only — HTML5 drag-and-drop) =====
    // Mobile would need a touch-based fallback; keeping scope tight for now.
    // We track which tab is being dragged in `_draggingTabId` and which tab
    // the mouse is currently over in `tabDragOverId` (drives a visual hint).
    _draggingTabId: "",
    tabDragOverId: "",
    onTabDragStart(ev, tid) {
      this._draggingTabId = tid;
      // dataTransfer must be set for Firefox to fire drag events at all.
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", tid);
      } catch (_) {}
    },
    onTabDragOver(ev, tid) {
      if (!this._draggingTabId || tid === this._draggingTabId) return;
      ev.dataTransfer.dropEffect = "move";
      this.tabDragOverId = tid;
    },
    onTabDragLeave(tid) {
      if (this.tabDragOverId === tid) this.tabDragOverId = "";
    },
    onTabDrop(ev, tid) {
      const src = this._draggingTabId;
      this._draggingTabId = "";
      this.tabDragOverId = "";
      if (!src || src === tid) return;
      const from = this.openTabIds.indexOf(src);
      const to = this.openTabIds.indexOf(tid);
      if (from < 0 || to < 0) return;
      this.openTabIds.splice(from, 1);
      this.openTabIds.splice(to, 0, src);
      this.savePrefs();
    },
    onTabDragEnd() {
      this._draggingTabId = "";
      this.tabDragOverId = "";
    },

    // ===== drag-to-reorder preview tabs (mirrors chat-tab drag, but operates
    // on `tabs` array instead of openTabIds and on file paths as the id).
    _draggingPreviewTabPath: "",
    previewDragOverPath: "",
    showPreviewTabMenu(ev, path) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const cx = (ev && ev.clientX) || 100;
      const cy = (ev && ev.clientY) || 100;
      // Overlay catches outside-clicks reliably; no need to defer mount.
      this.previewTabCtxMenu = {
        path,
        x: Math.min(cx, window.innerWidth - 220),
        y: Math.min(cy, window.innerHeight - 280),
      };
    },
    previewTabCtxMenuStyle() {
      if (!this.previewTabCtxMenu) return "";
      return `position: fixed; top: ${this.previewTabCtxMenu.y}px; left: ${this.previewTabCtxMenu.x}px;`;
    },
    async previewTabMenuAction(action) {
      const m = this.previewTabCtxMenu;
      if (!m) return;
      const path = m.path;
      this.previewTabCtxMenu = null;
      switch (action) {
        case "close":
          this.closeTab(path);
          break;
        case "closeOthers":
          this.tabs = this.tabs.filter(t => t.path === path);
          if (this.selected !== path) await this.switchTab(path);
          this.savePrefs();
          break;
        case "closeRight": {
          const idx = this.tabs.findIndex(t => t.path === path);
          if (idx >= 0) this.tabs = this.tabs.slice(0, idx + 1);
          this.savePrefs();
          break;
        }
        case "closeAll":
          this.tabs = []; this.selected = ""; this.savePrefs();
          break;
        case "reveal":
          await this.revealInTree(path);
          break;
        case "mention":
          this.insertFileMention(path);
          break;
        case "copyPath":
          navigator.clipboard?.writeText(path).then(
            () => this.toast(this.t("toast.copied") + ": " + path, "success", 1500),
            () => this.errToast("copy", this.lang === "zh"
                                            ? "需要 HTTPS"
                                            : "HTTPS required"));
          break;
      }
    },

    async onPreviewDrop(ev) {
      this.previewDragHover = false;
      this.osFileDragging = false;
      this._dragCounter = 0;
      const files = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (!files.length) return;
      // Always upload to the archive root (MUSELAB_ROOT), regardless of
      // which file is currently open in the preview pane. Earlier this
      // dropped into the previewed file's parent directory, but that
      // made it easy to accidentally pollute deep sub-folders (`health/
      // 2026-04/scans/random_screenshot.png`) when the user actually
      // wanted to triage drops from the top level first. Root is the
      // predictable target — the user can always move files later from
      // the file tree.
      for (const f of files) {
        try { await this.uploadFileTo("", f); } catch {}
      }
    },
    onPreviewTabDragStart(ev, path) {
      this._draggingPreviewTabPath = path;
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", path);
      } catch (_) {}
    },
    onPreviewTabDragOver(ev, path) {
      if (!this._draggingPreviewTabPath
          || path === this._draggingPreviewTabPath) return;
      ev.dataTransfer.dropEffect = "move";
      this.previewDragOverPath = path;
    },
    onPreviewTabDragLeave(path) {
      if (this.previewDragOverPath === path) this.previewDragOverPath = "";
    },
    onPreviewTabDrop(ev, path) {
      const src = this._draggingPreviewTabPath;
      this._draggingPreviewTabPath = "";
      this.previewDragOverPath = "";
      if (!src || src === path) return;
      const from = this.tabs.findIndex(t => t.path === src);
      const to = this.tabs.findIndex(t => t.path === path);
      if (from < 0 || to < 0) return;
      const [moved] = this.tabs.splice(from, 1);
      this.tabs.splice(to, 0, moved);
      this.savePrefs();
    },
    onPreviewTabDragEnd() {
      this._draggingPreviewTabPath = "";
      this.previewDragOverPath = "";
    },
    historyRowClass(sid) {
      return { active: sid === this.currentId, open: this.openTabIds.includes(sid) };
    },
    // The history picker popup escapes its container via position: fixed
    // (the parent .chat-tabs has overflow-x: auto which forces overflow-y to
    // also clip — an absolute-positioned popup gets cut off). We compute the
    // viewport-anchored position from the 📁 button's bounding rect at click
    // time so the popup floats just below it.
    historyPickerStyle: "",
    sessionPickerSearch: "",
    filteredSessions() {
      const q = (this.sessionPickerSearch || "").trim().toLowerCase();
      if (!q) return this.sessions;
      return this.sessions.filter(s =>
        (s.name && s.name.toLowerCase().includes(q))
        || (s.first_prompt && s.first_prompt.toLowerCase().includes(q))
      );
    },
    // Bucket the filtered list into Pinned / Today / Yesterday / Last 7d /
    // Last 30d / Earlier so a few hundred sessions stay scannable. Pinned
    // always floats to the top; the rest are based on updated_at
    // (epoch seconds — same source as the existing sort).
    groupedFilteredSessions() {
      const items = this.filteredSessions();
      if (!items.length) return [];
      const now = new Date();
      const startOfToday = new Date(now.getFullYear(), now.getMonth(),
                                     now.getDate()).getTime() / 1000;
      const startOfYesterday = startOfToday - 86400;
      const startOf7d = startOfToday - 7 * 86400;
      const startOf30d = startOfToday - 30 * 86400;
      const pinned = [], today = [], yest = [], week = [], month = [], earlier = [];
      for (const s of items) {
        if (s.pinned) { pinned.push(s); continue; }
        const t = s.updated_at || s.created_at || 0;
        if (t >= startOfToday) today.push(s);
        else if (t >= startOfYesterday) yest.push(s);
        else if (t >= startOf7d) week.push(s);
        else if (t >= startOf30d) month.push(s);
        else earlier.push(s);
      }
      const zh = this.lang === "zh";
      const groups = [];
      if (pinned.length) groups.push({ key: "pinned",
        label: zh ? "置顶" : "Pinned", items: pinned });
      if (today.length) groups.push({ key: "today",
        label: zh ? "今天" : "Today", items: today });
      if (yest.length) groups.push({ key: "yesterday",
        label: zh ? "昨天" : "Yesterday", items: yest });
      if (week.length) groups.push({ key: "week",
        label: zh ? "最近 7 天" : "Last 7 days", items: week });
      if (month.length) groups.push({ key: "month",
        label: zh ? "最近 30 天" : "Last 30 days", items: month });
      if (earlier.length) groups.push({ key: "earlier",
        label: zh ? "更早" : "Earlier", items: earlier });
      return groups;
    },
    toggleHistoryPicker(ev) {
      if (this.sessionPickerOpen) { this.sessionPickerOpen = false; return; }
      const btn = ev && ev.currentTarget;
      const rect = btn ? btn.getBoundingClientRect() : null;
      if (rect) {
        const popW = Math.min(320, window.innerWidth - 16);
        // Right-align under the button, but stay inside the viewport edges.
        let left = Math.round(rect.right - popW);
        if (left < 8) left = 8;
        const top = Math.round(rect.bottom + 4);
        this.historyPickerStyle =
          `position: fixed; top: ${top}px; left: ${left}px; width: ${popW}px;`;
      } else {
        this.historyPickerStyle = "";
      }
      this.sessionPickerOpen = true;
    },
    pickerOpenSession(sid) {
      this.sessionPickerOpen = false;
      this.openTab(sid);
    },
    pickerRowMenu(ev, sid) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      this.sessionPickerOpen = false;
      this.showTabMenu(ev, sid);
    },
    // Inline rename inside the picker row (✎ icon). MUST be fully
    // synchronous through to el.focus() — iOS Safari only opens the
    // on-screen keyboard when focus() is called within the same JS
    // tick as the click handler that received the user gesture. Any
    // await / $nextTick / setTimeout severs the chain and the user
    // sees the input appear but no keyboard. The input is already
    // mounted (x-show, not x-if) so we just need to flip the state
    // flag and call focus() in the same tick.
    pickerStartInlineRename(ev, sid) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const s = this.sessions.find(x => x.id === sid);
      if (!s) return;
      this.renamingPickerSid = sid;
      this.pickerRenameDraft = s.name || "";
      // Synchronous focus — same tick as the click. No await / nextTick.
      const el = document.querySelector(
        `.session-picker-rename-input[data-sid="${CSS.escape(sid)}"]`);
      if (el) { el.focus(); el.select(); }
    },
    async pickerCommitInlineRename() {
      const sid = this.renamingPickerSid;
      const name = (this.pickerRenameDraft || "").trim();
      this.renamingPickerSid = "";
      this.pickerRenameDraft = "";
      if (!sid || !name) return;
      const cur = this.sessions.find(x => x.id === sid);
      if (!cur || cur.name === name) return;
      const r = await fetch("/api/chat/sessions/" + sid, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) {
        cur.name = name;
        cur.auto_named = false;
      } else {
        this.toast(this.lang === "zh" ? "重命名失败" : "Rename failed", "error", 3000);
      }
    },
    pickerCancelInlineRename() {
      this.renamingPickerSid = "";
      this.pickerRenameDraft = "";
    },
    async pickerDeleteSession(sid, ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const s = this.sessions.find(x => x.id === sid);
      const name = (s && s.name) || sid.slice(0, 8);
      const ok = await this.confirm({
        title: this.lang === "zh" ? "删除会话" : "Delete session",
        body: this.lang === "zh"
          ? `要删除「${name}」吗？此操作不可恢复（CLI JSONL 也会被删除）。`
          : `Delete "${name}"? Not undoable — the CLI JSONL will be removed too.`,
        okText: this.lang === "zh" ? "删除" : "Delete",
        danger: true,
      });
      if (!ok) return;
      await this.deleteSessionById(sid);
      this.openTabIds = this.openTabIds.filter(x => x !== sid);
      if (this.tabState[sid]) delete this.tabState[sid];
      this.savePrefs();
    },

    // Right-click context menu on a tab (or a row in the session picker).
    // Also called by the mobile ⋮ kebab button (which uses normal click).
    // We DEFER the actual menu open by one tick — otherwise the click that
    // triggered showTabMenu propagates to the document during the same
    // synchronous flow, and the newly-mounted menu's @click.outside listener
    // (or any other ancestor click handler) immediately closes / re-acts on
    // the same event. setTimeout(0) lets the trigger event finish first.
    showTabMenu(ev, id) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      const cx = (ev && (ev.clientX || (ev.touches && ev.touches[0] && ev.touches[0].clientX))) || 100;
      const cy = (ev && (ev.clientY || (ev.touches && ev.touches[0] && ev.touches[0].clientY))) || 100;
      const x = Math.min(cx, window.innerWidth - 220);
      const y = Math.min(cy, window.innerHeight - 200);
      setTimeout(() => {
        this.sessionPickerOpen = false;
        this.tabCtxMenu = { id, x, y };
      }, 0);
    },
    closeTabMenu() { this.tabCtxMenu = null; },
    async menuRename(id) {
      this.closeTabMenu();
      // Inline rename input lives inside the tab DOM, so the tab must be open
      // for the input to appear. If the user right-clicked a session from the
      // history picker that isn't a tab yet, promote it first.
      if (!this.openTabIds.includes(id)) await this.openTab(id);
      this.startRenameTab(id);
    },
    async menuEditPrompt(id) {
      this.closeTabMenu();
      // editSessionPrompt() reads currentId. Borrow it briefly to target this
      // tab's session without forcing a full switch.
      const orig = this.currentId;
      this.currentId = id;
      try { await this.editSessionPrompt(); }
      finally { this.currentId = orig; }
    },
    async menuClose(id) { this.closeTabMenu(); await this.closeChatTab(id); },
    menuExportMarkdown(id) {
      this.closeTabMenu();
      if (!id) return;
      // Use a transient anchor so the browser opens the streaming Response
      // as a file download. Token goes in the query string because anchor
      // requests can't carry custom headers.
      const url = `/api/chat/sessions/${id}/export?token=`
                  + encodeURIComponent(this.token);
      const a = document.createElement("a");
      a.href = url; a.style.display = "none";
      // download attribute lets the server's Content-Disposition take
      // precedence but still hints to the browser this isn't navigation.
      a.setAttribute("download", "");
      document.body.appendChild(a);
      a.click();
      setTimeout(() => a.remove(), 200);
    },
    async menuDelete(id) {
      this.closeTabMenu();
      // Close side effects (ES / interval) on the dying tab BEFORE the
      // server delete, but defer tabState[id] cleanup until after we've
      // removed the tab from openTabIds (so x-for unmounts its DOM first).
      const st = this.tabState[id];
      if (st) {
        if (st.es) { try { st.es.close(); } catch {} }
        if (st._streamTimer) clearInterval(st._streamTimer);
      }
      // deleteSessionById handles server delete + sessions-list refresh AND
      // bumps the user to a remaining session if id was current.
      await this.deleteSessionById(id);
      this.openTabIds = this.openTabIds.filter(x => x !== id);
      if (!this.openTabIds.includes(this.currentId)) {
        this.openTabIds.push(this.currentId);
      }
      if (this.tabState[id]) delete this.tabState[id];
      this.savePrefs();
    },
    async switchSession() {
      // Mobile: any session switch implies "I want to see chat" — covers
      // every entry point at once (openTab from picker, activateTab from
      // chat-tabs strip, slash /resume, ctx-menu, programmatic newSession).
      // Earlier we only did this in openTab, which left chat-tabs taps and
      // a few other paths needing a second tap on the bottom Muse icon.
      if (window.innerWidth <= 900) this.mobileTab = "chat";
      // Switch the visible tab. We do NOT touch other tabs' streams — each
      // tab's ES is in its own tabState[id], and stream callbacks write
      // there directly. Switching is just "show that tab".
      this._activateTabState(this.currentId);
      this.savePrefs();
      const st = this._ensureTabState(this.currentId);
      if (!st._loaded) {
        await this.loadSession(this.currentId);
        st._loaded = true;
      } else {
        // Already loaded — just re-bind UI state. messages reference unchanged.
        if (this.model) {
          // Stay on the current root model; no auto-update needed.
        }
        this.atBottom = true;
        this.scrollToBottom(true);
        this.$nextTick(() => this.highlightCode(".chat-body"));
      }
    },
    // Background-completion hook: after loadSession populates the
    // JSONL-derived history, ask the backend whether this session has
    // an in-flight turn still running. If yes, transparently
    // reconnect to the broadcast — `send({reconnect: true})` opens
    // an empty-prompt SSE to the same endpoint, and the backend's
    // reconnect mode replays the existing event buffer then streams
    // live. User sees the reply continue right where it left off.
    async _checkActiveTurn(sid) {
      try {
        const r = await fetch("/api/chat/sessions/" + sid + "/active",
                               { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        if (!d.active || this.currentId !== sid) return;
        if (this.streaming) return;
        // Reconnect any time the backend says there's an active turn.
        // get_session_api returns SDK-only messages (no broadcast
        // overlay), so loadSession's view is just the user msg — the
        // SSE replay we kick off here will refill thinking / text /
        // tool blocks live. Fire-and-forget; the SSE handlers populate
        // messages as events arrive.
        this.send({ reconnect: true });
      } catch (e) { /* silent */ }
    },

    async loadSession(sid) {
      if (!sid) return;
      const st = this._ensureTabState(sid);
      // Skeleton on the active tab during the fetch — markdown rendering of
      // a long history can also take a noticeable beat after the network
      // returns, so the flag must wrap both phases.
      const isCurrent = sid === this.currentId;
      if (isCurrent) this.messagesLoading = true;
      try {
        const r = await fetch("/api/chat/sessions/" + sid, { headers: this.hdr() });
        if (!r.ok) {
          st.messages.length = 0;
          if (isCurrent) this.messages = st.messages;
          return;
        }
        const s = await r.json();
        const next = (s.messages || []).map((m, i) => {
          const out = { ...m, _k: sid + "-" + i };
          if (m.role === "assistant" && m.text) out.html = this.mdRender(m.text);
          return out;
        });
        // Mutate in place — preserves the Array reference Alpine is watching.
        st.messages.length = 0;
        st.messages.push(...next);
        if (isCurrent) {
          this.messages = st.messages;
          // Background-completion: if there's an in-flight turn on this
          // session that finished while we were elsewhere, the JSONL we
          // just loaded already has its complete output — nothing to do.
          // But if the turn is STILL in progress, tell the user so they
          // know the reply isn't done yet. A proper "reconnect SSE for
          // live streaming" UI is a larger refactor; for now we just
          // surface the state. The user can wait + reload to see more.
          this._checkActiveTurn(sid);
          if (s.model) this.model = s.model;
          // effort defaults to "" (adaptive); always assign so switching from
          // a high-effort tab to a fresh one doesn't leave the old value visible.
          this.effort = s.effort || "";
          this.atBottom = true;
          this.scrollToBottom(true);
          this.$nextTick(() => this.highlightCode(".chat-body"));
        }
        await this._fetchTabUsage(sid);
      } finally {
        if (isCurrent) this.messagesLoading = false;
      }
    },
    async renameSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const name = await this.prompt({
        title: this.lang === "zh" ? "重命名会话" : "Rename session",
        value: cur.name,
      });
      if (!name) return;
      const r = await fetch("/api/chat/sessions/" + cur.id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (r.ok) { await this.refreshSessions(); this.toast(this.t("toast.renamed"), "success"); }
    },

    async editSessionPrompt() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      // 取最新（含 system_prompt）
      const r0 = await fetch("/api/chat/sessions/" + cur.id, { headers: this.hdr() });
      const full = r0.ok ? await r0.json() : { system_prompt: "" };
      const prompt = await this.prompt({
        title: this.lang === "zh"
          ? "本会话 system prompt（留空 = 用默认）"
          : "Per-session system prompt (empty = use default)",
        body: this.lang === "zh"
          ? "会拼在 muselab 默认 system prompt 前。改后下一条消息生效。"
          : "Prepended to muselab's default system prompt. Takes effect on the next message.",
        value: full.system_prompt || "",
      });
      if (prompt === null) return;
      const r = await fetch("/api/chat/sessions/" + cur.id, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: prompt }),
      });
      if (r.ok) this.toast(this.t("toast.saved"), "success");
    },

    // ===== settings modal =====
    async openSettings() {
      const r = await fetch("/api/settings", { headers: this.hdr() });
      if (!r.ok) {
        this.toast(this.lang === "zh" ? "无法加载设置" : "Failed to load settings", "error");
        return;
      }
      const d = await r.json();
      this.settings.providers = d.providers;
      this.settings.draftKeys = Object.fromEntries(d.providers.map(p => [p.env_key, ""]));
      this.settings.draftDefaults = { ...d.defaults };
      this.settings.draftParams = { ...d.params };
      // Mobile 2-level menu: always land on the top-level entry list
      // when opening. activePage stays null so the menu is shown and
      // every section is hidden until the user picks one.
      this.settings.activePage = null;
      this.settings.show = true;
      // Load MCP + Skill + Cost dashboard in parallel — non-fatal if any fails.
      // Cost is loaded eagerly so desktop users see data without having to
      // click "refresh" (mobile users hit it via the menu item's @click anyway).
      this.refreshMcpList();
      this.refreshSkillList();
      this.loadCostDashboard();
    },
    async loadCostDashboard(force = false) {
      if (this.cost.loading) return;
      if (this.cost.data && !force) return;
      this.cost.loading = true;
      try {
        // Browser timezone offset is -getTimezoneOffset (JS reports east as
        // negative, server expects east-positive minutes).
        const tz = -new Date().getTimezoneOffset();
        const r = await fetch(
          `/api/chat/cost-dashboard?days=${this.cost.windowDays}&tz_offset_minutes=${tz}`,
          { headers: this.hdr() });
        if (!r.ok) {
          this.cost.data = null;
          this.toast(this.lang === "zh" ? "用量看板加载失败" : "Usage dashboard failed", "error");
          return;
        }
        this.cost.data = await r.json();
      } catch (e) {
        this.cost.data = null;
      } finally {
        this.cost.loading = false;
      }
    },
    // Sum of all token classes for a usage bucket. Used everywhere a
    // single comparable number is needed (KPI cards, bar chart, per-model
    // ranking) so different vendors aggregate consistently — cost can't
    // be that number because third-party vendors report $0.
    totalTokens(bucket) {
      if (!bucket) return 0;
      return (bucket.input_tokens || 0) + (bucket.output_tokens || 0)
              + (bucket.cache_read_tokens || 0)
              + (bucket.cache_creation_tokens || 0);
    },
    // (fmtTokens defined below — reused for both the header badge and
    // the cost dashboard.)
    // Bar-chart helpers — return percentages capped at 100 so a single
    // outlier day doesn't push every other bar to invisible. Max baseline
    // is the busiest day in the window (or 1 to avoid divide-by-zero
    // when everything is empty). Argument is the total-token count for
    // that bucket (cost would diverge across vendors).
    costBarHeight(tokens) {
      const max = Math.max(1, ...(this.cost.data?.by_day || []).map(d => this.totalTokens(d)));
      const pct = Math.min(100, (tokens / max) * 100);
      return Math.max(pct, tokens > 0 ? 3 : 0);   // tiny non-zero bar = 3% so user sees it
    },
    costModelPct(tokens) {
      const max = Math.max(1, ...(this.cost.data?.by_model || []).map(m => this.totalTokens(m)));
      return Math.min(100, (tokens / max) * 100);
    },
    async refreshSkillList() {
      try {
        const r = await fetch("/api/settings/skills", { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        this.settings.skills = d.skills || [];
      } catch (e) { /* silent */ }
    },
    async refreshMcpList() {
      try {
        const r = await fetch("/api/settings/mcp", { headers: this.hdr() });
        if (!r.ok) return;
        const d = await r.json();
        this.settings.mcpServers = d.servers || [];
        this.settings.mcpExamples = d.examples || [];
      } catch (e) { /* silent — UI shows empty state */ }
    },
    async toggleMcp(name, disabled) {
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}/toggle`, {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ disabled }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.toggle_saved"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    async deleteMcp(name) {
      const ok = await this.confirm({
        title: this.t("set.mcp.delete"),
        body: this.lang === "zh"
          ? `确定删除 MCP server「${name}」？`
          : `Delete MCP server "${name}"?`,
        danger: true,
        okText: this.t("set.mcp.delete"),
      });
      if (!ok) return;
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}`, {
        method: "DELETE", headers: this.hdr(),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.deleted"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.delete_failed"), "error", 3000);
      }
    },
    async addMcpFromDraft() {
      const d = this.settings.mcpDraft;
      const name = (d.name || "").trim();
      const command = (d.command || "").trim();
      if (!name || !command) {
        this.toast(this.t("set.mcp.name_command_required"), "warn", 2500);
        return;
      }
      const args = (d.argsStr || "").trim().split(/\s+/).filter(Boolean);
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, command, args, env: {}, disabled: false }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.added"), "success", 1500);
        this.settings.mcpDraft = { show: false, name: "", command: "", argsStr: "" };
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },
    // Provider key self-test — hits the vendor's anthropic-compatible endpoint
    // with the configured key and reports back. Useful when user gets 401 and
    // doesn't want to paste keys to debug.
    async probeProvider(envKey) {
      // Pick a representative model id for this env key.
      const ENV_TO_MODEL = {
        DEEPSEEK_API_KEY: "deepseek-v4-flash",
        ZHIPUAI_API_KEY:  "glm-4.7",
        MINIMAX_API_KEY:  "minimax-m2.7",
      };
      const m = ENV_TO_MODEL[envKey];
      if (!m) return;
      this.settings.probeResults[envKey] = { ok: null, text: this.t("set.probe_running") };
      try {
        const r = await fetch(`/api/chat/probe/${encodeURIComponent(m)}`,
                                 { headers: this.hdr() });
        const d = await r.json();
        if (d.ok) {
          this.settings.probeResults[envKey] = {
            ok: true,
            text: `${this.t("set.probe_ok")} · ${d.key_hint}`,
          };
        } else {
          const status = d.status ? `HTTP ${d.status}` : (d.reason || "error");
          // Extract a single-line vendor message if there is one
          let detail = "";
          try {
            const ex = d.vendor_response_excerpt ?
              JSON.parse(d.vendor_response_excerpt) : null;
            detail = ex?.error?.message || ex?.error?.type || "";
          } catch { detail = (d.vendor_response_excerpt || "").slice(0, 120); }
          // Tack on a hint based on common error shapes
          let hint = "";
          if (d.status === 401) hint = " · " + this.t("set.probe_hint_401");
          else if (d.status === 403) hint = " · " + this.t("set.probe_hint_403");
          else if (d.status === 429) hint = " · " + this.t("set.probe_hint_429");
          this.settings.probeResults[envKey] = {
            ok: false,
            text: `${status}: ${detail || "—"}${hint}`,
          };
        }
      } catch (e) {
        this.settings.probeResults[envKey] = {
          ok: false, text: this.t("set.probe_failed") + ": " + e.message,
        };
      }
    },

    async installMcpPreset(ex) {
      const r = await fetch(`/api/settings/mcp/${encodeURIComponent(ex.name)}`, {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({
          name: ex.name, command: ex.command, args: ex.args || [],
          env: ex.env || {}, disabled: false,
        }),
      });
      if (r.ok) {
        this.toast(this.t("set.mcp.installed"), "success", 1500);
        this.refreshMcpList();
      } else {
        this.toast(this.t("set.mcp.save_failed"), "error", 3000);
      }
    },

    // Delete any session from the picker dropdown's inline × button.
    async deleteSessionById(sid) {
      const s = this.sessions.find(x => x.id === sid);
      const ok = await this.confirm({
        title: this.lang === "zh" ? "删除会话" : "Delete session",
        body: this.lang === "zh"
          ? `确定删除「${s?.name || sid.slice(0, 8)}」？此操作不可恢复（含 CLI 历史）。`
          : `Delete '${s?.name || sid.slice(0, 8)}'? Irreversible (clears CLI history too).`,
        danger: true,
        okText: this.lang === "zh" ? "删除" : "Delete",
      });
      if (!ok) return;
      // Tear down per-tab cached state before we forget about the session.
      const dyingTab = this.tabState[sid];
      if (dyingTab) {
        if (dyingTab.es) { try { dyingTab.es.close(); } catch {} }
        if (dyingTab._streamTimer) clearInterval(dyingTab._streamTimer);
        delete this.tabState[sid];
      }
      await fetch(`/api/chat/sessions/${sid}`, { method: "DELETE", headers: this.hdr() });
      await this.refreshSessions();
      if (this.currentId === sid) {
        if (this.sessions.length === 0) {
          // newSession already pushes to openTabIds + activates tab state.
          await this.newSession();
        } else {
          this.currentId = this.sessions[0].id;
          this._activateTabState(this.currentId);
          await this.switchSession();
        }
        this.savePrefs();
      }
    },

    // ===== Versions + upgrade =====
    async loadVersions() {
      this.settings.versionsLoading = true;
      try {
        const r = await fetch("/api/settings/versions", { headers: this.hdr() });
        if (r.ok) this.settings.versions = await r.json();
        else this.toast(this.lang === "zh" ? "版本检查失败" : "Version check failed", "error", 3000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "版本检查失败：" : "Check failed: ") + e.message, "error", 3000);
      } finally {
        this.settings.versionsLoading = false;
      }
    },
    async runUpgrade(only = null) {
      // only = "sdk" | "cli" | null. When null, upgrade everything available.
      if (!this.settings.versions) return;
      let targets;
      if (only) {
        targets = [only];
      } else {
        targets = [];
        if (this.settings.versions.sdk_upgrade_available) targets.push("sdk");
        if (this.settings.versions.system_cli_upgrade_available) targets.push("cli");
      }
      if (targets.length === 0) {
        this.toast(this.lang === "zh" ? "无需升级" : "Nothing to upgrade", "info", 2000);
        return;
      }
      this.settings.upgradeRunning = true;
      this.settings.upgradeResult = null;
      try {
        const r = await fetch("/api/settings/upgrade", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ targets }),
        });
        if (r.ok) {
          this.settings.upgradeResult = await r.json();
          if (this.settings.upgradeResult.ok) {
            this.toast(this.lang === "zh" ? "升级完成" : "Upgrade complete", "success", 3000);
            // Refresh the versions table so user sees the new numbers
            await this.loadVersions();
          } else {
            this.toast(this.lang === "zh" ? "升级失败 — 查看日志" : "Upgrade failed — see log", "error", 5000);
          }
        } else {
          this.toast((this.lang === "zh" ? "请求失败：" : "Request failed: ") + r.status, "error", 4000);
        }
      } catch (e) {
        this.toast((this.lang === "zh" ? "升级出错：" : "Upgrade error: ") + e.message, "error", 5000);
      } finally {
        this.settings.upgradeRunning = false;
      }
    },

    async saveSettings() {
      const body = {
        default_model: this.settings.draftDefaults.model,
        default_permission: this.settings.draftDefaults.permission,
        thinking_budget: this.settings.draftParams.thinking_budget,
        max_turns: this.settings.draftParams.max_turns,
        notify_scheduled: this.settings.draftParams.notify_scheduled,
        notify_normal:    this.settings.draftParams.notify_normal,
      };
      // 字段名按后端转 snake_case
      const k2f = {
        ANTHROPIC_API_KEY: "anthropic_api_key",
        DEEPSEEK_API_KEY: "deepseek_api_key",
        ZHIPUAI_API_KEY: "zhipuai_api_key",
        MINIMAX_API_KEY: "minimax_api_key",
      };
      for (const [envK, field] of Object.entries(k2f)) {
        const v = this.settings.draftKeys[envK];
        if (v && v.trim()) body[field] = v.trim();
      }
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        const d = await r.json();
        this.settings.show = false;
        const n = d.updated.length;
        const msg = this.lang === "zh"
          ? `已保存 ${n} 项设置`
          : `Saved ${n} setting${n === 1 ? "" : "s"}`;
        this.toast(msg, "success");
        // Settings "default model" 改了之后，下一个新建会话应该用新值。
        // 之前只写了服务端 env，但前端的 this.model 还是 localStorage 里的
        // 老值 → 用户看不到任何变化。同步前端 + localStorage 让"我改了它生效"
        // 的预期成立。已建会话有自己 locked model，不受影响。
        const newDefaultModel = this.settings.draftDefaults.model;
        if (newDefaultModel && newDefaultModel !== this.model) {
          this.model = newDefaultModel;
          this.savePrefs();
        }
        const newDefaultPerm = this.settings.draftDefaults.permission;
        if (newDefaultPerm && newDefaultPerm !== this.permission) {
          this.permission = newDefaultPerm;
          this.savePrefs();
        }
        // 刷新可用 provider 列表
        const r2 = await fetch("/api/chat/providers", { headers: this.hdr() });
        if (r2.ok) {
          this.availableModels = (await r2.json()).models || [];
          this._rebindModelSelect();
        }
        // 也刷新 contextInfo — has_any_provider 变了，否则 "no provider" 卡片不消失
        this.fetchContextInfo();
      } else {
        const prefix = this.lang === "zh" ? "保存失败：" : "Save failed: ";
        this.toast(prefix + (await r.text()), "error");
      }
    },
    async deleteSession() {
      const cur = this.sessions.find(x => x.id === this.currentId);
      if (!cur) return;
      const ok = await this.confirm({
        title: this.lang === "zh" ? "删除会话" : "Delete session",
        body: this.lang === "zh"
          ? `确定删除「${cur.name}」？此操作不可恢复。`
          : `Delete "${cur.name}"? This cannot be undone.`,
        danger: true,
        okText: this.lang === "zh" ? "删除" : "Delete",
      });
      if (!ok) return;
      await fetch("/api/chat/sessions/" + cur.id, { method: "DELETE", headers: this.hdr() });
      await this.refreshSessions();
      if (this.sessions.length === 0) { const s = await this.newSession(); this.currentId = s.id; }
      else { this.currentId = this.sessions[0].id; }
      await this.loadSession(this.currentId);
      this.savePrefs();
      this.toast(this.t("toast.deleted"), "success");
    },

    // ===== file tree =====
    async loadRoot() {
      this.childCache = {};
      const children = await this.fetchChildren("");
      this.visible = children.map(c => ({ ...c, depth: 0 }));
      this.expanded = new Set();
      const want = this._pendingExpanded || [];
      this._pendingExpanded = null;
      for (const p of want.sort((a, b) => a.length - b.length)) {
        const node = this.visible.find(n => n.path === p);
        if (node && node.is_dir) await this.expand(node);
      }
    },
    reloadTree() {
      this._pendingExpanded = Array.from(this.expanded);
      this.childCache = {};
      this.loadRoot();
    },
    async fetchChildren(path) {
      if (this.childCache[path]) return this.childCache[path];
      const url = "/api/files/list?path=" + encodeURIComponent(path)
        + (this.showHidden ? "&show_hidden=true" : "");
      const r = await fetch(url, { headers: this.hdr() });
      if (!r.ok) return [];
      const d = await r.json();
      this.childCache[path] = d.entries;
      if (d.truncated) {
        this.toast(`/${path || ""} 条目过多，仅显示前 ${d.entries.length} 条`, "warn", 3500);
      }
      return d.entries;
    },
    toggleHidden() {
      this.showHidden = !this.showHidden;
      this.savePrefs();
      this.reloadTree();
      this.toast(this.showHidden ? "显示隐藏文件" : "已隐藏 .* 文件", "info", 1500);
    },
    async onNodeClick(n) {
      if (n.is_dir) {
        if (this.expanded.has(n.path)) this.collapse(n);
        else await this.expand(n);
        this.savePrefs();
      } else {
        await this.openFile(n);
      }
    },
    async expand(n) {
      const children = await this.fetchChildren(n.path);
      const idx = this.visible.findIndex(x => x.path === n.path);
      if (idx < 0) return;
      const items = children.map(c => ({ ...c, depth: n.depth + 1 }));
      this.visible.splice(idx + 1, 0, ...items);
      this.expanded.add(n.path);
      this.expanded = new Set(this.expanded);
    },
    collapse(n) {
      const idx = this.visible.findIndex(x => x.path === n.path);
      if (idx < 0) return;
      let end = idx + 1;
      while (end < this.visible.length && this.visible[end].depth > n.depth) end++;
      this.visible.splice(idx + 1, end - idx - 1);
      for (const p of Array.from(this.expanded)) {
        if (p === n.path || p.startsWith(n.path + "/")) this.expanded.delete(p);
      }
      this.expanded = new Set(this.expanded);
    },
    // ===== context menu =====
    openCtxMenu(ev, n) {
      // Clamp to viewport so menu doesn't overflow.
      const MENU_W = 200, MENU_H = 280;
      const x = Math.min(ev.clientX, window.innerWidth - MENU_W - 8);
      const y = Math.min(ev.clientY, window.innerHeight - MENU_H - 8);
      this.ctxMenu = { show: true, x, y, node: n };
    },
    async ctxAction(action) {
      const n = this.ctxMenu.node;
      this.ctxMenu.show = false;
      if (!n) return;
      switch (action) {
        case "open":
          if (!n.is_dir) await this.openFile(n);
          break;
        case "mention":
          this.insertFileMention(n.path);
          break;
        case "copyPath":
          await navigator.clipboard?.writeText(n.path);
          this.toast(this.t("toast.copied") + ": " + n.path, "success", 1500);
          break;
        case "download":
          if (!n.is_dir) window.open(this.downloadUrl(n.path), "_blank");
          break;
        case "rename":
          await this.doRename(n);
          break;
        case "delete":
          await this.doDelete(n);
          break;
        case "newFile":
          await this.doNewFile(n);
          break;
        case "newDir":
          await this.doNewDir(n);
          break;
        case "upload":
          this._ctxUploadDir = n.path;
          this.$refs.ctxUpload.click();
          break;
      }
    },
    async doNewFile(dirNode) {
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建文件" : "New file",
        body: (zh ? "在 " : "Inside ") + `/${dirNode.path}`,
        value: "new.md",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path, content: "" }),
      });
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(`已创建 ${name}`, "success");
        // 自动打开编辑
        await this.openFile({ path, name });
        this.editing = true;
      } else this.errToast("create", await r.text());
    },
    async doNewDir(dirNode) {
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建子目录" : "New subdirectory",
        body: (zh ? "在 " : "Inside ") + `/${dirNode.path}`,
        value: "",
      });
      if (!name) return;
      const path = dirNode.path ? `${dirNode.path}/${name}` : name;
      const r = await fetch("/api/files/mkdir", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      if (r.ok) {
        delete this.childCache[dirNode.path];
        this.reloadTree();
        this.toast(`已创建 ${name}/`, "success");
      } else this.errToast("generic", await r.text());
    },
    _ctxUploadDir: "",
    async ctxUploadHandler(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      await this.uploadFileTo(this._ctxUploadDir, file);
      ev.target.value = "";
      this._ctxUploadDir = "";
    },
    async doRename(n) {
      const zh = this.lang === "zh";
      const newName = await this.prompt({
        title: zh ? "重命名" : "Rename",
        body: (zh ? "当前路径:" : "Current path: ") + n.path,
        value: n.name,
      });
      if (!newName || newName === n.name) return;
      const parent = n.path.split("/").slice(0, -1).join("/");
      const newPath = parent ? `${parent}/${newName}` : newName;
      const r = await fetch("/api/files/rename", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ src: n.path, dst: newPath }),
      });
      if (r.ok) {
        if (this.selected === n.path) this.selected = newPath;
        delete this.childCache[parent];
        this.reloadTree();
        this.toast(this.t("toast.renamed"), "success");
      } else this.errToast("rename", await r.text());
    },
    async doDelete(n) {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "删除" : "Delete",
        body: zh
          ? (`删除 ${n.name}？` + (n.is_dir ? "（仅可删除空目录）" : ""))
          : (`Delete ${n.name}?` + (n.is_dir ? " (only empty dirs)" : "")),
        danger: true,
        okText: zh ? "删除" : "Delete",
      });
      if (!ok) return;
      const r = await fetch("/api/files/delete", {
        method: "DELETE",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: n.path }),
      });
      if (r.ok) {
        // 同步 tabs：删了的文件如果在 tabs 也清掉
        this.tabs = this.tabs.filter(t => t.path !== n.path);
        if (this.selected === n.path) { this.selected = ""; this.previewMode = ""; }
        const parent = n.path.split("/").slice(0, -1).join("/");
        delete this.childCache[parent];
        this.reloadTree();
        this.toast(this.t("toast.deleted"), "success");
      } else this.errToast("delete", await r.text());
    },
    async openFile(n) {
      // multi-tab：第一次打开就推进 tabs；已存在则切换
      if (!this.tabs.find(t => t.path === n.path)) {
        this.tabs.push({ path: n.path, name: n.name || n.path.split("/").pop() });
      }
      this.selected = n.path;
      this.editing = false;
      // Persist preview-pane state so a refresh restores tabs + selected.
      this.savePrefs();
      // Mobile: opening a file should jump to the preview pane (otherwise
      // the user is still on `files` tab and sees nothing change).
      if (window.innerWidth <= 900) this.mobileTab = "preview";
      const name = n.name || n.path.split("/").pop();
      const ext = name.split(".").pop().toLowerCase();
      if (["md", "markdown"].includes(ext)) {
        this.previewMode = "md";
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(n.path), { headers: this.hdr() });
        if (r.ok) {
          this.rawText = await r.text();
          this.renderedMd = this.mdRender(this.rawText);
          this.$nextTick(() => this.highlightCode(".markdown"));
        }
      } else if (["html", "htm"].includes(ext)) {
        // Render via sandboxed iframe (backend sends strict CSP + sandbox token).
        this.previewMode = "html";
      }
      else if (["png", "jpg", "jpeg", "gif", "webp", "ico", "bmp"].includes(ext)) this.previewMode = "img";
      else if (ext === "pdf") this.previewMode = "pdf";
      else if (["xlsx", "xlsm", "xltx", "xltm"].includes(ext)) {
        // xlsx preview: backend serializes the workbook into capped per-sheet
        // string matrices so the frontend just renders <table>s. No formula
        // evaluation; cells show the last-cached value.
        const r = await fetch("/api/files/xlsx?path=" + encodeURIComponent(n.path),
                              { headers: this.hdr() });
        if (r.ok) {
          const data = await r.json();
          this.previewMode = "xlsx";
          this.xlsxSheets = data.sheets || [];
          this.xlsxActive = (this.xlsxSheets[0] && this.xlsxSheets[0].name) || "";
          this.xlsxLimits = data.limits || null;
          this.xlsxSheetsTruncated = !!data.sheets_truncated;
        } else {
          this.previewMode = "unsupported";
        }
      }
      else if (["csv", "tsv"].includes(ext)) {
        // CSV preview: paginated table render (xlsx-style) so a million-row
        // file doesn't blow the browser. First page only here; "next page"
        // button is wired through csvLoadPage().
        this.csvPath = n.path;
        this.csvOffset = 0;
        await this.csvLoadPage();
        if (this.csvData) {
          this.previewMode = "csv";
        } else {
          this.previewMode = "unsupported";
        }
      }
      else {
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(n.path), { headers: this.hdr() });
        if (r.ok) {
          this.previewMode = "text";
          this.rawText = await r.text();
          this.previewLang = this.hljsLang(n.path);
          // 强制重新高亮：删 dataset.hl 让 highlightCode 重新跑
          this.$nextTick(() => {
            document.querySelectorAll(".text code").forEach(el => { delete el.dataset.hl; });
            this.highlightCode(".text");
          });
        }
        else this.previewMode = "unsupported";
      }
    },
    async csvLoadPage() {
      if (!this.csvPath || this.csvLoading) return;
      this.csvLoading = true;
      try {
        const url = `/api/files/csv?path=${encodeURIComponent(this.csvPath)}`
                    + `&offset=${this.csvOffset}&limit=${this.csvLimit}`;
        const r = await fetch(url, { headers: this.hdr() });
        if (r.ok) {
          this.csvData = await r.json();
        } else {
          this.csvData = null;
          this.toast(this.lang === "zh" ? "CSV 解析失败" : "CSV parse failed",
                      "error", 3000);
        }
      } finally {
        this.csvLoading = false;
      }
    },
    csvNextPage() {
      if (!this.csvData) return;
      const next = this.csvOffset + this.csvLimit;
      if (next >= (this.csvData.total_rows || 0)) return;
      this.csvOffset = next;
      this.csvLoadPage();
    },
    csvPrevPage() {
      if (this.csvOffset <= 0) return;
      this.csvOffset = Math.max(0, this.csvOffset - this.csvLimit);
      this.csvLoadPage();
    },
    csvWindowEnd() {
      if (!this.csvData) return 0;
      return Math.min(this.csvOffset + (this.csvData.rows || []).length,
                       this.csvData.total_rows || 0);
    },

    hljsLang(path) {
      if (!path) return "plaintext";
      const name = path.split("/").pop().toLowerCase();
      // No-extension files mapped by name
      const noExt = {
        dockerfile: "dockerfile", containerfile: "dockerfile",
        makefile: "makefile",
        rakefile: "ruby", gemfile: "ruby",
        vagrantfile: "ruby", brewfile: "ruby",
      };
      if (noExt[name]) return noExt[name];
      const ext = name.includes(".") ? name.split(".").pop() : "";
      const map = {
        md: "markdown", markdown: "markdown",
        py: "python", pyi: "python",
        js: "javascript", mjs: "javascript", cjs: "javascript",
        jsx: "javascript", ts: "typescript", tsx: "typescript",
        cpp: "cpp", "c++": "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
        c: "c", h: "c", m: "objectivec",
        rs: "rust", go: "go",
        java: "java", kt: "kotlin", scala: "scala",
        rb: "ruby", php: "php", swift: "swift", lua: "lua",
        sh: "bash", bash: "bash", zsh: "bash", fish: "bash",
        ps1: "powershell",
        sql: "sql", graphql: "graphql",
        html: "xml", htm: "xml", xml: "xml", svg: "xml",
        css: "css", scss: "scss", less: "less",
        json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini",
        env: "bash", conf: "ini",
        log: "accesslog",
        vue: "xml", svelte: "xml",
        proto: "protobuf",
      };
      return map[ext] || "plaintext";
    },
    async openByPath(path) { await this.openFile({ path, name: path.split("/").pop() }); },

    async switchTab(path) {
      // 不再 push（已在 tabs 里），只是切换 selected 并重新加载内容
      await this.openFile({ path, name: path.split("/").pop() });
      await this.revealInTree(path);
    },
    async revealInTree(path) {
      // Expand every ancestor directory so the file's row exists in `visible`,
      // then scroll its <li> into view. Skips no-op when the file is already
      // visible (`scrollIntoView({block:"nearest"})` won't scroll if visible).
      if (!path) return;
      const parts = path.split("/");
      parts.pop();   // drop the filename, keep only directory chain
      const dirPath = parts.join("/");
      if (dirPath) await this.expandPath(dirPath);
      this.$nextTick(() => {
        const el = document.querySelector(
          `.filelist li[data-path="${(window.CSS && CSS.escape) ? CSS.escape(path) : path}"]`);
        if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    },
    closeTab(path) {
      const idx = this.tabs.findIndex(t => t.path === path);
      if (idx < 0) return;
      this.tabs.splice(idx, 1);
      if (this.selected === path) {
        // 关掉的是当前 tab，切到旁边
        if (this.tabs.length === 0) {
          this.selected = "";
          this.previewMode = "";
          this.rawText = "";
          this.renderedMd = "";
          this.editing = false;
        } else {
          const next = this.tabs[Math.min(idx, this.tabs.length - 1)];
          this.openByPath(next.path);
          return;   // openByPath → openFile → savePrefs runs there
        }
      }
      this.savePrefs();
    },

    rawUrl(p) {
      const v = this.previewVersion ? `&_v=${this.previewVersion}` : "";
      return "/api/files/raw?path=" + encodeURIComponent(p)
              + "&token=" + encodeURIComponent(this.token) + v;
    },
    async reloadPreview() {
      // Manual "🗘 reload" button in preview header. Bumps previewVersion
      // (cache-buster for iframe / image / pdf rawUrl) AND re-fetches the
      // read endpoint for md / text. Useful when the file changed outside
      // muselab's normal write paths (terminal git pull, external editor).
      if (!this.selected) return;
      this.previewVersion = Date.now();
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              this.$nextTick(() => {
                document.querySelectorAll(".text code")
                  .forEach(el => { delete el.dataset.hl; });
                this.highlightCode(".text");
              });
            }
          }
        } catch (_e) { /* network blip */ }
      }
      this.toast(this.lang === "zh" ? "已刷新预览" : "Preview reloaded",
                  "success", 1200);
    },

    async _maybeReloadPreview(toolFilePath) {
      // Called from the tool_use SSE handler when a write-style tool fires.
      // Refresh the preview pane if the tool's target path matches what's
      // currently being previewed. Path matching is by basename + suffix
      // match against this.selected (which may be absolute or ROOT-relative);
      // false-positives across same-named files in different dirs are
      // acceptable (we'd over-refresh, which is harmless) vs missing a real
      // hit by being too strict on path normalization.
      if (!this.selected || !toolFilePath) return;
      const selBase = this.selected.split("/").pop();
      const toolBase = toolFilePath.split("/").pop();
      if (selBase !== toolBase) return;
      // Same basename → cache-bust. For html/img/pdf/iframe the new
      // previewVersion flows through rawUrl on next render; for md/text we
      // also need to re-fetch since rawText is cached in the component.
      this.previewVersion = Date.now();
      if (this.previewMode === "md" || this.previewMode === "text") {
        const url = "/api/files/read?path=" + encodeURIComponent(this.selected)
                     + "&_v=" + this.previewVersion;
        try {
          const r = await fetch(url, { headers: this.hdr() });
          if (r.ok) {
            this.rawText = await r.text();
            if (this.previewMode === "md") {
              this.renderedMd = this.mdRender(this.rawText);
              this.$nextTick(() => this.highlightCode(".markdown"));
            } else {
              this.$nextTick(() => {
                document.querySelectorAll(".text code")
                  .forEach(el => { delete el.dataset.hl; });
                this.highlightCode(".text");
              });
            }
          }
        } catch (e) { /* network blip — manual refresh still possible */ }
      }
    },
    downloadUrl(p) { return "/api/files/download?path=" + encodeURIComponent(p) + "&token=" + encodeURIComponent(this.token); },

    iconRef(n) {
      if (n.is_dir) return "#i-folder";
      const name = n.name || n.path.split("/").pop() || "";
      const ext = name.split(".").pop().toLowerCase();
      if (["md", "markdown", "txt", "rst"].includes(ext)) return "#i-file-text";
      if (["html", "htm"].includes(ext)) return "#i-globe";
      if (["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"].includes(ext)) return "#i-image";
      if (["py", "js", "ts", "go", "rs", "java", "cpp", "c", "sh", "json", "yaml", "yml", "toml"].includes(ext)) return "#i-code";
      return "#i-file";
    },
    // Coarse category string used as a data-ext CSS hook for icon tinting.
    // Keeps the SVG sprite small (no new icons needed) — colors do the
    // disambiguation work.
    fileExtClass(n) {
      if (!n || n.is_dir) return "";
      const name = (n.name || n.path.split("/").pop() || "").toLowerCase();
      const ext = name.includes(".") ? name.split(".").pop() : "";
      if (["md", "markdown", "rst", "txt"].includes(ext)) return "doc";
      if (["py", "ipynb"].includes(ext))                  return "py";
      if (["js", "mjs", "cjs", "jsx"].includes(ext))      return "js";
      if (["ts", "tsx"].includes(ext))                    return "ts";
      if (["go", "rs", "java", "kt", "swift"].includes(ext)) return "compiled";
      if (["c", "cpp", "h", "hpp", "cc", "cxx"].includes(ext)) return "cstyle";
      if (["sh", "bash", "zsh", "fish"].includes(ext))    return "shell";
      if (["json", "yaml", "yml", "toml", "ini", "conf"].includes(ext)) return "config";
      if (["html", "htm", "css", "scss", "less", "vue", "svelte"].includes(ext)) return "web";
      if (["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"].includes(ext)) return "image";
      if (["pdf"].includes(ext))                          return "pdf";
      if (["zip", "tar", "gz", "tgz", "bz2", "7z", "rar"].includes(ext)) return "archive";
      if (["csv", "tsv", "xls", "xlsx"].includes(ext))    return "data";
      if (["mp3", "wav", "ogg", "flac", "m4a"].includes(ext)) return "audio";
      if (["mp4", "mkv", "mov", "webm", "avi"].includes(ext)) return "video";
      if (["sql"].includes(ext))                          return "data";
      if (["log"].includes(ext))                          return "log";
      return "";
    },
    fmtSize(n) {
      if (n < 1024) return n + "B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + "K";
      return (n / 1024 / 1024).toFixed(1) + "M";
    },
    highlightCode(root) {
      if (!window.hljs) { console.warn("[muselab] hljs not loaded"); return; }
      document.querySelectorAll(root + " code").forEach(el => {
        // hljs.highlightElement refuses to re-highlight already-highlighted
        // elements. So always go through highlight() directly and replace HTML.
        const text = el.textContent;
        const m = el.className.match(/language-([\w+#-]+)/);
        const lang = m && m[1];
        try {
          const r = (lang && window.hljs.getLanguage(lang))
            ? window.hljs.highlight(text, { language: lang, ignoreIllegals: true })
            : window.hljs.highlightAuto(text);
          el.innerHTML = r.value;
          el.classList.add("hljs");
        } catch (e) { console.warn("[muselab] highlight failed:", e); }
        // Attach copy button to every <pre> wrapping a code block — only once.
        this._attachCopyBtn(el);
      });
    },

    // Wraps a fenced-code-block <code> with a hover-revealed copy button on
    // its enclosing <pre>. Idempotent (data-copybtn marker).
    _attachCopyBtn(codeEl) {
      const pre = codeEl.parentElement;
      if (!pre || pre.tagName !== "PRE") return;       // inline `code`, skip
      if (pre.dataset.copybtn === "1") return;          // already attached
      pre.dataset.copybtn = "1";
      pre.classList.add("has-copy-btn");
      const btn = document.createElement("button");
      btn.className = "code-copy-btn";
      btn.type = "button";
      const labelCopy = this.lang === "zh" ? "复制" : "Copy";
      const labelOk   = this.lang === "zh" ? "已复制" : "Copied";
      btn.textContent = labelCopy;
      btn.setAttribute("aria-label", labelCopy);
      btn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try {
          // textContent strips the hljs <span> tags, gives clean source
          const raw = codeEl.textContent || "";
          if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(raw);
          } else {
            // Fallback for http://localhost where clipboard API needs a permission
            const ta = document.createElement("textarea");
            ta.value = raw; ta.style.position = "fixed"; ta.style.left = "-9999px";
            document.body.appendChild(ta); ta.select();
            document.execCommand("copy"); document.body.removeChild(ta);
          }
          btn.textContent = labelOk;
          btn.classList.add("copied");
          setTimeout(() => { btn.textContent = labelCopy; btn.classList.remove("copied"); }, 1500);
        } catch (e) {
          btn.textContent = this.lang === "zh" ? "失败" : "Failed";
          setTimeout(() => { btn.textContent = labelCopy; }, 1500);
        }
      });
      pre.appendChild(btn);
    },

    // ===== search =====
    async doSearch() {
      const q = this.searchQ.trim();
      if (q.length < 2) { this.clearSearch(); return; }
      this.searchMode = true;
      this.searching = true;
      const [a, b] = await Promise.all([
        fetch("/api/files/search?q=" + encodeURIComponent(q), { headers: this.hdr() }).then(r => r.ok ? r.json() : { entries: [] }),
        fetch("/api/files/grep?q=" + encodeURIComponent(q), { headers: this.hdr() }).then(r => r.ok ? r.json() : { hits: [] }),
      ]);
      this.searchHits = a.entries || [];
      this.searchTruncated = !!a.truncated;
      this.grepHits = b.hits || [];
      this.grepTruncated = !!b.truncated;
      this.searching = false;
    },
    clearSearch() {
      this.searchQ = ""; this.searchMode = false; this.searching = false;
      this.searchHits = []; this.grepHits = []; this.searchTruncated = false; this.grepTruncated = false;
    },
    async onSearchClick(n) {
      if (n.is_dir) { this.clearSearch(); await this.expandPath(n.path); }
      else { await this.openFile(n); }
    },
    async expandPath(path) {
      const parts = path.split("/");
      let acc = "";
      for (let i = 0; i < parts.length; i++) {
        acc = acc ? acc + "/" + parts[i] : parts[i];
        const node = this.visible.find(x => x.path === acc);
        if (node && node.is_dir && !this.expanded.has(acc)) await this.expand(node);
      }
    },

    // ===== upload / drag-drop / mkdir =====
    async upload(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      await this.uploadFileTo("", file);
      ev.target.value = "";
    },
    async uploadFileTo(dirPath, file) {
      const fd = new FormData();
      fd.append("path", dirPath);
      fd.append("file", file);
      const r = await fetch("/api/files/upload", { method: "POST", headers: this.hdr(), body: fd });
      if (r.ok) {
        delete this.childCache[dirPath];
        this.reloadTree();
        this.toast(`已上传 ${file.name} 到 /${dirPath || ""}`, "success");
      } else this.errToast("upload", await r.text());
    },
    // Custom MIME so tree-internal drags don't collide with OS file drops.
    // Reading getData with this type during dragover would force a stale
    // permissions roundtrip; we use ev.dataTransfer.types.includes() to
    // detect an internal drag without actually pulling the payload until
    // drop fires.
    _DRAG_MIME_INTERNAL: "application/x-muselab-path",

    onTreeNodeDragStart(ev, n) {
      // Stamp both our custom mime (used in onDrop to know "this is a
      // tree-internal move, not an OS upload") and text/plain (broad
      // compatibility — some browsers strip custom types in certain
      // scenarios). text/plain doubles as fallback.
      ev.dataTransfer.setData(this._DRAG_MIME_INTERNAL, n.path);
      ev.dataTransfer.setData("text/plain", n.path);
      ev.dataTransfer.effectAllowed = "move";
      this._dragSrcPath = n.path;
    },
    onTreeNodeDragOver(ev, n) {
      // Target dir = the node itself when it's a folder, or its parent
      // directory when it's a file. Dropping onto a file lands the
      // item next to that file (matches Finder / VSCode behavior).
      const targetDir = n.is_dir
        ? n.path
        : n.path.split("/").slice(0, -1).join("/");
      const src = this._dragSrcPath || "";
      // Block illegal targets: dropping onto itself, into its own
      // subtree, or onto something already in the same parent dir
      // (would be a no-op rename).
      if (src) {
        const srcParent = src.split("/").slice(0, -1).join("/");
        if (src === targetDir
            || (targetDir + "/").startsWith(src + "/")
            || srcParent === targetDir) {
          ev.dataTransfer.dropEffect = "none";
          return;
        }
      }
      // Highlight the target *directory* row so the user sees where
      // the drop will land — for file targets this means the parent
      // dir lights up, not the file itself.
      this.dragOver = targetDir;
      ev.dataTransfer.dropEffect = "move";
    },
    async onDrop(ev, n) {
      this.dragOver = "";
      const wasSrc = this._dragSrcPath;
      this._dragSrcPath = null;
      // Same dir-resolution rule as dragover: folder → self, file → parent.
      const targetDir = n.is_dir
        ? n.path
        : n.path.split("/").slice(0, -1).join("/");

      // Tree-internal drag → move via /api/files/rename. We check
      // dataTransfer.types first so we don't accidentally trip on plain
      // text from elsewhere on the page.
      const types = Array.from(ev.dataTransfer?.types || []);
      const isInternal = types.includes(this._DRAG_MIME_INTERNAL);
      if (isInternal) {
        const src = ev.dataTransfer.getData(this._DRAG_MIME_INTERNAL) || wasSrc;
        await this.moveTreeItem(src, targetDir);
        return;
      }

      // OS file upload — dropping onto a file uploads into that file's
      // parent dir (same dir-resolution as internal moves).
      const files = ev.dataTransfer?.files || [];
      if (!files.length) return;
      for (const f of files) await this.uploadFileTo(targetDir, f);
    },
    async moveTreeItem(srcPath, targetDir) {
      if (!srcPath) return;
      const srcName = srcPath.split("/").pop();
      const srcParent = srcPath.split("/").slice(0, -1).join("/");
      // Same-parent drop = no-op (user dragged a file inside its own
      // directory without changing anything).
      if (srcParent === targetDir) return;
      // Dropping a directory onto itself or anywhere in its own subtree
      // would create a cycle. Backend would 422 on rename but we'd
      // rather not even attempt it — feedback is faster client-side.
      if (srcPath === targetDir
          || (targetDir + "/").startsWith(srcPath + "/")) {
        this.toast(this.lang === "zh"
          ? "不能把目录拖进自己的子目录"
          : "Can't move a folder into its own subtree", "warn", 2500);
        return;
      }
      const newPath = targetDir ? `${targetDir}/${srcName}` : srcName;
      const r = await fetch("/api/files/rename", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ src: srcPath, dst: newPath }),
      });
      if (!r.ok) {
        const err = await r.text();
        this.toast((this.lang === "zh" ? "移动失败：" : "Move failed: ") + err,
          "error", 4000);
        return;
      }
      this.toast(this.lang === "zh"
        ? `已移动到 /${targetDir || "(根)"}`
        : `Moved to /${targetDir || "(root)"}`, "success", 2000);
      // Refresh the tree and reroute selected/preview if we just moved
      // the currently-open file.
      if (this.selected === srcPath) this.selected = newPath;
      const openTab = this.tabs.find(t => t.path === srcPath);
      if (openTab) openTab.path = newPath;
      await this.reloadTree();
    },
    async mkdirPrompt() {
      const zh = this.lang === "zh";
      const name = await this.prompt({
        title: zh ? "新建目录" : "New directory",
        body: zh ? "输入相对根的路径，例如 archives/2026"
                 : "Path relative to root, e.g. archives/2026",
        placeholder: "archives/2026",
      });
      if (!name) return;
      const r = await fetch("/api/files/mkdir", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: name }),
      });
      if (r.ok) { this.reloadTree(); this.toast(this.t("toast.created"), "success"); }
      else this.errToast("generic", await r.text());
    },

    // ===== edit =====
    isEditable(path) {
      if (!path) return false;
      const name = path.split("/").pop().toLowerCase();
      const ext = name.includes(".") ? name.split(".").pop() : name;
      return EDITABLE_EXT.has(ext);
    },

    layoutStyle() {
      // 动态算 template，匹配实际渲染的元素数。否则 x-show 隐藏 resizer 时
      // 元素被移出 grid，剩余 children 错位填入空闲 column，导致右 resizer
      // 拿到 1fr 宽 → 鼠标 hover 它整片变成 accent 色。
      // Clamp persisted widths to 2/3 of viewport so window-shrink doesn't
      // leave one pane wider than the viewport (which would collapse the
      // center chat to 0 and lock the user out). Mirrors the drag clamp.
      const maxW = Math.max(220, Math.floor(window.innerWidth * 2 / 3));
      const cols = [];
      if (this.leftOpen) cols.push(Math.min(this.leftWidth, maxW) + "px", "4px");
      cols.push("1fr");
      if (this.rightOpen) cols.push("4px", Math.min(this.rightWidth, maxW) + "px");
      return { gridTemplateColumns: cols.join(" ") };
    },
    // computedOpenFilesHeight() removed — auto-fit now relies on CSS
    // (.open-files-list max-height + .open-files height: auto). Splitter
    // drag still sets a pixel value, which wins via inline style.
    startOpenFilesResize(ev) {
      // Drag the splitter at the bottom of .open-files to resize. Reuses the
      // same fullscreen overlay trick as the pane resizer so iframe / video
      // children don't eat the mousemove.
      ev.preventDefault();
      const startY = ev.clientY;
      // openFilesHeight now controls the LIST height (not the container).
      // Snapshot the currently-rendered list height so the drag picks up
      // smoothly from wherever CSS auto-fit put it.
      const listEl = ev.currentTarget.parentElement.querySelector(".open-files-list");
      const startH = this.openFilesHeight
                       || (listEl ? listEl.offsetHeight : 100);
      const splitter = ev.currentTarget;
      splitter.classList.add("active");
      document.body.style.cursor = "ns-resize";
      document.body.style.userSelect = "none";
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;z-index:99999;cursor:ns-resize;background:transparent;";
      document.body.appendChild(overlay);
      const onMove = (e) => {
        const delta = e.clientY - startY;
        // min ~1 row, max ~70% viewport.
        this.openFilesHeight = Math.max(28, Math.min(window.innerHeight * 0.7, startH + delta));
      };
      const onUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        splitter.classList.remove("active");
        overlay.remove();
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        this.savePrefs();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    startResize(which, ev) {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = which === "left" ? this.leftWidth : this.rightWidth;
      const target = ev.currentTarget;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      target.classList.add("active");
      // 关键修复：拖动时鼠标经过 HTML 预览的 sandboxed iframe（或其他
      // 嵌入元素）时，mousemove 事件被 iframe 吞掉，分隔条跟不上鼠标，
      // 释放后还会"跳脱"到错位置。覆盖一个全屏透明 overlay 在 iframe
      // 上方接管事件命中区，但不 stopPropagation —— mousemove 仍冒泡
      // 到 document 让 onMove 接收。
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;z-index:99999;cursor:col-resize;background:transparent;";
      document.body.appendChild(overlay);
      // Bounds.
      // Max: 2/3 of the viewport — leaves at least 1/3 for the center
      // chat. Big-monitor users get serious side-pane real estate.
      // Hide/show hysteresis: a single threshold would jitter when the
      // user wiggled around it. So two thresholds with a 20px gap —
      // dragging below HIDE_AT collapses the pane, dragging back above
      // SHOW_AT re-opens it AT THAT NEW WIDTH (not the pre-drag size).
      // Crucially: we don't end the drag on hide. The user can keep
      // dragging — pulling outward past SHOW_AT reopens the pane mid-
      // drag, so you can shrink-then-recover with one continuous gesture.
      const HIDE_AT = 200;
      const SHOW_AT = 220;
      const maxW = Math.floor(window.innerWidth * 2 / 3);
      const isLeft = which === "left";
      const onMove = (e) => {
        const delta = isLeft ? (e.clientX - startX) : (startX - e.clientX);
        const targetW = startW + delta;
        const isOpenNow = isLeft ? this.leftOpen : this.rightOpen;
        if (isOpenNow && targetW < HIDE_AT) {
          // Going-down threshold crossed. Hide and remember the pre-drag
          // width so chevron-reopen later restores that size (rather than
          // showing a sliver). Drag continues so you can pull back out.
          if (isLeft) { this.leftWidth = startW; this.leftOpen = false; }
          else        { this.rightWidth = startW; this.rightOpen = false; }
          return;
        }
        if (!isOpenNow && targetW >= SHOW_AT) {
          // Going-up threshold crossed during the same drag — re-open.
          if (isLeft) this.leftOpen = true;
          else        this.rightOpen = true;
        }
        // Only resize when actually open (post-show transition counts).
        const reopened = !isOpenNow && targetW >= SHOW_AT;
        if (isOpenNow || reopened) {
          const w = Math.max(SHOW_AT, Math.min(maxW, targetW));
          if (isLeft) this.leftWidth = w;
          else        this.rightWidth = w;
        }
      };
      const onUp = () => {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        target.classList.remove("active");
        overlay.remove();
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        this.savePrefs();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },

    async toggleEdit() {
      if (this.editing) { this.editing = false; return; }
      // 进入编辑：确保 rawText 已加载（html/img/pdf 走 raw 模式时没 fetch 文本）
      if (!this.rawText || this.previewMode === "html" || this.previewMode === "pdf" || this.previewMode === "img") {
        const r = await fetch("/api/files/read?path=" + encodeURIComponent(this.selected), { headers: this.hdr() });
        if (!r.ok) {
          this.errToast("read", this.lang === "zh"
                                  ? "可能是二进制或太大 — " + (await r.text())
                                  : "binary or too large — " + (await r.text()));
          return;
        }
        this.rawText = await r.text();
      }
      this.editText = this.rawText;
      this.editing = true;
    },
    async saveEdit() {
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: this.selected, content: this.editText }),
      });
      if (r.ok) {
        this.rawText = this.editText;
        if (this.previewMode === "md") {
          this.renderedMd = this.mdRender(this.rawText);
          this.$nextTick(() => this.highlightCode(".markdown"));
        }
        // Bump previewVersion so HTML / PDF / image iframes pick up the new
        // file content. Without this, iframes keep showing the stale render
        // (browser disk cache + same URL) until the user hard-refreshes —
        // the issue was visible when editing a html report styled in dark
        // mode to light mode: editor saved, preview iframe still showed dark.
        this.previewVersion = Date.now();
        this.editing = false;
        this.toast(this.t("toast.saved"), "success");
      } else this.errToast("save", await r.text());
    },

    // ===== @ mention =====
    insertFileMention(path) {
      const mention = "@" + path + " ";
      this.input = (this.input || "") + (this.input && !this.input.endsWith(" ") ? " " : "") + mention;
      if (this.$refs.chatInput) this.$refs.chatInput.focus();
      this.toast(this.t("toast.mention_added", { path }), "success", 1500);
      // Mobile: @ mention is a chat-side action, jump to the chat pane
      if (window.innerWidth <= 900) this.mobileTab = "chat";
    },
    autoGrow(ta) {
      // Grow to fit content up to max. The hard problem: iOS Safari
      // on touch forces font-size: 16px (anti-zoom) and reports a
      // scrollHeight a few px above the textarea's min-height for
      // single-line input — depending on subpixel rendering, line-
      // height rounding, etc, it can be min+1 through min+4. So the
      // naive "if scrollHeight > min-height, grow" fires on the very
      // first character typed and keeps the textarea inflated forever.
      //
      // Real fix: distinguish "single line" from "multi line" by
      // checking whether scrollHeight is closer to 1× or ≥ 2× the
      // min-height. Below 1.4×min — single line, clear inline height
      // and let CSS handle it. At or above 1.4×min — content has
      // genuinely wrapped to a second line, expand inline to fit.
      // 1.4 is comfortably between 1× (single line, ~min) and 2×
      // (two lines, ~2 × line-height + padding) on both PC and mobile.
      ta.style.height = "auto";
      const sh = ta.scrollHeight;
      const max = 200;
      const minH = parseFloat(getComputedStyle(ta).minHeight) || 34;
      if (sh < minH * 1.4) {
        ta.style.height = "";          // single line: hand control back to CSS
      } else {
        ta.style.height = Math.min(sh, max) + "px";
      }
    },

    // ===== slash commands =====
    slashResults: [],   // filled by onChatInput
    _navPop(delta) {
      // shared up/down handler for either @ mention or / slash popup
      if (this.slashShow) {
        if (delta < 0) this.slashIdx = Math.max(0, this.slashIdx - 1);
        else this.slashIdx = Math.min(this.slashResults.length - 1, this.slashIdx + 1);
        return true;
      }
      if (this.mentionShow) {
        if (delta < 0) this.mentionIdx = Math.max(0, this.mentionIdx - 1);
        else this.mentionIdx = Math.min(this.mentionResults.length - 1, this.mentionIdx + 1);
        return true;
      }
      return false;
    },
    pickSlash(i) {
      const c = this.slashResults[i];
      if (!c) return;
      // Replace current input with the canonical form so user sees what's submitted
      this.input = "/" + c.name + (c.name === "model" || c.name === "resume" ? " " : "");
      this.slashShow = false;
      if (this.$refs.chatInput) this.$refs.chatInput.focus();
      // For commands with NO argument needed, auto-execute on selection
      if (!["model", "resume"].includes(c.name)) {
        this._runSlash(c.name, "");
        this.input = "";
      }
    },

    async _runSlash(cmd, arg) {
      arg = (arg || "").trim();
      switch (cmd) {
        case "help": {
          const cmds = this.SLASH_CMDS
            .map(c => `**/${c.name}** — ${c.desc[this.lang] || c.desc.zh}`)
            .join("\n");
          const md = [
            `## ${this.t("slash.help_title")}`,
            "",
            `### ${this.t("help.sec_slash")}`,
            cmds,
            "",
            `### ${this.t("help.sec_keys")}`,
            this.t("help.keys_list"),
            "",
            `### ${this.t("help.sec_layout")}`,
            this.t("help.layout_list"),
            "",
            `${this.t("help.docs_link")} → [docs/personalize-claude-md.md](docs/personalize-claude-md.md)`,
          ].join("\n");
          this._injectAssistantNote(md);
          return;
        }
        case "clear": {
          if (!this.currentId) return;
          const oldId = this.currentId;
          await fetch(`/api/chat/reset?token=${encodeURIComponent(this.token)}&session_id=${encodeURIComponent(oldId)}`,
                       { method: "POST" });
          await fetch(`/api/chat/sessions/${oldId}`, { method: "DELETE", headers: this.hdr() });
          await this.refreshSessions();
          // Drop the old session's tab + cached state, then open a fresh one
          // in its slot. newSession() handles tabState + openTabIds + switch.
          const oldStreamState = this.tabState[oldId];
          if (oldStreamState) {
            if (oldStreamState.es) { try { oldStreamState.es.close(); } catch {} }
            if (oldStreamState._streamTimer) clearInterval(oldStreamState._streamTimer);
            delete this.tabState[oldId];
          }
          this.openTabIds = this.openTabIds.filter(x => x !== oldId);
          await this.newSession();
          this.toast(this.t("slash.cleared"), "success", 1500);
          return;
        }
        case "compact": {
          if (!this.currentId) return;
          const r = await fetch(`/api/chat/sessions/${this.currentId}/compact`,
                                  { method: "POST", headers: this.hdr() });
          if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
          const meta = await r.json();
          await this.refreshSessions();
          this.currentId = meta.id;
          await this.loadSession(meta.id);
          // Pre-fill input with the compact prompt — user reviews then sends
          this.input = this.t("slash.compact_prompt");
          this.toast(this.t("slash.compact_ok"), "success", 2500);
          return;
        }
        case "model": {
          if (!arg) {
            const list = (this.availableModels || []).map(m => `- ${m.group} · **${m.model}**`).join("\n");
            this._injectAssistantNote(this.t("slash.model_list_title") + "\n\n" + list);
            return;
          }
          const found = (this.availableModels || []).find(m => m.model === arg);
          if (!found) { this.toast(this.t("slash.model_unknown", { id: arg }), "warn", 3000); return; }
          this.model = arg;
          this.toast(this.t("slash.model_switched", { id: arg }), "success", 1500);
          return;
        }
        case "resume": {
          if (!arg) {
            const list = (this.sessions || []).slice(0, 10)
              .map(s => {
                const turns = s.turn_count ?? Math.floor((s.message_count || 0) / 2);
                return `- **${s.name}** (${turns}t, ${s.id.slice(0, 8)})`;
              }).join("\n");
            this._injectAssistantNote(this.t("slash.resume_list_title") + "\n\n" + list);
            return;
          }
          const q = arg.toLowerCase();
          const hit = (this.sessions || []).find(s =>
            s.id.startsWith(arg) || s.name.toLowerCase().includes(q));
          if (!hit) { this.toast(this.t("slash.resume_no_match"), "warn", 2000); return; }
          this.currentId = hit.id;
          await this.loadSession(hit.id);
          this.toast(this.t("slash.resumed", { name: hit.name }), "success", 1500);
          return;
        }
        case "cost": {
          await this.fetchStats();
          const s = this.stats;
          const lines = [
            `**${this.t("slash.cost_title")}**`,
            `- ${this.t("cost.total")}: $${s.total_cost_usd.toFixed(4)}`,
            `- ${this.t("cost.in_out")}: ${s.total_input_tokens.toLocaleString()} in / ${s.total_output_tokens.toLocaleString()} out`,
            `- ${this.t("cost.cache_hit")}: ${s.cache_hit_pct}% (${s.total_cache_read_tokens.toLocaleString()} cached read)`,
            s.budget_usd > 0
              ? `- ${this.t("cost.budget")}: $${s.budget_usd} (${s.budget_used_pct}% used)`
              : `- ${this.t("cost.no_budget")}`,
            `- ${this.t("cost.context")}: ${((this.sessionUsage.context_used || this.sessionUsage.input_tokens || 0)/1000).toFixed(1)}K / ${(this.sessionUsage.context_limit/1000).toFixed(0)}K (${this.sessionUsage.context_used_pct}%)`,
          ];
          this._injectAssistantNote(lines.join("\n"));
          return;
        }
        case "config": this.openSettings(); return;
        case "stop":   if (this.streaming) this.stop(); return;
        default:
          this.toast(this.t("slash.unknown", { cmd }), "warn", 2000);
      }
    },

    // Inject a synthetic assistant bubble (markdown rendered) for slash output.
    // Not persisted — slash output is ephemeral, doesn't pollute session history.
    _injectAssistantNote(md) {
      this.messages.push({
        role: "assistant", text: md, html: this.mdRender(md),
        cost: "", model: "muselab", _ephemeral: true,
      });
      this.scrollToBottom(true);
    },

    // Suggest a few subdirs the user could fill in first, based on what's
    // missing. Order: health → work → money → people → notes (most common
    // first for a personal-archive use case).
    onboardingSubdirs() {
      const sp = this.contextInfo.subdir_present || {};
      const hints = {
        health: this.t("onboard.dir_health"),
        work:   this.t("onboard.dir_work"),
        money:  this.t("onboard.dir_money"),
        people: this.t("onboard.dir_people"),
        notes:  this.t("onboard.dir_notes"),
      };
      return ["health", "work", "money", "people", "notes"]
        .filter(k => sp[k])     // only show subdirs that actually exist
        .map(k => ({ name: k, hint: hints[k] }));
    },

    // Suggested first questions when the user has set things up but hasn't
    // chatted yet. Tailored a bit to what data they've dropped in.
    // Skill chips for the onboarding card — give a short, friendly example
    // prompt that triggers each known skill (matches the 7 presets in skills/).
    SKILL_TRIGGERS: [
      { name: "web-search",         label_zh: "查时效数据",  label_en: "live web fact",     prompt_zh: "查一下今天 USD 兑 CNY 的汇率，给出处" },
      { name: "markdown-formatter", label_zh: "整理 markdown", label_en: "clean markdown",   prompt_zh: "帮我把这段 markdown 整理一下" },
      { name: "mermaid-helper",     label_zh: "画架构图",    label_en: "draw a diagram",   prompt_zh: "画一张三栏 web 应用的架构图" },
      { name: "code-reviewer",      label_zh: "code review", label_en: "code review",      prompt_zh: "帮我 review 这段代码：" },
      { name: "citation-formatter", label_zh: "格式化引用",  label_en: "format a citation", prompt_zh: "把 DOI 10.1038/nature12345 格式化为 APA" },
      { name: "task-decomposer",    label_zh: "拆任务",      label_en: "decompose a goal", prompt_zh: "帮我把「6 个月内换工作」拆成可执行任务" },
      { name: "summary-distiller",  label_zh: "长文摘要",    label_en: "summarize",         prompt_zh: "总结这篇文章的要点" },
    ],

    skillSuggestions() {
      const loaded = new Set(this.settings.skills.map(s => s.name));
      const lang = this.lang;
      return this.SKILL_TRIGGERS
        .filter(t => loaded.has(t.name))
        .map(t => ({
          name: t.name,
          label: lang === "zh" ? t.label_zh : t.label_en,
          prompt: t.prompt_zh,   // prompt always Chinese (Muse responds in user's lang)
          description: lang === "zh" ? "触发 skill: " + t.name : "Triggers skill: " + t.name,
        }))
        .slice(0, 6);
    },

    // Filter the Settings → Skills grid by free-text search (name /
    // description / plugin source). Case-insensitive substring match.
    filteredSkills() {
      const q = (this.settings.skillFilter || "").trim().toLowerCase();
      if (!q) return this.settings.skills;
      return this.settings.skills.filter(s => {
        const hay = (s.name + " " + (s.description || "") + " " + (s.source || ""))
          .toLowerCase();
        return hay.includes(q);
      });
    },

    // "Try this" button on a skill card. For the 7 hand-crafted skills
    // (see SKILL_TRIGGERS), uses the concrete demo prompt. For other
    // skills (user-installed Claude Code skills + plugin skills), generates
    // a generic seed and focuses the chat input so the user can fill in
    // the rest. Closes the Settings modal first so the chat is visible.
    trySkill(sk) {
      const zh = this.lang === "zh";
      // Look up hand-crafted prompt by name; fall back to generic seed.
      const hand = (this.SKILL_TRIGGERS || []).find(t => t.name === sk.name);
      const prompt = hand
        ? hand.prompt_zh
        : (zh ? `用 ${sk.name} 帮我：` : `Use ${sk.name} to: `);
      // Close settings modal if open
      if (this.settings && this.settings.show) this.settings.show = false;
      // Close skills drawer if open
      if (this.skillsDrawerOpen) this.skillsDrawerOpen = false;
      this.input = prompt;
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) {
          ta.focus();
          // Put cursor at end so user can keep typing
          ta.selectionStart = ta.selectionEnd = ta.value.length;
          this.autoGrow(ta);
        }
      });
    },

    // Skills drawer (chat-input 🧩 entry). Reactive boolean so Alpine
    // re-renders on toggle.
    skillsDrawerOpen: false,
    toggleSkillsDrawer() {
      this.skillsDrawerOpen = !this.skillsDrawerOpen;
      // Refresh skills list each time the drawer opens — picks up newly
      // installed Claude Code skills without requiring a settings open.
      if (this.skillsDrawerOpen) this.loadSkills();
    },
    async loadSkills() {
      try {
        const r = await fetch("/api/settings/skills", { headers: this.hdr() });
        if (r.ok) {
          const data = await r.json();
          this.settings.skills = data.skills || [];
        }
      } catch (e) { /* network / first-boot — silent fail */ }
    },

    onboardingPrompts() {
      // Inspire prompts come from window.MUSELAB_INSPIRE_PROMPTS (a 30+
      // tagged bilingual list). Filter to those whose tags either match
      // an existing archive subdir or are tagged "general" (always-on).
      // Shuffle and slice — gives a fresh-feeling sample each time the
      // chat-empty state renders. _inspireSeed is bumped by
      // shuffleInspirePrompts() so the user can ask for "another round"
      // without reloading.
      const list = window.MUSELAB_INSPIRE_PROMPTS || [];
      const sp = this.contextInfo.subdir_present || {};
      const lang = this.lang;
      const eligible = list.filter(p => {
        if (!p.tags || p.tags.length === 0) return true;
        return p.tags.some(t => t === "general" || sp[t]);
      });
      // Seeded shuffle (Fisher-Yates with a tiny linear-congruential PRNG
      // seeded by _inspireSeed) — keeps the chosen set stable as Alpine
      // re-renders during typing, but flips on shuffleInspirePrompts().
      const seed = this._inspireSeed || 1;
      const a = eligible.slice();
      let s = seed;
      for (let i = a.length - 1; i > 0; i--) {
        s = (s * 1664525 + 1013904223) & 0xffffffff;
        const j = Math.abs(s) % (i + 1);
        [a[i], a[j]] = [a[j], a[i]];
      }
      return a.slice(0, 5).map(p => p[lang] || p.zh);
    },
    shuffleInspirePrompts() {
      // Bump the seed so onboardingPrompts() picks a different sample.
      // +1 each time; the LCG inside onboardingPrompts spreads it.
      this._inspireSeed = (this._inspireSeed || 1) + 1;
    },

    async quickNewNote() {
      const name = await this.prompt({
        title: this.t("preview.new_note_title"),
        body: this.t("preview.new_note_body"),
        value: "untitled.md",
      });
      if (!name) return;
      const trimmed = name.trim();
      if (!trimmed) return;
      // Create empty file at archive root
      const r = await fetch("/api/files/write", {
        method: "PUT",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ path: trimmed, content: "# " + trimmed.replace(/\.md$/, "") + "\n\n" }),
      });
      if (!r.ok) { this.toast(this.t("slash.failed"), "error"); return; }
      await this.loadRoot();
      await this.openFile({ path: trimmed, name: trimmed });
      this.editing = true;
      this.toast(this.t("toast.saved"), "success", 1200);
    },

    useSuggestedPrompt(q) {
      this.input = q;
      if (this.$refs.chatInput) {
        this.$refs.chatInput.focus();
        this.autoGrow(this.$refs.chatInput);
      }
    },

    claudeMdChipTitle() {
      const i = this.contextInfo;
      if (!i.claude_md_exists) {
        return this.t("ctx.no_claude_md", { root: i.archive_root });
      }
      const d = i.claude_md_mtime ? new Date(i.claude_md_mtime * 1000).toLocaleDateString() : "";
      return this.t("ctx.claude_md_tip", { root: i.archive_root, date: d });
    },
    openClaudeMdHelp() {
      this.modal = {
        show: true,
        title: this.t("ctx.no_claude_md_title"),
        body: this.t("ctx.no_claude_md_body", { root: this.contextInfo.archive_root }),
        input: null, danger: false,
        okText: this.t("btn.confirm"),
        confirm: () => { this.modal.show = false; },
        cancel: () => { this.modal.show = false; },
      };
    },

    // Start a dedicated profile-intake session: Muse walks the user
    // through CLAUDE.md conversationally, asking 1-2 questions per turn
    // and using Edit to save answers. Backend creates the session with
    // a curator-style system prompt (see backend/prompts.py) and seeds
    // the template file if missing. This is the primary path for
    // "let me set up / refresh my profile" — direct file editing is
    // deliberately not surfaced (worse UX for personal-info data entry).
    async startProfileIntake() {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "设置档案" : "Set up profile",
        body: zh
          ? "将新建一个 [设置档案] 会话，Muse 会通过聊天逐项问你（不是填表单）。任意一步都可以跳过或随时停下。"
          : "Will create a new [Set up profile] session — Muse asks you questions conversationally (no form). You can skip any question or stop anytime.",
        okText: zh ? "开始" : "Start",
      });
      if (!ok) return;
      const r = await fetch("/api/chat/sessions/profile-intake", {
        method: "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ model: this.model }),
      });
      if (!r.ok) {
        this.toast(zh
          ? "创建失败：" + (await r.text())
          : "Create failed: " + (await r.text()), "error", 4000);
        return;
      }
      const meta = await r.json();
      await this.refreshSessions();
      this.currentId = meta.id;
      const st = this._ensureTabState(meta.id);
      st.messages.length = 0;
      st._loaded = true;
      this._activateTabState(meta.id);
      if (!this.openTabIds.includes(meta.id)) this.openTabIds.push(meta.id);
      this._fetchTabUsage(meta.id);
      this.savePrefs();
      // Auto-send the intake seed message — the system prompt tells
      // Muse to begin by reading the existing CLAUDE.md and asking
      // about whichever sections are still blank.
      const lang = this.lang === "en" ? "en" : "zh";
      const initialMsg = (meta.initial_message && meta.initial_message[lang])
        || meta.initial_message?.zh || "开始";
      this.input = initialMsg;
      this.$nextTick(() => { this.send(); });
    },

    // Welcome card — shown until user dismisses it (then never again on
    // this device). Suppressed while session is loading to avoid a flicker
    // between the skeleton and the real cards.
    showWelcomeCard() {
      if (this._welcomeDismissed) return false;
      if (this.messagesLoading) return false;
      // Only on the empty-chat screen — once the user has sent a turn,
      // they've clearly oriented themselves and the welcome card is noise.
      if (this.messages && this.messages.length) return false;
      return true;
    },
    dismissWelcome() {
      this._welcomeDismissed = true;
      try { localStorage.setItem("muselab_welcome_dismissed", "1"); }
      catch (e) { /* private mode / quota / no-op */ }
    },

    // Pretty-print a USD amount for the cost badge.
    //   0          → "$0"
    //   0.0023     → "0.23¢"   (cents form for sub-dollar)
    //   0.45       → "45¢"
    //   1.234      → "$1.23"
    //   12.34      → "$12.34"
    fmtCost(usd) {
      if (!usd || usd < 0) return "$0";
      if (usd < 0.01) {
        const c = usd * 100;
        return (c < 0.1 ? c.toFixed(2) : c.toFixed(1)) + "¢";
      }
      if (usd < 1) return Math.round(usd * 100) + "¢";
      return "$" + usd.toFixed(2);
    },

    // Header badge: show accumulated input/output tokens instead of $.
    // 1.2K / 350 format — concise, intuitive (in / out). Use M for ≥1M.
    fmtTokens(n) {
      n = n || 0;
      if (n < 1000) return n.toString();
      if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "K";
      return (n / 1_000_000).toFixed(2) + "M";
    },
    tokenBadgeText() {
      const s = this.stats;
      return `↓ ${this.fmtTokens(s.total_input_tokens)} · ↑ ${this.fmtTokens(s.total_output_tokens)}`;
    },

    costBadgeTitle() {
      const s = this.stats;
      const parts = [
        `${s.total_input_tokens.toLocaleString()} in / ${s.total_output_tokens.toLocaleString()} out  ·  ${s.total_messages} msg`,
        `cache hit ${s.cache_hit_pct}%  ·  cache_read ${s.total_cache_read_tokens.toLocaleString()}`,
        `${this.t("cost.total")}: $${s.total_cost_usd.toFixed(4)}`,
      ];
      if (s.budget_usd > 0) parts.push(`${this.t("cost.budget")} ${s.budget_used_pct}% of $${s.budget_usd}`);
      return parts.join("\n");
    },
    ctxMeterTitle() {
      const u = this.sessionUsage;
      return `${this.t("ctx.tip_line1")}\n` +
             `${(u.context_used || u.input_tokens || 0).toLocaleString()} / ${u.context_limit.toLocaleString()} tokens (${u.context_used_pct}%)\n\n` +
             `${this.t("ctx.tip_line2")}`;
    },
    ctxMeterLabel() {
      const limit = this.sessionUsage.context_limit || 0;
      // Pre-fetch state — backend hasn't told us the real limit yet.
      // Show a placeholder rather than rendering "0K / 0K · NaN%".
      if (!limit) return this.lang === "zh" ? "上下文 …" : "Context …";
      const pct = this.sessionUsage.context_used_pct || 0;
      const usedTokens = (this.sessionUsage.context_used != null)
        ? this.sessionUsage.context_used
        : (this.sessionUsage.input_tokens || 0)
          + (this.sessionUsage.cache_read_tokens || 0)
          + (this.sessionUsage.cache_creation_tokens || 0);
      const cachedTokens = (this.sessionUsage.cache_read_tokens || 0)
                         + (this.sessionUsage.cache_creation_tokens || 0);
      const usedK = (usedTokens / 1000).toFixed(1);
      const cachedK = (cachedTokens / 1000).toFixed(1);
      const limitK = (limit / 1000).toFixed(0);
      const args = { used: usedK, limit: limitK, pct, cached: cachedK };
      if (pct >= 90) return this.t("ctx.danger", args);
      if (pct >= 70) return this.t("ctx.warn",   args);
      return this.t("ctx.normal", args);
    },
    // Real compact: a) make sure the OLD session has been summarized in chat,
    // b) fork it, c) the fork inherits the summary as starting context.
    // Easier path: just send a /compact instruction to the CURRENT session that
    // asks the model to produce a self-contained summary, which the user can
    // copy / use as basis. The "true" compact is a feature of the underlying
    // CLI we don't have direct API for, so we implement it as a synthesized
    // summarize-and-fork workflow.
    async runCompact() {
      if (!this.currentId) return;
      if (this.streaming) {
        this.toast(this.t("ctx.compact_wait_streaming"), "warn", 2500);
        return;
      }
      // Empty-session guard. Frontend `messages` may be transiently empty
      // right after a page refresh (loadSession is still running async),
      // so fall back to the session metadata's message_count from the
      // backend — that survives reloads.
      const hasFrontendContent = this.messages.some(
        m => m.role === "assistant" && m.text);
      const meta = this.sessions.find(s => s.id === this.currentId);
      const backendCount = (meta && meta.message_count) || 0;
      if (!hasFrontendContent && backendCount < 2) {
        this.toast(this.t("ctx.compact_empty"), "warn", 2500);
        return;
      }

      // Native compact: send "/compact" to CLI via SDK, which writes
      // compact_boundary + isCompactSummary to the session JSONL. Lossless,
      // preserves tool use history, same session ID. Old self-implemented
      // summarize-and-fork is gone — it was lossy and unnecessary once we
      // realized the SDK forwards slash commands to CLI natively.
      this._compacting = true;
      // ctx-meter shows a persistent pulsing banner while _compacting is true
      // (CSS-only, see .ctx-meter.compacting). A short toast confirms the kick
      // — the long banner is what the user actually watches, not a 2-min toast.
      this.toast(this.lang === "zh" ? "📦 开始压缩..." : "📦 Compacting…", "info", 2000);
      // Scroll to the bottom so the .compact-pending placeholder bubble (added
      // at the tail of the message list via x-show="_compacting") is in view
      // the moment compact kicks. Without this the user could be scrolled up
      // mid-history and never see the bottom indicator at all.
      this.$nextTick(() => this.scrollToBottom(true));

      try {
        const r = await fetch(`/api/chat/sessions/${this.currentId}/native-compact`,
                                { method: "POST", headers: this.hdr() });
        if (!r.ok) {
          const txt = await r.text();
          this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + txt, "error", 5000);
          return;
        }
        // Reload current session so the new compact-marker shows up.
        await this.loadSession(this.currentId);
        await this.refreshSessions();
        // Refresh ctx-meter — sessionUsage is only auto-updated on stream
        // 'done' events, so without this the meter shows the pre-compact
        // (large) value until the user sends a new message.
        await this._refreshCtxMeter();
        this.toast(this.lang === "zh" ? "📦 压缩完成" : "📦 Compacted", "success", 2000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "压缩失败：" : "Compact failed: ") + e.message, "error", 5000);
      } finally {
        this._compacting = false;
        // Queued messages: ask the user before flushing. Auto-sending all
        // queued messages was surprising — the user could end up with 3
        // unexpected user-bubbles when they thought compact would let them
        // re-evaluate context first.
        if (this._compactQueue.length > 0) {
          const n = this._compactQueue.length;
          const previewLines = this._compactQueue
            .map((q, i) => `${i + 1}. ${(q.text || "").slice(0, 50)}`)
            .join("\n");
          const ok = await this.confirm({
            title: this.lang === "zh"
              ? `压缩期间排队了 ${n} 条消息`
              : `${n} message(s) queued during compact`,
            body: this.lang === "zh"
              ? `要依次发送吗？取消会全部丢弃。\n\n${previewLines}`
              : `Send them sequentially? Cancel discards all.\n\n${previewLines}`,
            okText: this.lang === "zh" ? `发送 ${n} 条` : `Send ${n}`,
            cancelText: this.lang === "zh" ? "丢弃" : "Discard",
          });
          if (ok) {
            while (this._compactQueue.length > 0) {
              const q = this._compactQueue.shift();
              this.input = q.text;
              this.pendingImages = q.pendingImages || [];
              this.pendingDocs = q.pendingDocs || [];
              await this.send();
            }
          } else {
            this._compactQueue.length = 0;
          }
        }
      }
    },

    onChatArrowUp(ev) {
      // 1. If a mention/slash popup is open, ↑ navigates it (preserves
      //    the prior keymap).
      if (this.mentionShow || this.slashShow) {
        this._navPop(-1);
        ev.preventDefault();
        return;
      }
      // 2. Empty input → recall the most recent user message so the user
      //    can edit + re-send (Slack/Cursor/iTerm/zsh style).
      if (!this.input.trim()) {
        const msgs = this.messages || [];
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i];
          if (m && m.role === "user" && m.text) {
            this.input = m.text;
            ev.preventDefault();
            this.$nextTick(() => {
              const ta = this.$refs.chatInput;
              if (ta) {
                ta.focus();
                const len = this.input.length;
                ta.setSelectionRange(len, len);
                this.autoGrow(ta);
              }
            });
            return;
          }
        }
      }
      // Otherwise let the browser handle ↑ (cursor up inside textarea).
    },

    onChatInput(ev) {
      const ta = ev.target;
      const pos = ta.selectionStart;
      const text = this.input.slice(0, pos);

      // Slash command palette — only when input starts with '/' (no leading space).
      if (text.startsWith("/")) {
        const q = text.slice(1).toLowerCase();
        // Hide once user typed a space (means they're past the command name)
        if (/\s/.test(q)) { this.slashShow = false; }
        else {
          this.slashResults = this.SLASH_CMDS.filter(c => c.name.startsWith(q));
          this.slashIdx = 0;
          this.slashShow = this.slashResults.length > 0;
          this.slashAnchor = 0;
        }
        this.mentionShow = false;
        return;
      } else {
        this.slashShow = false;
      }

      const at = text.lastIndexOf("@");
      if (at < 0 || (at > 0 && /\S/.test(text[at - 1]))) { this.mentionShow = false; return; }
      const query = text.slice(at + 1);
      if (/\s/.test(query)) { this.mentionShow = false; return; }
      this.mentionAnchor = at;
      this.fetchMention(query);
    },
    async fetchMention(q) {
      if (q.length === 0) {
        this.mentionResults = (await this.fetchChildren("")).slice(0, 8);
      } else {
        const r = await fetch("/api/files/search?q=" + encodeURIComponent(q) + "&limit=15", { headers: this.hdr() });
        const d = r.ok ? await r.json() : { entries: [] };
        this.mentionResults = d.entries.filter(e => !e.is_dir).slice(0, 12);
      }
      this.mentionIdx = 0;
      this.mentionShow = true;
    },
    pickMention(i) {
      const idx = (i ?? this.mentionIdx);
      const item = this.mentionResults[idx];
      if (!item) return;
      const ta = this.$refs.chatInput;
      const before = this.input.slice(0, this.mentionAnchor);
      const after = this.input.slice(ta.selectionStart);
      this.input = before + "@" + item.path + " " + after;
      this.mentionShow = false;
      this.$nextTick(() => {
        const newPos = (before + "@" + item.path + " ").length;
        ta.setSelectionRange(newPos, newPos);
        ta.focus();
      });
    },

    // ===== chat =====
    onEnter(ev) {
      // 中文 / 日文 输入法在选词阶段也会触发 Enter (keyCode=229 / isComposing=true)。
      // 那时不应该当成"发送"，让 IME 自己处理。
      if (ev.isComposing || ev.keyCode === 229) return;
      if (this.mentionShow) { this.pickMention(); return; }
      if (ev.shiftKey) { this.input += "\n"; return; }
      // While a turn is streaming, the textarea is intentionally still
      // editable (so users can pre-type the next prompt). But Enter must
      // NOT send a second turn concurrently — fall through to newline
      // insertion instead. Once streaming ends, Enter goes back to send().
      // (Touch path below already inserts newlines unconditionally.)
      if (this.streaming) {
        const ta = this.$refs.chatInput;
        if (ta) {
          const s = ta.selectionStart, e = ta.selectionEnd;
          this.input = this.input.slice(0, s) + "\n" + this.input.slice(e);
          this.$nextTick(() => {
            ta.setSelectionRange(s + 1, s + 1);
            this.autoGrow(ta);
          });
        } else {
          this.input += "\n";
        }
        return;
      }
      // Mobile / touch screens: Enter inserts a newline; send is via the
      // button only. Desktop users get the keyboard-first flow.
      const isTouch = (window.matchMedia
                        && window.matchMedia("(pointer: coarse)").matches)
                      || window.innerWidth < 768;
      if (isTouch) {
        const ta = this.$refs.chatInput;
        if (ta) {
          const s = ta.selectionStart, e = ta.selectionEnd;
          this.input = this.input.slice(0, s) + "\n" + this.input.slice(e);
          this.$nextTick(() => {
            ta.setSelectionRange(s + 1, s + 1);
            this.autoGrow(ta);
          });
        } else {
          this.input += "\n";
        }
        return;
      }
      this.send();
    },
    onChatScroll() {
      const el = this.$refs.chatBody;
      if (!el) return;
      this.atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
    },
    scrollToBottom(force) {
      this.$nextTick(() => {
        const el = this.$refs.chatBody;
        if (!el) return;
        if (force || this.atBottom) {
          el.scrollTop = el.scrollHeight;
          this.atBottom = true;
        }
      });
    },

    async send(opts = {}) {
      // Reconnect mode: skip user-input validation + user-msg push.
      // Used by _reconnectActiveTurn() when loadSession discovers an
      // in-flight background turn on the current session — we just want
      // to subscribe to the existing TurnBroadcast (empty prompt =
      // attach), all the EventSource handlers below stay the same.
      const isReconnect = !!opts.reconnect;
      const text = isReconnect ? "" : this.input.trim();
      // Slash command: intercept BEFORE hitting the SDK. /word or /word arg
      if (!isReconnect && text.startsWith("/") && !this.streaming) {
        const m = text.match(/^\/(\w+)(?:\s+(.*))?$/);
        if (m) {
          this.input = "";
          this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
          await this._runSlash(m[1], m[2] || "");
          return;
        }
      }
      const readyImages = isReconnect ? []
        : this.pendingImages.filter(im => im.id && !im.error);
      const readyDocs = isReconnect ? []
        : this.pendingDocs.filter(d => d.id && !d.error);
      if (!isReconnect) {
        if ((!text && !readyImages.length && !readyDocs.length)
            || this.streaming || !this.currentId) return;
      } else {
        if (this.streaming || !this.currentId) return;
      }
      // Compact in progress → queue the message, drained when compact finishes.
      // Users keep typing & sending without waiting; the message stays out of
      // the visible transcript until it's actually dispatched (avoids the
      // "sent but no response" UX where the bubble sits there for 90 s).
      if (!isReconnect && this._compacting) {
        this._compactQueue.push({
          text,
          pendingImages: this.pendingImages.slice(),
          pendingDocs: this.pendingDocs.slice(),
        });
        this.pendingImages = [];
        this.pendingDocs = [];
        this.input = "";
        this.$nextTick(() => { if (this.$refs.chatInput) this.autoGrow(this.$refs.chatInput); });
        this.toast(this.lang === "zh"
                    ? `📦 压缩中，消息已排队（${this._compactQueue.length}）`
                    : `📦 Compact in progress, message queued (${this._compactQueue.length})`,
                    "info", 2200);
        return;
      }
      // If any attachment is still mid-upload, silently wait for it to
      // finish before kicking off the turn. Per 2026-05-21 user feedback
      // the 30 s deadline was removed — on 4G / weak Wi-Fi an iPhone-
      // resolution photo can legitimately take longer than 30 s to upload,
      // and falling back to "wait_upload" toast + bail forced the user to
      // re-tap send for no good reason. We keep polling indefinitely; the
      // upload itself has its own per-request HTTP timeout (fetch network
      // stack default) which will mark `entry.error = true` if the request
      // fails permanently — that breaks `stillUploading()` so this loop
      // exits naturally with a red-border chip the user can remove + retry.
      // The send button stays disabled (_sendWaitingForUpload) so a double-
      // tap can't enqueue a second send while we wait.
      if (!isReconnect) {
        const stillUploading = () =>
          this.pendingImages.some(im => im.uploading)
            || this.pendingDocs.some(d => d.uploading);
        if (stillUploading()) {
          this._sendWaitingForUpload = true;
          while (stillUploading()) {
            await new Promise(r => setTimeout(r, 80));
          }
          this._sendWaitingForUpload = false;
        }
      }
      // Reconnect mode skips pushing a user msg — the backend already
      // has the user prompt from the original turn, and the
      // broadcast-rebuild on `/sessions/{sid}` GET produced it for us.
      if (!isReconnect) {
        this.messages.push({
          role: "user", text,
          images: readyImages.map(im => ({ preview: im.preview })),
          docs: readyDocs.map(d => ({ name: d.name, kind: d.kind })),
        });
      } else {
        // Truncate the in-flight portion: the backend broadcast will
        // replay every event from the start of the turn (thinking +
        // assistant + tool_use + ...), and our handlers below push
        // them as messages. The mid-turn rebuild that loadSession
        // already populated would otherwise be duplicated, so drop
        // anything after the most recent user msg before the replay
        // fills it back in.
        const roles = this.messages.map(m => m.role);
        const lastUserIdx = roles.lastIndexOf("user");
        if (lastUserIdx >= 0 && lastUserIdx < this.messages.length - 1) {
          this.messages.splice(lastUserIdx + 1);
        }
      }
      // Single id-list for both kinds — backend dispatches by stored kind.
      const attachIds = isReconnect ? [] : [
        ...readyImages.map(im => im.id),
        ...readyDocs.map(d => d.id),
      ];
      if (!isReconnect) {
        this.pendingImages = [];
        this.pendingDocs = [];
        this.input = "";
        this.$nextTick(() => {
          const ta = this.$refs.chatInput;
          if (ta) { this.autoGrow(ta); ta.focus(); }
        });
      }
      this.mentionShow = false;
      // Capture per-stream tab state once at send time. All event handlers
      // below write to streamState (the ORIGIN tab) instead of `this`, so the
      // stream keeps targeting the right tab even if the user switches to a
      // different tab mid-reply. Root state (`this.streaming` etc.) is only
      // mirrored when the origin tab is still the active one.
      const streamSid = this.currentId;
      const streamState = this._ensureTabState(streamSid);

      streamState.streaming = true; this.streaming = true;
      streamState.streamingModel = this.model;
      this.streamingModel = this.model;   // 锁定 — pending bubble 用它，不跟着 dropdown
      // Tick elapsed time so user sees "thinking · 3.2s" when first token is slow.
      streamState._streamStartedAt = Date.now();
      this._streamStartedAt = streamState._streamStartedAt;
      streamState.streamElapsed = 0; this.streamElapsed = 0;
      if (streamState._streamTimer) clearInterval(streamState._streamTimer);
      streamState._streamTimer = setInterval(() => {
        const elapsed = (Date.now() - streamState._streamStartedAt) / 1000;
        streamState.streamElapsed = elapsed;
        if (this.currentId === streamSid) this.streamElapsed = elapsed;
      }, 200);
      this._streamTimer = streamState._streamTimer;
      this.atBottom = true;
      this.scrollToBottom(true);

      const url = "/api/chat/stream"
        + "?prompt=" + encodeURIComponent(text)
        + "&session_id=" + encodeURIComponent(streamSid)
        + "&model=" + encodeURIComponent(this.model)
        + "&permission=" + encodeURIComponent(this.permission)
        + (attachIds.length ? "&image_ids=" + encodeURIComponent(attachIds.join(",")) : "")
        + "&token=" + encodeURIComponent(this.token);
      const es = new EventSource(url);
      streamState.es = es; this.es = es;
      // Reset auto-reconnect counter on every fresh stream open. Survives
      // across `send({reconnect:true})` chains because we reset to 0 only
      // when the connection actually succeeds (es.onopen below) — failed
      // reconnects keep the counter ticking so we give up after N tries
      // instead of looping forever on a dead backend.
      es.onopen = () => { streamState._reconnectAttempts = 0; };

      // Active assistant bubble pointer. Text events open / extend it; tool /
      // thinking events close it so subsequent text starts a fresh bubble.
      // curBubble is a direct OBJECT reference — survives tab switches because
      // it lives inside streamState.messages (same array regardless of which
      // tab is active).
      let curBubble = null;
      let acc = "";
      const modelForBubble = this.model;
      // Scroll only if the active tab is the one receiving the stream;
      // otherwise we'd yank the user away from whatever they're reading.
      const _scrollIfActive = () => {
        if (this.currentId === streamSid) this.scrollToBottom(false);
      };
      const openAsst = () => {
        if (curBubble) return;
        const bubble = { role: "assistant", text: "", html: "", cost: "", model: modelForBubble };
        streamState.messages.push(bubble);
        // CRITICAL: pull the reactive-wrapped object back out of the
        // array, not the raw `bubble` reference. Alpine 3 (and Vue 3
        // reactivity under it) intercepts at the array level — accessing
        // `messages[i]` returns the proxy that has dependency tracking
        // wired up. Mutating the raw `bubble` directly bypasses the
        // proxy, so later changes to `curBubble.html` aren't seen by
        // the `x-html="m.html"` effect. Symptom that triggered this fix:
        // the first text_delta showed up (push triggered re-render with
        // initial state) but every subsequent chunk only became visible
        // after switching tabs (which forced Alpine to re-read the
        // array through the proxy).
        curBubble = streamState.messages[streamState.messages.length - 1];
        acc = "";
      };
      // Throttle markdown rendering during fast token streams. mdRender is
      // O(content length) and re-runs on every chunk; on long replies that's
      // hundreds of full re-parses. 80ms cap keeps UI smooth while still
      // feeling realtime. flushRender() forces a final render on close/done.
      const RENDER_MIN_INTERVAL = 80;
      let lastRender = 0;
      let pendingTimer = null;
      const renderNow = () => {
        if (!curBubble) { pendingTimer = null; return; }
        curBubble.html = this.mdRender(acc);
        lastRender = Date.now();
        pendingTimer = null;
      };
      const scheduleRender = () => {
        const since = Date.now() - lastRender;
        if (since >= RENDER_MIN_INTERVAL) {
          renderNow();
        } else if (!pendingTimer) {
          pendingTimer = setTimeout(renderNow, RENDER_MIN_INTERVAL - since);
        }
      };
      const flushRender = () => {
        if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
        if (curBubble) renderNow();
      };
      const closeAsst = () => { flushRender(); curBubble = null; acc = ""; };

      es.addEventListener("text", ev => {
        const d = JSON.parse(ev.data);
        openAsst();
        acc += d.text;
        curBubble.text = acc;
        // Direct synchronous render — drop the 80ms throttle / setTimeout
        // path because it was causing "first chunk shows then frozen
        // until tab switch" symptom. Throttling was a perf nicety for
        // long replies but reactivity was unreliable through the timer.
        curBubble.html = this.mdRender(acc);
        _scrollIfActive();
      });
      es.addEventListener("thinking", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        // Backend yields one SSE event per thinking_delta. Coalesce them
        // into the most recent thinking message so we see ONE block per
        // reasoning segment, not N tiny ones. If the tail isn't a thinking
        // message (e.g. previous was tool_use), start a new one.
        const msgs = streamState.messages;
        const last = msgs[msgs.length - 1];
        let pushed = false;
        if (last && last.role === "thinking") {
          last.text = (last.text || "") + (d.text || "");
        } else {
          msgs.push({ role: "thinking", text: d.text || "" });
          pushed = true;
        }

        _scrollIfActive();
      });
      es.addEventListener("tool_use", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        const msg = { role: "tool_use", name: d.name, summary: d.summary, input: d.input };
        if (d.todos != null) msg.todos = d.todos;
        if (d.task != null) msg.task = d.task;
        if (d.plan != null) msg.plan = d.plan;
        streamState.messages.push(msg);
        // File-mutating tools invalidate any open preview of the same file.
        // Bump previewVersion → rawUrl picks up a new ?_v= → iframe reloads;
        // _reloadPreviewIfDirty re-fetches md/text contents inline.
        if (["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(d.name)) {
          const fp = (d.input && (d.input.file_path
                                    || d.input.notebook_path)) || "";
          this._maybeReloadPreview(fp);
        }

        _scrollIfActive();
      });
      es.addEventListener("tool_result", ev => {
        const d = JSON.parse(ev.data);
        // `text` (up to 50KB) drives the "expand" affordance and per-tool
        // rich renderers (Bash terminal / Read with gutter / WebFetch card).
        // `tool_name` lets the FE pick a renderer without scanning backwards
        // for the matching tool_use. `bash` is pre-parsed exit_code +
        // stdout/stderr when the result came from a Bash call.
        streamState.messages.push({
          role: "tool_result",
          id: d.id,
          tool_name: d.tool_name || "",
          preview: d.preview,
          text: d.text || "",
          truncated: d.truncated,
          text_truncated: d.text_truncated,
          is_error: d.is_error,
          bash: d.bash || null,
        });

        _scrollIfActive();
      });
      es.addEventListener("ask_user_question", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        streamState.messages.push({
          role: "ask_user_question",
          id: d.id,
          questions: d.questions,
          pendingAnswers: {},
          submitted: false,
        });

        _scrollIfActive();
      });
      es.addEventListener("permission_request", ev => {
        closeAsst();
        const d = JSON.parse(ev.data);
        streamState.messages.push({
          role: "permission_request",
          id: d.id,
          tool: d.tool,
          summary: d.summary,
          resolved: false,
          decision: null,
        });

        _scrollIfActive();
      });
      const _stopTimer = () => {
        if (streamState._streamTimer) {
          clearInterval(streamState._streamTimer);
          streamState._streamTimer = null;
        }
        streamState.streamElapsed = 0;
        if (this.currentId === streamSid) {
          this._streamTimer = null;
          this.streamElapsed = 0;
        }
      };
      // Mark the stream done for the ORIGIN tab. If the user is on a
      // different tab, we still update tabState[streamSid] silently — they'll
      // see the final state when they switch back.
      const _markDone = () => {
        streamState.streaming = false;
        streamState.es = null;
        // Stamp the tail block of the just-finished turn with a
        // completion timestamp. A "turn" = a contiguous run of muse-
        // side messages between two user messages; only its tail msg
        // carries .ts, and the .turn-footer in index.html renders
        // "HH:MM" off it. Walk from the end backwards to find the
        // last non-user message and stamp it (idempotent — we don't
        // overwrite an existing ts so reconnect scenarios stay clean).
        // Walking backwards is cheap and avoids needing to track
        // turn boundaries explicitly.
        const _now = Date.now();
        for (let k = streamState.messages.length - 1; k >= 0; k--) {
          const m = streamState.messages[k];
          if (m.role === "user") break;          // entered the previous turn
          if (!m.ts) { m.ts = _now; break; }     // found the tail of this turn
          break;                                  // already stamped — nothing to do
        }
        if (this.currentId === streamSid) {
          this.streaming = false;
          this.es = null;
          // textarea was :disabled while streaming → focus during stream was
          // a no-op. Re-focus now so the user can immediately type the next
          // message (supports rapid-fire conversation).
          this.$nextTick(() => {
            const ta = this.$refs.chatInput;
            if (ta && !ta.disabled) ta.focus();
          });
        }
      };
      es.addEventListener("done", ev => {
        flushRender();
        const d = JSON.parse(ev.data);
        if (d.total_cost_usd != null && curBubble) {
          curBubble.cost = "$" + d.total_cost_usd.toFixed(4);
        }
        if (d.stats) this.stats = { ...this.stats, ...d.stats };
        if (d.session_usage) {
          Object.assign(streamState.sessionUsage, d.session_usage);
          if (this.currentId === streamSid) this.sessionUsage = streamState.sessionUsage;
        }
        if (d.budget_usd > 0 && d.budget_used_pct >= 90 && !this._budgetWarned) {
          this._budgetWarned = true;
          this.toast(this.t("cost.budget_warn", { pct: d.budget_used_pct, usd: d.budget_usd }),
                      "warn", 5000);
        }
        // Context window handling — two-tier:
        //   85-94%: one-shot toast "compact now?" (user decides)
        //   ≥95%:   silent auto-compact (don't let user hit hard limit)
        // _ctxWarned keys by streamSid so a new session starts fresh.
        this._ctxWarned = this._ctxWarned || {};
        this._autoCompacted = this._autoCompacted || {};
        const ctxPct = d.session_usage && d.session_usage.context_used_pct;
        if (ctxPct >= 95 && !this._autoCompacted[streamSid] && !this._compacting) {
          this._autoCompacted[streamSid] = true;
          // Schedule on next tick so the stream's done handler fully
          // unwinds first (runCompact checks `streaming` flag).
          this.$nextTick(() => {
            const zh = this.lang === "zh";
            this.toast(zh ? `上下文 ${Math.round(ctxPct)}%，自动压缩中…`
                          : `Context ${Math.round(ctxPct)}% — auto-compacting…`,
                       "info", 3000);
            this.runCompact();
          });
        } else if (ctxPct >= 85 && ctxPct < 95 && !this._ctxWarned[streamSid]) {
          this._ctxWarned[streamSid] = true;
          this.toast(
            this.t("ctx.window_warn", { pct: Math.round(ctxPct) }),
            "warn", 6000,
            { label: this.t("ctx.window_warn_action"), onClick: () => this.runCompact() },
          );
        }
        es.close(); _markDone(); _stopTimer();
        this.refreshSessions();
        if (this.currentId === streamSid) {
          this.$nextTick(() => this.highlightCode(".chat-body"));
        }
      });
      es.addEventListener("error", ev => {
        flushRender();
        // Two distinct error paths share this handler:
        //   1. Server-sent `event: error` (well-formed JSON in ev.data) —
        //      a real turn failure (vendor 401, quota, 30-min timeout).
        //      Retrying the same SSE won't help; mark the user msg failed
        //      so the ↻ button shows, and the user can edit + resend.
        //   2. Transport-level disconnect (ev.data undefined → JSON.parse
        //      throws) — network blip, sleep/wake, server restart, etc.
        //      The backend's TurnBroadcast survives client disconnect, so
        //      we can transparently re-subscribe via _checkActiveTurn().
        //      Capped at 3 attempts with exponential backoff so a truly
        //      dead backend doesn't loop forever.
        let serverError = null;
        let errKind = "unknown";
        let errCta = "retry";
        let errRetryable = true;
        try {
          const d = JSON.parse(ev.data);
          if (d && d.error) serverError = d.error;
          if (d && d.kind) errKind = d.kind;
          if (d && typeof d.cta === "string") errCta = d.cta;
          if (d && typeof d.retryable === "boolean") errRetryable = d.retryable;
        } catch (_) {
          // ev.data missing → transport-level. Fall through to auto-retry.
        }
        const markUserFailed = () => {
          for (let i = streamState.messages.length - 1; i >= 0; i--) {
            const m = streamState.messages[i];
            if (m.role === "user") {
              m._failed = true;
              // Stash the classification so the FE can render a useful
              // CTA button under the failed bubble (Open Settings on auth,
              // Switch model on quota, Compact on cross-vendor signature).
              m._error_kind = errKind;
              m._error_cta = errCta;
              m._error_retryable = errRetryable;
              m._error_text = serverError || "";
              break;
            }
          }
        };

        if (serverError) {
          this.toast(this._humanizeStreamError(serverError), "error", 6000);
          markUserFailed();
          es.close(); _markDone(); _stopTimer();
          return;
        }

        // ---- Transport-level: auto-retry path ----
        const MAX_ATTEMPTS = 3;
        const attempts = (streamState._reconnectAttempts = (streamState._reconnectAttempts || 0) + 1);

        // Always close the old ES — leaving it triggers the browser's own
        // auto-reconnect (every ~3 s) on top of ours, which would race.
        try { es.close(); } catch (_) {}

        if (attempts > MAX_ATTEMPTS) {
          // Given up. Surface manual retry UI.
          this.toast(this.lang === "zh"
                      ? "和 Muse 的连接断开了，重试一下"
                      : "Lost connection to Muse — try again",
                      "error");
          markUserFailed();
          _markDone(); _stopTimer();
          return;
        }

        // Exponential backoff: 800 ms, 1.6 s, 3.2 s. _checkActiveTurn
        // confirms the backend turn is still in flight before opening a
        // fresh SSE — if the turn finished cleanly while we were
        // disconnected, it loads the session view from disk instead, so
        // the user sees the completed reply rather than an in-progress
        // bubble that never resolves.
        const delay = 800 * Math.pow(2, attempts - 1);
        setTimeout(async () => {
          // User switched tabs / cancelled / page already navigated away?
          // Don't surprise them by re-opening a stream they no longer want.
          if (this.currentId !== streamSid) { _markDone(); _stopTimer(); return; }
          // streamState.streaming is still true from initial send(); use
          // it as the in-flight gate _checkActiveTurn checks internally.
          try {
            const r = await fetch(`/api/chat/sessions/${streamSid}/active`,
                                    { headers: this.hdr() });
            if (!r.ok) throw new Error("active probe failed");
            const d = await r.json();
            if (!d.active) {
              // Backend turn already finished while we were disconnected.
              // Refresh session from disk to pick up the completed reply.
              _markDone(); _stopTimer();
              if (this.currentId === streamSid) this.loadSession(streamSid);
              streamState._reconnectAttempts = 0;
              return;
            }
            // Re-subscribe via the existing reconnect plumbing.
            // streaming flag must be cleared first or send() bails as
            // "already streaming."
            streamState.streaming = false;
            this.streaming = false;
            this.send({ reconnect: true });
          } catch (_e) {
            // Probe failed — try again on next error tick (counter will
            // continue incrementing until MAX_ATTEMPTS). Force a fresh
            // error event by manufacturing one: easiest is to schedule
            // another setTimeout that mimics this branch but bypasses
            // the EventSource. Cheaper: just bump the counter and let
            // the next real error fire normally. Bail here.
            _markDone(); _stopTimer();
            if (attempts >= MAX_ATTEMPTS) {
              this.toast(this.lang === "zh"
                          ? "和 Muse 的连接断开了，重试一下"
                          : "Lost connection to Muse — try again",
                          "error");
              markUserFailed();
            } else {
              // Schedule next retry ourselves since no new ES error will fire.
              setTimeout(() => {
                if (this.currentId !== streamSid) return;
                streamState.streaming = false;
                this.streaming = false;
                this.send({ reconnect: true });
              }, 800 * Math.pow(2, attempts));
            }
          }
        }, delay);
      });
      es.addEventListener("cancelled", () => {
        flushRender();
        this.toast(this.lang === "zh" ? "已中断" : "Interrupted", "warn", 2000);
        es.close(); _markDone(); _stopTimer();
      });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { _markDone(); _stopTimer(); }
      };
    },
    stop() {
      // Stop only the active tab's stream — other tabs keep running their
      // own. Uses tabState[currentId] as the authoritative source for what
      // ES to close. Backend uses SDK's client.interrupt() — keeps the
      // client / CLI subprocess alive so the next message continues the
      // same conversation without reloading CLAUDE.md / MCP / system prompt.
      const sid = this.currentId;
      const st = this._ensureTabState(sid);
      if (st.es) { try { st.es.close(); } catch {} st.es = null; }
      st.streaming = false;
      this.streaming = false; this.es = null;
      if (st._streamTimer) { clearInterval(st._streamTimer); st._streamTimer = null; }
      this._streamTimer = null; this.streamElapsed = 0;
      fetch("/api/chat/interrupt?token=" + encodeURIComponent(this.token)
            + "&session_id=" + encodeURIComponent(sid),
            { method: "POST" });
    },

    // ====== ask_user_question UI helpers ======
    // Defensive label/description extraction. The backend now normalizes
    // option objects to `{label, description}` (see ask_user_question.py
    // _normalize_questions), but we keep this fallback so a frontend
    // running against an older backend, or a future malformed payload,
    // doesn't render buttons with empty text.
    askOptionLabel(opt) {
      if (opt == null) return "";
      if (typeof opt === "string") return opt;
      return String(opt.label || opt.text || opt.name || opt.value || "");
    },
    askOptionDesc(opt) {
      if (opt == null || typeof opt === "string") return "";
      return String(opt.description || opt.desc || opt.detail || "");
    },
    // Single-select: user clicks an option → submit immediately.
    pickAskOption(msg, qIdx, optionLabel) {
      if (msg.submitted) return;
      const q = msg.questions[qIdx];
      msg.pendingAnswers[q.question] = optionLabel;
      // If single-select AND all questions answered → submit
      if (!q.multiSelect && this._allAskQuestionsAnswered(msg)) {
        this.submitAskAnswers(msg);
      }
    },
    // Multi-select: user toggles a checkbox; submitted via the "提交" button.
    toggleAskOption(msg, qIdx, optionLabel) {
      if (msg.submitted) return;
      const q = msg.questions[qIdx];
      const key = q.question;
      const cur = msg.pendingAnswers[key];
      const arr = Array.isArray(cur) ? cur.slice() : [];
      const i = arr.indexOf(optionLabel);
      if (i >= 0) arr.splice(i, 1); else arr.push(optionLabel);
      msg.pendingAnswers[key] = arr;
    },
    isAskOptionPicked(msg, qIdx, optionLabel) {
      const q = msg.questions[qIdx];
      const cur = msg.pendingAnswers[q.question];
      if (q.multiSelect) return Array.isArray(cur) && cur.includes(optionLabel);
      return cur === optionLabel;
    },
    _allAskQuestionsAnswered(msg) {
      return msg.questions.every(q => {
        const v = msg.pendingAnswers[q.question];
        if (q.multiSelect) return Array.isArray(v) && v.length > 0;
        return v != null;
      });
    },
    // "Other" free-text fallback. The MCP tool only lets the model give
    // 2-4 fixed buttons; when none fit, the user opens this and types
    // a regular reply. The typed text is sent as the answer for EVERY
    // pending question on the card (typical case is one question; for
    // multi-question cards the model gets the same custom reply for
    // each Q, which is fine — it's only meaningful when no other
    // option fits, so duplicating beats forcing the user to type N
    // separate replies).
    openAskOther(msg) {
      if (msg.submitted) return;
      msg.askOtherOpen = true;
      if (msg.askOtherText == null) msg.askOtherText = "";
      // Focus the textarea on next tick — Alpine has to apply the x-show
      // toggle first or the element isn't in the DOM yet.
      this.$nextTick(() => {
        const ta = document.querySelector(".ask-question .ask-other-textarea");
        if (ta && typeof ta.focus === "function") ta.focus();
      });
    },
    cancelAskOther(msg) {
      msg.askOtherOpen = false;
    },
    async submitAskOther(msg) {
      if (msg.submitted) return;
      const text = (msg.askOtherText || "").trim();
      if (!text) {
        this.toast(this.lang === "zh" ? "请输入回复" : "Please enter a reply",
                    "warn", 2000);
        return;
      }
      msg.submitted = true;
      // Use the typed text as the answer for every question on the card.
      // Backend's _normalize_questions has already canonicalized q.question
      // into the keying we need.
      const answers = {};
      for (const q of (msg.questions || [])) {
        answers[q.question] = text;
      }
      try {
        const r = await fetch(
          `/api/chat/answer/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ answers }),
          },
        );
        if (!r.ok) {
          msg.submitted = false;
          this.toast(this.t("ask.submit_failed"), "error", 3000);
        }
      } catch (e) {
        msg.submitted = false;
        this.toast(this.t("ask.submit_failed"), "error", 3000);
      }
    },
    async submitAskAnswers(msg) {
      if (msg.submitted) return;
      if (!this._allAskQuestionsAnswered(msg)) {
        this.toast(this.t("ask.unanswered"), "warn", 2000);
        return;
      }
      msg.submitted = true;
      try {
        const r = await fetch(
          `/api/chat/answer/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ answers: msg.pendingAnswers }),
          },
        );
        if (!r.ok) {
          msg.submitted = false;
          this.toast(this.t("ask.submit_failed"), "error", 3000);
        }
      } catch (e) {
        msg.submitted = false;
        this.toast(this.t("ask.submit_failed"), "error", 3000);
      }
    },
    // ====== permission_request helpers ======
    async decidePermission(msg, decision) {
      if (msg.resolved) return;
      msg.resolved = true;
      msg.decision = decision;
      try {
        const r = await fetch(
          `/api/chat/permission/${encodeURIComponent(this.currentId)}/${encodeURIComponent(msg.id)}`,
          {
            method: "POST",
            headers: { ...this.hdr(), "Content-Type": "application/json" },
            body: JSON.stringify({ decision }),
          },
        );
        if (!r.ok) {
          msg.resolved = false;
          msg.decision = null;
          this.toast(this.t("perm.submit_failed"), "error", 3000);
        }
      } catch (e) {
        msg.resolved = false;
        msg.decision = null;
        this.toast(this.t("perm.submit_failed"), "error", 3000);
      }
    },

    async togglePinSession(sid) {
      const s = this.sessions.find(x => x.id === sid);
      if (!s) return;
      const newPinned = !s.pinned;
      // Optimistic UI update so the row jumps to the top instantly.
      s.pinned = newPinned;
      this.sessions = [...this.sessions];   // trigger sort re-render
      const { ok } = await this.api(`/api/chat/sessions/${sid}`, {
        method: "PATCH", json: { pinned: newPinned },
      });
      if (!ok) {
        s.pinned = !newPinned;   // revert
        this.toast(this.lang === "zh" ? "操作失败" : "Failed", "error");
        return;
      }
      await this.refreshSessions();
    },

    openLightbox(src) {
      if (!src) return;
      this.lightbox = { show: true, src };
    },

    retryFailedMessage(m) {
      if (!m || m.role !== "user" || !m._failed) return;
      // Drop the failed bubble, put text back in input, and send.
      const idx = this.messages.indexOf(m);
      if (idx >= 0) this.messages.splice(idx, 1);
      this.input = m.text || "";
      // pendingImages/Docs we don't have here (preview state) — re-prompt
      // user to re-attach if they had files. Acceptable: error retry is rare.
      this.$nextTick(() => {
        const ta = this.$refs.chatInput;
        if (ta) { ta.focus(); this.autoGrow(ta); }
        this.send();
      });
    },

    _humanizeStreamError(raw) {
      // Translate the raw SDK / vendor error strings into something the user
      // can act on. Falls through to the raw text if no pattern matches —
      // better to show something technical than swallow useful info.
      const zh = this.lang === "zh";
      const s = String(raw || "");
      if (/401|unauthorized|invalid.api.key/i.test(s))
        return zh ? "API key 无效，去 Settings 检查" : "Invalid API key — check Settings";
      if (/429|rate.?limit|too many/i.test(s))
        return zh ? "请求频率超限，等几秒再试" : "Rate limit hit — wait a few seconds";
      if (/quota|credit|insufficient.*balance/i.test(s))
        return zh ? "账户额度不足，去 vendor 控制台充值" : "Out of credit — top up at vendor console";
      if (/timeout|timed out/i.test(s))
        return zh ? "请求超时，可能模型在长上下文上忙，重试一下" : "Timed out — retry";
      if (/network|connection|ECONNREFUSED|fetch/i.test(s))
        return zh ? "网络断开，检查连接后重试" : "Network down — check connection";
      if (/context.*length|maximum.*tokens|too long/i.test(s))
        return zh ? "对话太长了，先压缩历史再试" : "Conversation too long — compact then retry";
      if (/already in use/i.test(s))
        return zh ? "session 还被上一次的 CLI 占着 — 等几秒再试或切回原模型" : "Session still locked by previous CLI — wait a moment or switch model back";
      if (/Command failed with exit code|ProcessError/i.test(s))
        return zh ? "CLI 子进程异常退出（看 systemctl --user logs muselab）" : "CLI subprocess exited unexpectedly (check service logs)";
      if (/thinking.*signature|cross.*vendor/i.test(s))
        return zh ? "跨厂商切换模型遇到 thinking-signature 问题 — 新建会话或压缩历史" : "Cross-vendor thinking-signature mismatch — new chat or compact history";
      // Default: prefix + raw, so technical detail isn't lost but framed.
      return (zh ? "Muse 出错：" : "Muse error: ") + s;
    },
    copyMsg(m) {
      const text = m.text || "";
      navigator.clipboard?.writeText(text).then(
        () => {
          this.toast(this.t("toast.copied"), "success", 1500);
          // Inline ✓ feedback on the message — sets a flag the template
          // reads to swap the copy icon to a check for 1.2s. Faster signal
          // than the toast, which appears at the screen edge.
          m._copied = true;
          setTimeout(() => { m._copied = false; }, 1200);
        },
        () => this.errToast("copy", this.lang === "zh" ? "需要 HTTPS" : "HTTPS required")
      );
    },

    escape(s) {
      return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },
    // Render a user message: HTML-escape + convert raw `\n` into <br>.
    // Replaces the prior `x-text + white-space: pre-wrap` approach,
    // which caused fit-content to compute max-content per-line — so a
    // multi-line user message could never grow wider than its longest
    // single line, leaving an asymmetric gap vs. muse's continuous
    // markdown blocks. With <br> the bubble's max-content collapses
    // to the longest CONTINUOUS line (still subject to the same forced
    // break, but for typical single-paragraph user input this lines
    // up with muse's bubble at the same max-width edge).
    userTextHtml(text) {
      return this.escape(text || "").replace(/\n/g, "<br>");
    },

    // ===== command palette =====
    openPalette() {
      this.palette.query = "";
      this.palette.activeIndex = 0;
      this.palette.fileResults = [];
      this.palette.fileQuery = "";
      this.palette.messageResults = [];
      this.palette.messageQuery = "";
      this.palette.show = true;
      this.$nextTick(() => {
        const el = document.querySelector(".cmd-palette-input");
        if (el) el.focus();
      });
    },
    closePalette() { this.palette.show = false; },
    // Cross-session full-text message search. Mirrors _fetchPaletteFiles
    // shape — debounced from palette input, race-safe via query echo
    // check. Server caps at 30 hits.
    async _fetchPaletteMessages() {
      const q = this.palette.query.trim();
      if (q.length < 2) {
        this.palette.messageResults = [];
        this.palette.messageQuery = "";
        return;
      }
      if (q === this.palette.messageQuery) return;
      this.palette.messageQuery = q;
      this.palette.messageLoading = true;
      try {
        const r = await fetch(
          "/api/chat/search?q=" + encodeURIComponent(q) + "&limit=20",
          { headers: this.hdr() });
        if (!r.ok) { this.palette.messageResults = []; return; }
        const data = await r.json();
        if (this.palette.query.trim() === q) {
          this.palette.messageResults = data.hits || [];
        }
      } catch {
        this.palette.messageResults = [];
      } finally {
        this.palette.messageLoading = false;
      }
    },
    // Jump to a session and scroll to a specific message uuid. Used by
    // the palette's message-search results. If the session isn't open
    // yet, openTab handles loading; we wait until the messages are
    // rendered before scrolling.
    async _jumpToMessage(sid, uuid) {
      await this.openTab(sid);
      // openTab fires loadSession async — give it a tick or two to render.
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 50));
        const target = document.querySelector(
          `.msg[data-uuid="${CSS.escape(uuid)}"]`);
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.add("msg-highlight");
          setTimeout(() => target.classList.remove("msg-highlight"), 2400);
          return;
        }
      }
    },
    // Fetch files matching the current palette query against the whole
    // archive (not just loaded tree rows). Called from the palette input's
    // debounced @input. Idempotent — skips if the query hasn't changed.
    async _fetchPaletteFiles() {
      const q = this.palette.query.trim();
      if (q.length < 2) {
        this.palette.fileResults = [];
        this.palette.fileQuery = "";
        return;
      }
      if (q === this.palette.fileQuery) return;
      this.palette.fileQuery = q;
      this.palette.fileLoading = true;
      try {
        const r = await fetch(
          "/api/files/search?q=" + encodeURIComponent(q) + "&limit=30",
          { headers: this.hdr() });
        if (!r.ok) { this.palette.fileResults = []; return; }
        const data = await r.json();
        // Race: only commit if the user hasn't typed something else since
        // we kicked off this request.
        if (this.palette.query.trim() === q) {
          this.palette.fileResults = (data.entries || []).filter(n => !n.is_dir);
        }
      } catch {
        this.palette.fileResults = [];
      } finally {
        this.palette.fileLoading = false;
      }
    },
    // Build the item list freshly each render — cheap (few hundred entries
    // at most) and keeps logic out of x-show templates. Item shape:
    //   { type, label, hint, run }
    paletteItems() {
      const zh = this.lang === "zh";
      const q = this.palette.query.trim().toLowerCase();
      const items = [];

      // 1) Quick actions — always available
      const actions = [
        { type: "act", label: zh ? "新建会话" : "New session",
          hint: "Ctrl+T", run: () => this.newSession() },
        { type: "act", label: zh ? "打开设置" : "Open settings",
          hint: "⚙", run: () => this.openSettings() },
        { type: "act", label: zh ? "切换主题（深/浅）" : "Toggle theme",
          hint: "", run: () => this.toggleTheme() },
        { type: "act", label: zh ? "切换语言到 English" : "Switch language to 中文",
          hint: "", run: () => this.setLang(zh ? "en" : "zh") },
        { type: "act", label: zh ? "刷新文件树" : "Refresh file tree",
          hint: "", run: () => this.reloadTree() },
        { type: "act", label: zh ? "压缩当前会话历史" : "Compact session history",
          hint: "", run: () => this.runCompact() },
        { type: "act", label: zh ? "退出登录" : "Logout",
          hint: "", run: () => this.logout() },
      ];
      items.push(...actions);

      // 2) Open sessions — switch to any session
      for (const s of (this.sessions || [])) {
        items.push({
          type: "session",
          label: s.name || "(untitled)",
          hint: zh ? "会话" : "session",
          run: () => this.activateTab(s.id),
          _searchExtra: (s.first_prompt || "").slice(0, 100),
        });
      }

      // 3) Files — server-side search across the whole archive (not
      // limited to the loaded tree view). Results in palette.fileResults
      // are pre-filtered server-side by name match; we still pass them
      // through the substring scorer below so they get ordered together
      // with action / session matches.
      for (const n of (this.palette.fileResults || [])) {
        if (n.is_dir) continue;
        items.push({
          type: "file",
          label: n.name,
          hint: n.path,
          run: () => this.openFile(n),
        });
      }

      // 4) Cross-session message hits — pre-filtered server-side via
      // /api/chat/search. Each item already matched `q`, so we mark
      // _searchExtra with the full snippet to make sure the substring
      // scorer below keeps them.
      for (const h of (this.palette.messageResults || [])) {
        items.push({
          type: "message",
          label: h.snippet,
          hint: (h.name || "(untitled)") + " · " + (h.role || ""),
          run: () => this._jumpToMessage(h.sid, h.uuid),
          _searchExtra: h.snippet,
        });
      }

      // Fuzzy filter — substring match over label + hint + _searchExtra.
      // Empty query returns first 30 items (so opening the palette without
      // typing still shows something useful — most-recent sessions, etc).
      if (!q) return items.slice(0, 30);
      const matched = [];
      for (const it of items) {
        const hay = (it.label + " " + (it.hint || "") + " " + (it._searchExtra || "")).toLowerCase();
        const i = hay.indexOf(q);
        if (i >= 0) matched.push({ it, score: i + (it.type === "act" ? -100 : 0) });
      }
      matched.sort((a, b) => a.score - b.score);
      return matched.slice(0, 40).map(m => m.it);
    },
    paletteMove(delta) {
      const list = this.paletteItems();
      if (!list.length) return;
      this.palette.activeIndex =
        (this.palette.activeIndex + delta + list.length) % list.length;
    },
    paletteRun(item) {
      if (!item || !item.run) return;
      this.closePalette();
      // Defer the run so the modal close transition can paint before any
      // heavy action (e.g. activateTab triggering loadSession's fetch).
      this.$nextTick(() => { try { item.run(); } catch (e) { console.error(e); } });
    },
    onPaletteEnter() {
      const list = this.paletteItems();
      const idx = Math.min(this.palette.activeIndex, list.length - 1);
      this.paletteRun(list[idx]);
    },

    paletteIcon(type) {
      // Tiny svg id mapping; falls back to a dot if unknown.
      return ({ act: "#i-settings", session: "#i-file-text",
                 file: "#i-file", message: "#i-search" })[type] || "#i-circle";
    },

    // ===== scheduler drawer =====
    async openScheduler() {
      this.scheduler.show = true;
      await this.loadSchedulerTasks();
      await this.loadSchedulerHistory();
      // Opening the drawer = user has seen unread results. Server-side
      // ack so the badge clears on this AND any other tab.
      if (this.scheduler.unreadCount > 0) await this.ackSchedulerUnread();
      // First open ever, or after a reset — populate the draft's model
      // with whatever the user has selected in the chat UI so new tasks
      // don't silently default to an SDK fallback model.
      if (!this.scheduler.draft.editingId && !this.scheduler.draft.model) {
        this.scheduler.draft.model = this.model || "";
      }
    },
    closeScheduler() { this.scheduler.show = false; },
    _resetSchedDraft() {
      this.scheduler.draft = {
        editingId: null,
        name: "", prompt: "", model: this.model || "",
        kind: "daily",
        hour: 9, minute: 0,
        weekdays: [1, 2, 3, 4, 5],
        day: 1,
        onceDate: "",
      };
    },
    // Load an existing task into the draft form. The same form template
    // then becomes "edit mode" because draft.editingId is set; the save
    // button switches to PATCH and a Cancel button appears.
    editSchedTask(t) {
      if (!t) return;
      const s = t.schedule || {};
      this.scheduler.draft = {
        editingId: t.id,
        name: t.name || "",
        prompt: t.prompt || "",
        model: t.model || "",
        kind: s.kind || "daily",
        hour: (typeof s.hour === "number") ? s.hour : 9,
        minute: (typeof s.minute === "number") ? s.minute : 0,
        weekdays: Array.isArray(s.weekdays) ? s.weekdays.slice() : [1, 2, 3, 4, 5],
        day: (typeof s.day === "number") ? s.day : 1,
        onceDate: (s.kind === "once" && s.year && s.month && s.day)
          ? `${s.year}-${String(s.month).padStart(2, "0")}-${String(s.day).padStart(2, "0")}`
          : "",
      };
      // Scroll the form into view — list rows are below it so the user
      // is otherwise looking at empty form they can't see.
      this.$nextTick(() => {
        const el = document.querySelector(".sched-create");
        if (el && typeof el.scrollIntoView === "function") {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    },
    cancelEditSched() { this._resetSchedDraft(); },
    async loadSchedulerTasks() {
      this.scheduler.loading = true;
      try {
        const r = await fetch("/api/scheduler/tasks", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          this.scheduler.tasks = d.tasks || [];
          this.scheduler.unreadCount = d.unread_count || 0;
        }
      } finally {
        this.scheduler.loading = false;
      }
    },
    async loadSchedulerHistory() {
      const r = await fetch("/api/scheduler/history?limit=30", { headers: this.hdr() });
      if (r.ok) {
        const d = await r.json();
        this.scheduler.history = d.history || [];
        this.scheduler.unreadCount = d.unread_count || 0;
      }
    },
    async createSchedTask() {
      const d = this.scheduler.draft;
      if (!d.name.trim() || !d.prompt.trim()) {
        this.toast(this.lang === "zh"
          ? "任务名 / prompt 不能为空" : "Name and prompt are required",
          "warn", 2500);
        return;
      }
      // Build the schedule dict per kind. Backend's ScheduleIn validates
      // ranges + ignores fields irrelevant to the chosen kind.
      // tz_offset_minutes is east-positive (Beijing=+480, NYC=-240); JS
      // reports east-negative so we flip the sign. Sent on every create/
      // edit so the backend fires at the user's local hh:mm regardless of
      // where the server clock thinks it is — fixes the Docker/UTC case
      // where "daily 09:00" fired at 17:00 Beijing time.
      const sched = {
        kind: d.kind,
        hour: Number(d.hour),
        minute: Number(d.minute),
        tz_offset_minutes: -new Date().getTimezoneOffset(),
      };
      if (d.kind === "weekly") {
        if (!d.weekdays.length) {
          this.toast(this.lang === "zh"
            ? "至少选一天" : "Pick at least one weekday", "warn", 2500);
          return;
        }
        sched.weekdays = d.weekdays.slice();
      } else if (d.kind === "monthly") {
        sched.day = Number(d.day);
      } else if (d.kind === "once") {
        if (!d.onceDate) {
          this.toast(this.lang === "zh"
            ? "选个日期" : "Pick a date", "warn", 2500);
          return;
        }
        const [y, m, dy] = d.onceDate.split("-").map(Number);
        sched.year = y; sched.month = m; sched.day = dy;
      }
      const isEdit = !!d.editingId;
      const url = isEdit
        ? "/api/scheduler/tasks/" + encodeURIComponent(d.editingId)
        : "/api/scheduler/tasks";
      const r = await fetch(url, {
        method: isEdit ? "PATCH" : "POST",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({
          name: d.name.trim(),
          prompt: d.prompt.trim(),
          schedule: sched,
          model: d.model || "",
        }),
      });
      if (!r.ok) {
        const err = await r.text();
        const verb = isEdit
          ? (this.lang === "zh" ? "保存失败：" : "Save failed: ")
          : (this.lang === "zh" ? "创建失败：" : "Create failed: ");
        this.toast(verb + err, "error", 4000);
        return;
      }
      this._resetSchedDraft();
      await this.loadSchedulerTasks();
      this.toast(
        isEdit
          ? (this.lang === "zh" ? "已保存" : "Saved")
          : (this.lang === "zh" ? "任务已创建" : "Task created"),
        "success", 2000);
    },
    toggleDraftWeekday(w) {
      const wds = this.scheduler.draft.weekdays;
      const i = wds.indexOf(w);
      if (i >= 0) wds.splice(i, 1);
      else wds.push(w);
    },
    fmtSchedule(s) {
      // Human-readable summary of a schedule dict — appears next to the
      // task name in the list.
      if (!s) return "";
      const zh = this.lang === "zh";
      const hh = String(s.hour).padStart(2, "0") + ":"
                + String(s.minute).padStart(2, "0");
      if (s.kind === "daily") return (zh ? "每天 " : "Daily ") + hh;
      if (s.kind === "weekly") {
        const names = zh
          ? ["一", "二", "三", "四", "五", "六", "日"]
          : ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const days = (s.weekdays || []).sort().map(w => names[w]).join(
          zh ? "、" : " ");
        return (zh ? "每周" : "Weekly ") + days + " " + hh;
      }
      if (s.kind === "monthly") {
        return zh ? `每月 ${s.day} 日 ${hh}` : `Monthly day ${s.day} ${hh}`;
      }
      if (s.kind === "once") {
        return zh
          ? `${s.year}-${String(s.month).padStart(2,"0")}-${String(s.day).padStart(2,"0")} ${hh}`
          : `Once ${s.year}-${String(s.month).padStart(2,"0")}-${String(s.day).padStart(2,"0")} ${hh}`;
      }
      return hh;
    },
    async deleteSchedTask(t) {
      const zh = this.lang === "zh";
      const ok = await this.confirm({
        title: zh ? "删除任务" : "Delete task",
        body: zh
          ? `确定删除「${t.name}」？关联的 [定时] 会话会一并删除。`
          : `Delete "${t.name}"? The bound [Scheduled] session is removed too.`,
        danger: true,
        okText: zh ? "删除" : "Delete",
      });
      if (!ok) return;
      const r = await fetch("/api/scheduler/tasks/" + encodeURIComponent(t.id),
        { method: "DELETE", headers: this.hdr() });
      if (r.ok) {
        if (this.scheduler.draft.editingId === t.id) this._resetSchedDraft();
        await this.loadSchedulerTasks();
      }
    },
    async toggleSchedEnabled(t) {
      const r = await fetch("/api/scheduler/tasks/" + encodeURIComponent(t.id), {
        method: "PATCH",
        headers: { ...this.hdr(), "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !t.enabled }),
      });
      if (r.ok) await this.loadSchedulerTasks();
    },
    // Out-of-schedule run. Backend dispatches a background task; we poll
    // history once and again after a short delay so the new entry shows
    // up without waiting for the next periodic refresh.
    async runSchedTaskNow(t) {
      if (!t || !t.id || this.schedRunning[t.id]) return;
      this.schedRunning[t.id] = true;
      try {
        const r = await fetch(
          "/api/scheduler/tasks/" + encodeURIComponent(t.id) + "/run",
          { method: "POST", headers: this.hdr() });
        if (!r.ok) throw new Error(await r.text());
        this.toast(this.lang === "zh" ? "已触发，等待结果…" : "Triggered — awaiting result…",
                    "info", 2000);
        // Poll history a couple of times to surface the new entry; backend
        // appends synchronously in _execute_task's finally so a few-second
        // delay catches even slow LLM turns.
        setTimeout(() => this.loadSchedulerHistory(), 1500);
        setTimeout(async () => {
          await this.loadSchedulerHistory();
          this.schedRunning[t.id] = false;
        }, 8000);
      } catch (e) {
        this.toast((this.lang === "zh" ? "触发失败: " : "Trigger failed: ")
                    + ((e && e.message) || e), "error", 4000);
        this.schedRunning[t.id] = false;
      }
    },
    retrySchedHistory(h) {
      if (!h || !h.task_id) return;
      const task = this.scheduler.tasks.find(t => t.id === h.task_id);
      if (task) this.runSchedTaskNow(task);
    },
    async ackSchedulerUnread() {
      const r = await fetch("/api/scheduler/ack", {
        method: "POST", headers: this.hdr(),
      });
      if (r.ok) {
        const d = await r.json();
        this.scheduler.unreadCount = d.unread_count || 0;
      }
    },
    async fetchSchedulerUnread() {
      // Called from the heartbeat — keeps the bell badge live without
      // requiring the user to open the drawer. Also detects a tick-up
      // since last poll → triggers foreground vibration (if user opted in).
      try {
        const r = await fetch("/api/scheduler/tasks", { headers: this.hdr() });
        if (r.ok) {
          const d = await r.json();
          const next = d.unread_count || 0;
          if (next > this._lastSeenUnread && this.notifyPrefs.vibrate) {
            // 3-pulse "task done" pattern. navigator.vibrate is a no-op
            // (returns false) on devices without a vibration motor, so
            // it's safe to call unconditionally.
            try { navigator.vibrate?.([120, 60, 120]); } catch {}
          }
          this._lastSeenUnread = next;
          this.scheduler.unreadCount = next;
        }
      } catch {}
    },
    saveNotifyPrefs() {
      try {
        localStorage.setItem("muselab_notify",
          JSON.stringify(this.notifyPrefs));
      } catch {}
    },
    async onPushToggle(ev) {
      const wantOn = ev?.target?.checked ?? this.notifyPrefs.push;
      if (wantOn) {
        const ok = await this.pushSubscribe();
        this.notifyPrefs.push = ok;
        if (ev?.target) ev.target.checked = ok;
      } else {
        await this.pushUnsubscribe();
        this.notifyPrefs.push = false;
      }
      this.saveNotifyPrefs();
    },
    async pushSubscribe() {
      // Browser feature checks upfront so we can give a clearer error
      // than "TypeError: Cannot read property 'subscribe' of undefined".
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        this.toast(this.lang === "zh"
          ? "此浏览器不支持 Web Push" : "This browser doesn't support Web Push",
          "warn", 3500);
        return false;
      }
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          this.toast(this.lang === "zh"
            ? "未授权通知 — 在浏览器设置里允许后重试"
            : "Notification permission denied", "warn", 4000);
          return false;
        }
        // Make sure the SW is installed + activated before we touch
        // pushManager — pushManager.subscribe on an installing worker
        // throws on Firefox.
        const reg = await navigator.serviceWorker.register("/sw.js");
        await navigator.serviceWorker.ready;
        // Public key arrives as urlsafe-b64. PushManager wants raw bytes.
        const r = await fetch("/api/push/vapid-public", { headers: this.hdr() });
        if (!r.ok) throw new Error("vapid fetch failed: " + r.status);
        const { public_key } = await r.json();
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: this._urlsafeB64ToBytes(public_key),
        });
        const sr = await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify(sub.toJSON()),
        });
        if (!sr.ok) throw new Error("subscribe POST failed: " + sr.status);
        this.toast(this.lang === "zh"
          ? "已开启推送通知" : "Push notifications enabled",
          "success", 2500);
        return true;
      } catch (e) {
        this.toast((this.lang === "zh" ? "开启失败：" : "Push subscribe failed: ")
          + (e.message || e), "error", 5000);
        return false;
      }
    },
    async pushUnsubscribe() {
      try {
        if (!("serviceWorker" in navigator)) return;
        const reg = await navigator.serviceWorker.getRegistration("/sw.js");
        if (!reg) return;
        const sub = await reg.pushManager.getSubscription();
        if (!sub) return;
        const endpoint = sub.endpoint;
        await sub.unsubscribe();
        await fetch("/api/push/unsubscribe", {
          method: "POST",
          headers: { ...this.hdr(), "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint }),
        });
      } catch (e) {
        // Silent — even if the cleanup fails, the local subscription is gone.
      }
    },
    _urlsafeB64ToBytes(s) {
      const pad = "=".repeat((4 - (s.length % 4)) % 4);
      const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
      const raw = atob(b64);
      const buf = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
      return buf;
    },
    loadNotifyPrefs() {
      try {
        const p = JSON.parse(localStorage.getItem("muselab_notify") || "{}");
        if (typeof p.vibrate === "boolean") this.notifyPrefs.vibrate = p.vibrate;
        if (typeof p.push === "boolean")    this.notifyPrefs.push    = p.push;
      } catch {}
    },
    async openSchedTaskSession(t) {
      // Jump straight to the muselab session bound to this scheduled
      // task. Use openTab — NOT activateTab — because the bound session
      // may not be in openTabIds yet (user hasn't manually opened it).
      // activateTab only switches currentId; without push to openTabIds
      // the tab strip wouldn't show this session and the user would see
      // messages with no visible tab label.
      this.closeScheduler();
      const sid = (t && t.session_id) || t;
      if (!sid) return;
      await this.openTab(sid);
    },
    fmtSchedTime(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const pad = n => String(n).padStart(2, "0");
      const today = new Date();
      const sameDay = d.toDateString() === today.toDateString();
      const hh = pad(d.getHours()) + ":" + pad(d.getMinutes());
      if (sameDay) return (this.lang === "zh" ? "今天 " : "today ") + hh;
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hh}`;
    },
  };
}
